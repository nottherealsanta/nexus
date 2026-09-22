"""Phase 5 P5-A: the normalized MCP client (plan section 5.5).

The tests are deliberately offline: a real, deterministic fake stdio server
(``tests/fixtures/mcp/fake_server.py``) is spawned as a subprocess, HTTP
transports run against ``httpx`` mock/streaming transports, and the optional
``mcp`` package is never required. What they pin down:

* normalized types at the boundary -- no upstream object escapes;
* explicit ``${env:VAR}`` interpolation only, and redaction of every secret;
* stdio framing and lifecycle, including process-group cleanup on close, a
  deadline, and cancellation;
* bounded, redacted stderr that never reaches a result or an event;
* streamable HTTP and legacy SSE framing with no network;
* lazy/optional official wrapping, with a clear ``MCPUnavailable`` when absent.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from nexus.mcp.client import (
    FileStderrSink,
    MCPCallResult,
    MCPClient,
    MCPContent,
    MCPPrompt,
    MCPResource,
    MCPServerConfig,
    MCPTool,
    MemoryStderrSink,
    NullStderrSink,
    _OfficialAPI,
    load_official_api,
    official_available,
    parse_server_config,
    redact_secrets,
)
from nexus.mcp.errors import (
    MCPCallError,
    MCPClosed,
    MCPConfigError,
    MCPError,
    MCPProtocolError,
    MCPTimeout,
    MCPTransportError,
    MCPUnavailable,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp" / "fake_server.py"
SECRET = "sk-super-secret-fixture-token-987654321"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def stdio_config(
    *, mode: str = "normal", env: dict[str, str] | None = None, **overrides: Any
) -> MCPServerConfig:
    raw: dict[str, Any] = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FIXTURE)],
        "env": {"MCP_FIXTURE_MODE": mode, **(env or {})},
        "connect_timeout_s": 2.0,
        "init_timeout_s": 2.0,
        "list_timeout_s": 2.0,
        "call_timeout_s": 2.0,
    }
    raw.update(overrides)
    return parse_server_config("fake", raw, environ=os.environ)


async def connect(config: MCPServerConfig, **kwargs: Any) -> MCPClient:
    client = MCPClient(config, backend="native", **kwargs)
    await client.connect()
    return client


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def wait_gone(pid: int, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while pid_alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    return not pid_alive(pid)


async def read_pid_when_ready(path: Path, timeout: float = 4.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        await asyncio.sleep(0.05)
    raise AssertionError(f"grandchild pid file never appeared: {path}")


# ---------------------------------------------------------------------------
# Config parsing, interpolation, redaction
# ---------------------------------------------------------------------------


def test_stdio_requires_a_command():
    with pytest.raises(MCPConfigError):
        parse_server_config("s", {"transport": "stdio", "args": []})


def test_http_requires_a_url():
    with pytest.raises(MCPConfigError):
        parse_server_config("s", {"transport": "http"})


def test_transport_must_be_known():
    with pytest.raises(MCPConfigError):
        parse_server_config("s", {"transport": "carrier-pigeon", "url": "x"})


def test_unknown_keys_are_refused():
    with pytest.raises(MCPConfigError):
        parse_server_config("s", {"transport": "stdio", "command": "x", "typo": 1})


def test_only_explicit_env_interpolation_expands():
    config = parse_server_config(
        "s",
        {
            "transport": "stdio",
            "command": sys.executable,
            "env": {
                "A": "${env:TOKEN}",
                "B": "$TOKEN",
                "C": "${TOKEN}",
                "D": "pre-${env:TOKEN}-post",
            },
        },
        environ={"TOKEN": SECRET},
    )
    assert config.env["A"] == SECRET
    assert config.env["B"] == "$TOKEN"
    assert config.env["C"] == "${TOKEN}"
    assert config.env["D"] == f"pre-{SECRET}-post"


def test_missing_env_var_names_the_var_not_a_value():
    with pytest.raises(MCPConfigError) as info:
        parse_server_config(
            "s",
            {"transport": "stdio", "command": "x", "env": {"K": "${env:NOPE}"}},
            environ={},
        )
    assert "NOPE" in str(info.value)


def test_env_values_are_tracked_and_redacted():
    config = parse_server_config(
        "s",
        {
            "transport": "stdio",
            "command": "x",
            "env": {"TOKEN": SECRET},
            "headers": {"Authorization": f"Bearer {SECRET}"},
        },
        environ={},
    )
    assert SECRET in config.secrets
    text = config.redact(f"failed with token={SECRET}")
    assert SECRET not in text
    assert "***" in text


def test_url_userinfo_is_tracked_as_a_secret():
    config = parse_server_config(
        "s",
        {"transport": "http", "url": f"https://user:{SECRET}@example.invalid/mcp"},
        environ={},
    )
    assert SECRET in config.secrets
    assert SECRET not in config.redact(config.url)


def test_argv_is_a_list_never_a_shell_string():
    config = parse_server_config(
        "s",
        {"transport": "stdio", "command": "my-server", "args": ["-y", "pkg; rm -rf /"]},
        environ={},
    )
    assert config.command == "my-server"
    assert config.args == ("-y", "pkg; rm -rf /")


async def test_argv_reaches_the_child_without_a_shell(tmp_path: Path):
    argv_file = tmp_path / "argv.json"
    weird = "a b; $(touch /tmp/nexus-should-not-exist) & echo x"
    config = stdio_config(
        mode="normal",
        env={"MCP_FIXTURE_ARGV_FILE": str(argv_file)},
        args=[str(FIXTURE), weird],
    )
    client = await connect(config)
    try:
        deadline = time.monotonic() + 2.0
        while not argv_file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        received = json.loads(argv_file.read_text(encoding="utf-8"))
        assert received == [weird]
    finally:
        await client.aclose()


def test_timeouts_must_be_positive_finite():
    with pytest.raises(MCPConfigError):
        parse_server_config(
            "s",
            {"transport": "stdio", "command": "x", "call_timeout_s": -1},
            environ={},
        )


def test_redact_secrets_replaces_exact_and_prefixed_values():
    assert SECRET not in redact_secrets(f"a {SECRET} b", [SECRET])
    assert "sk-" not in redact_secrets("key=sk-abcdef123456", [])
    assert redact_secrets("value", []) == "value"


def test_config_repr_hides_secrets():
    config = parse_server_config(
        "s",
        {
            "transport": "stdio",
            "command": sys.executable,
            "env": {"TOKEN": SECRET},
            "headers": {"Authorization": f"Bearer {SECRET}"},
        },
        environ={},
    )
    text = repr(config)
    assert SECRET not in text
    assert "TOKEN" in text
    assert "redacted" in text


# ---------------------------------------------------------------------------
# Normalizers: dicts and duck-typed upstream objects both become normalized
# ---------------------------------------------------------------------------


def test_normalize_tool_from_dict():
    from nexus.mcp.client import _normalize_tool

    normalized = _normalize_tool(
        {
            "name": "echo",
            "description": "Echo",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True, "title": "Echo"},
        }
    )
    assert isinstance(normalized, MCPTool)
    assert normalized.read_only is True
    assert normalized.title == "Echo"


def test_normalize_tool_from_upstream_object():
    from nexus.mcp.client import _normalize_tool

    upstream = SimpleNamespace(
        name="echo",
        description="Echo",
        inputSchema={"type": "object"},
        annotations=SimpleNamespace(readOnlyHint=True, destructiveHint=False),
    )
    normalized = _normalize_tool(upstream)
    assert normalized.name == "echo"
    assert normalized.read_only is True


def test_normalize_unknown_content_is_preserved_as_text():
    from nexus.mcp.client import _normalize_content

    content = _normalize_content({"type": "future-thing", "text": "payload"})
    assert isinstance(content, MCPContent)
    assert content.type == "future-thing"
    assert content.text == "payload"


def test_normalize_embedded_resource_content():
    from nexus.mcp.client import _normalize_content

    content = _normalize_content(
        {
            "type": "resource",
            "resource": {"uri": "file:///a", "mimeType": "text/plain", "text": "hi"},
        }
    )
    assert content.type == "resource"
    assert content.uri == "file:///a"
    assert content.text == "hi"


# ---------------------------------------------------------------------------
# Stdio lifecycle against the fake server
# ---------------------------------------------------------------------------


async def test_stdio_connect_and_server_info():
    client = await connect(stdio_config())
    try:
        info = client.server_info
        assert info.name == "fake"
        assert info.version == "1.2.3"
        assert info.protocol_version == "2025-06-18"
        assert "tools" in info.capabilities
    finally:
        await client.aclose()


async def test_stdio_list_tools_normalizes_annotations():
    client = await connect(stdio_config())
    try:
        tools = await client.list_tools()
        names = [tool.name for tool in tools]
        assert names == ["echo", "boom"]
        echo = tools[0]
        assert isinstance(echo, MCPTool)
        assert type(echo) is MCPTool
        assert echo.read_only is True
        assert echo.input_schema["type"] == "object"
    finally:
        await client.aclose()


async def test_stdio_call_tool_returns_normalized_content():
    client = await connect(stdio_config())
    try:
        result = await client.call_tool("echo", {"text": "hello"})
        assert isinstance(result, MCPCallResult)
        assert type(result.content[0]) is MCPContent
        assert result.text() == "hello"
        assert result.is_error is False
    finally:
        await client.aclose()


async def test_stdio_tool_error_is_flagged_not_raised():
    client = await connect(stdio_config())
    try:
        result = await client.call_tool("boom")
        assert result.is_error is True
    finally:
        await client.aclose()


async def test_stdio_resources_and_prompts():
    client = await connect(stdio_config())
    try:
        resources = await client.list_resources()
        assert isinstance(resources[0], MCPResource)
        assert resources[0].uri == "file:///a.txt"
        contents = await client.read_resource("file:///a.txt")
        assert contents[0].text == "hello"

        prompts = await client.list_prompts()
        assert isinstance(prompts[0], MCPPrompt)
        assert prompts[0].arguments[0].required is True
        prompt_result = await client.get_prompt("greet", {"who": "you"})
        assert prompt_result.messages[0].text == "hi"
    finally:
        await client.aclose()


async def test_stdio_notification_stream():
    client = await connect(stdio_config())
    try:
        stream = client.notifications()
        notification = await asyncio.wait_for(anext(stream), timeout=2.0)
        assert notification.method == "notifications/tools/list_changed"
        assert notification.params == {"note": "ready"}
        await stream.aclose()
    finally:
        await client.aclose()


async def test_stdio_pagination_follows_cursor():
    client = await connect(stdio_config(mode="paginate"))
    try:
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["echo", "boom"]
    finally:
        await client.aclose()


async def test_operations_require_connect_first():
    client = MCPClient(stdio_config(), backend="native")
    with pytest.raises(MCPClosed):
        await client.list_tools()


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


async def test_server_death_mid_call_is_normalized():
    client = await connect(stdio_config(mode="die_mid_call"))
    try:
        with pytest.raises(MCPTransportError):
            await client.call_tool("echo")
    finally:
        await client.aclose()


async def test_hang_hits_the_deadline():
    config = stdio_config(mode="hang_tools_list", list_timeout_s=0.3)
    client = await connect(config)
    started = time.monotonic()
    try:
        with pytest.raises(MCPTimeout):
            await client.list_tools()
        assert time.monotonic() - started < 3.0
    finally:
        await client.aclose()


async def test_malformed_frame_is_a_protocol_error():
    client = await connect(stdio_config(mode="malformed_tools_list"))
    try:
        with pytest.raises(MCPProtocolError):
            await client.list_tools()
    finally:
        await client.aclose()


async def test_missing_command_is_a_transport_error():
    config = stdio_config(command="/definitely/not/here/nope")
    client = MCPClient(config, backend="native")
    with pytest.raises(MCPTransportError):
        await client.connect()


async def test_remote_error_secret_is_redacted():
    env = {"MCP_FIXTURE_SECRET": SECRET}
    client = await connect(stdio_config(mode="leak_error", env=env))
    try:
        with pytest.raises(MCPCallError) as info:
            await client.call_tool("leak")
        assert SECRET not in str(info.value)
        assert "***" in str(info.value)
        assert isinstance(info.value, MCPError)
    finally:
        await client.aclose()


async def test_server_exit_after_init_is_normalized():
    client = await connect(stdio_config(mode="exit_after_init"))
    try:
        with pytest.raises(MCPTransportError):
            await client.list_tools()
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Process-group cleanup, cancellation, stderr
# ---------------------------------------------------------------------------


async def test_aclose_kills_the_whole_process_group(tmp_path: Path):
    pid_file = tmp_path / "child.pid"
    env = {"MCP_FIXTURE_PID_FILE": str(pid_file)}
    client = await connect(stdio_config(mode="grandchild", env=env))
    child_pid = await read_pid_when_ready(pid_file)
    assert pid_alive(child_pid)
    await client.aclose()
    assert await wait_gone(child_pid)


async def test_deadline_cleanup_kills_the_group(tmp_path: Path):
    pid_file = tmp_path / "child.pid"
    env = {"MCP_FIXTURE_PID_FILE": str(pid_file)}
    config = stdio_config(mode="hang_grandchild", env=env, call_timeout_s=0.3)
    client = await connect(config)
    child_pid = await read_pid_when_ready(pid_file)
    try:
        with pytest.raises(MCPTimeout):
            await client.call_tool("echo")
    finally:
        await client.aclose()
    assert await wait_gone(child_pid)


async def test_cancellation_closes_resources(tmp_path: Path):
    pid_file = tmp_path / "child.pid"
    env = {"MCP_FIXTURE_PID_FILE": str(pid_file)}
    config = stdio_config(mode="hang_grandchild", env=env)
    client = await connect(config)
    child_pid = await read_pid_when_ready(pid_file)
    task = asyncio.create_task(client.call_tool("echo"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()
    assert await wait_gone(child_pid)


async def test_aclose_is_idempotent():
    client = await connect(stdio_config())
    await client.aclose()
    await client.aclose()
    assert client.connected is False


async def test_stderr_is_bounded_and_redacted_and_keeps_out_of_results():
    sink = MemoryStderrSink(max_bytes=4096)
    env = {"MCP_FIXTURE_SECRET": SECRET}
    client = await connect(
        stdio_config(mode="stderr_secret", env=env), stderr_sink=sink
    )
    try:
        result = await client.call_tool("echo", {"text": "ok"})
        deadline = time.monotonic() + 2.0
        while "boot" not in sink.text and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert "boot" in sink.text
        assert SECRET not in sink.text
        assert SECRET not in result.text()
        assert SECRET not in repr(client.server_info)
    finally:
        await client.aclose()


def test_memory_sink_truncates():
    sink = MemoryStderrSink(max_bytes=8)
    sink.write("12345")
    sink.write("67890")
    assert sink._size <= 8
    assert sink.text.endswith("[truncated]")


def test_file_sink_writes_and_bounds(tmp_path: Path):
    sink = FileStderrSink(tmp_path / "logs" / "server.log", max_bytes=5)
    sink.write("abc")
    sink.write("defghijk")
    sink.write("dropped")
    assert (tmp_path / "logs" / "server.log").read_text(encoding="utf-8") == "abcde"


def test_null_sink_discards():
    sink = NullStderrSink()
    sink.write("anything")
    assert sink.write("more") is None


# ---------------------------------------------------------------------------
# HTTP transports (mock/streaming, no network)
# ---------------------------------------------------------------------------


def _dispatch(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "http-fake", "version": "9"},
            "instructions": "http",
        }
    if method == "tools/list":
        return {"tools": [{"name": "echo", "description": "d", "inputSchema": {}}]}
    if method == "tools/call":
        if params.get("name") == "boom":
            return {"content": [{"type": "text", "text": "no"}], "isError": True}
        return {"content": [{"type": "text", "text": "pong"}], "isError": False}
    return {}


class _StreamableServer:
    def __init__(self, *, sse: bool = False, fail_status: int = 0) -> None:
        self.sse = sse
        self.fail_status = fail_status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append(request)
        if self.fail_status:
            return httpx.Response(self.fail_status, text="upstream boom")
        headers = (
            {"mcp-session-id": "sess-1"} if body.get("method") == "initialize" else {}
        )
        if "id" not in body:
            return httpx.Response(202, headers=headers)
        result = _dispatch(body.get("method", ""), body.get("params", {}))
        if self.sse:
            payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream", **headers},
                content=f"event: message\ndata: {payload}\n\n".encode(),
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json", **headers},
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
        )


def http_config(
    *, url: str = "https://mcp.example.invalid/mcp", **overrides: Any
) -> MCPServerConfig:
    raw: dict[str, Any] = {
        "transport": "http",
        "url": url,
        "headers": {"Authorization": "$" + "{env:MCP_HTTP_TOKEN}"},
        "connect_timeout_s": 2.0,
        "init_timeout_s": 2.0,
        "list_timeout_s": 2.0,
        "call_timeout_s": 2.0,
    }
    raw.update(overrides)
    return parse_server_config("httpfake", raw, environ={"MCP_HTTP_TOKEN": SECRET})


async def test_streamable_http_json_results():
    server = _StreamableServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handler)) as http:
        client = MCPClient(http_config(), backend="native", http_client=http)
        await client.connect()
        try:
            assert client.server_info.name == "http-fake"
            tools = await client.list_tools()
            assert tools[0].name == "echo"
            result = await client.call_tool("echo")
            assert result.text() == "pong"
        finally:
            await client.aclose()
    sent = [json.loads(req.content) for req in server.requests if req.content]
    methods = [body.get("method") for body in sent]
    assert "initialize" in methods
    assert "notifications/initialized" in methods


async def test_streamable_http_sse_framing():
    server = _StreamableServer(sse=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handler)) as http:
        client = MCPClient(http_config(), backend="native", http_client=http)
        await client.connect()
        try:
            tools = await client.list_tools()
            assert tools[0].name == "echo"
        finally:
            await client.aclose()


async def test_streamable_http_echoes_session_id():
    server = _StreamableServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handler)) as http:
        client = MCPClient(http_config(), backend="native", http_client=http)
        await client.connect()
        try:
            await client.list_tools()
        finally:
            await client.aclose()
    after_init = [
        r for r in server.requests if r.headers.get("mcp-session-id") == "sess-1"
    ]
    assert after_init
    assert all("authorization" in r.headers for r in server.requests)


async def test_streamable_http_error_is_normalized():
    server = _StreamableServer(fail_status=500)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handler)) as http:
        client = MCPClient(http_config(), backend="native", http_client=http)
        with pytest.raises(MCPTransportError):
            await client.connect()


class _QueueStream(httpx.AsyncByteStream):
    def __init__(self, queue: asyncio.Queue[Any]) -> None:
        self._queue = queue

    async def __aiter__(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def aclose(self) -> None:
        return None


class _LegacySSEServer(httpx.AsyncBaseTransport):
    def __init__(self, endpoint: str = "/messages") -> None:
        self.endpoint = endpoint
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.posts: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            await self.queue.put(f"event: endpoint\ndata: {self.endpoint}\n\n".encode())
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_QueueStream(self.queue),
            )
        body = json.loads(request.content) if request.content else {}
        self.posts.append(body)
        if "id" in body:
            result = _dispatch(body.get("method", ""), body.get("params", {}))
            payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
            await self.queue.put(f"event: message\ndata: {payload}\n\n".encode())
        return httpx.Response(202)


async def test_legacy_sse_transport():
    server = _LegacySSEServer()
    config = parse_server_config(
        "ssefake",
        {"transport": "sse", "url": "https://sse.example.invalid/sse"},
        environ={},
    )
    async with httpx.AsyncClient(transport=server) as http:
        client = MCPClient(config, backend="native", http_client=http)
        await client.connect()
        try:
            assert client.server_info.name == "http-fake"
            tools = await client.list_tools()
            assert tools[0].name == "echo"
        finally:
            await client.aclose()
    assert any(body.get("method") == "initialize" for body in server.posts)


# ---------------------------------------------------------------------------
# Injected transport: deterministic deadlines and idempotent close
# ---------------------------------------------------------------------------


class _ScriptedTransport:
    def __init__(self, *, hang_method: str | None = None) -> None:
        self.hang_method = hang_method
        self.opened = False
        self.closed = False
        self.sent: list[dict[str, Any]] = []
        self._queue: asyncio.Queue[Any] = asyncio.Queue()

    async def open(self) -> None:
        self.opened = True

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if message.get("method") == self.hang_method or "id" not in message:
            return
        if message["method"] == "initialize":
            result = _dispatch("initialize", {})
        elif message["method"] == "tools/list":
            result = _dispatch("tools/list", {})
        else:
            result = {}
        self._queue.put_nowait(
            {"jsonrpc": "2.0", "id": message["id"], "result": result}
        )

    async def frames(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)


async def test_injected_transport_lifecycle():
    transport = _ScriptedTransport()
    client = MCPClient(stdio_config(), backend="native", transport=transport)
    await client.connect()
    tools = await client.list_tools()
    assert tools[0].name == "echo"
    await client.aclose()
    await client.aclose()
    assert transport.opened is True
    assert transport.closed is True


async def test_injected_transport_hang_times_out():
    transport = _ScriptedTransport(hang_method="tools/list")
    config = stdio_config(list_timeout_s=0.2)
    client = MCPClient(config, backend="native", transport=transport)
    await client.connect()
    try:
        with pytest.raises(MCPTimeout):
            await client.list_tools()
        for _ in range(25):
            if any(
                m.get("method") == "notifications/cancelled" for m in transport.sent
            ):
                break
            await asyncio.sleep(0.02)
        assert any(m.get("method") == "notifications/cancelled" for m in transport.sent)
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Official backend: lazy, optional, normalized
# ---------------------------------------------------------------------------


def test_official_is_optional():
    if official_available():
        pytest.skip("official mcp package is installed")
    with pytest.raises(MCPUnavailable):
        load_official_api()


def test_importing_the_client_does_not_import_the_optional_package():
    code = (
        "import sys, nexus.mcp.client as client; "
        "assert 'mcp' not in sys.modules; "
        "print('mcp' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"


async def test_official_backend_unavailable_raises():
    if official_available():
        pytest.skip("official mcp package is installed")
    client = MCPClient(stdio_config(), backend="official")
    with pytest.raises(MCPUnavailable):
        await client.connect()


class _FakeOfficialInner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.initialized = False

    async def initialize(self) -> dict[str, Any]:
        self.initialized = True
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "official-fake", "version": "3"},
            "instructions": "official",
        }

    async def list_tools(self) -> Any:
        if self.fail:
            raise RuntimeError(f"official exploded token={SECRET}")
        return {
            "tools": [
                {
                    "name": "official",
                    "description": "d",
                    "inputSchema": {"type": "object"},
                    "annotations": {"readOnlyHint": True},
                }
            ]
        }

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return {"content": [{"type": "text", "text": "from-official"}]}


class _FakeClientSession:
    def __init__(self, read: Any, write: Any) -> None:
        self.inner = _FakeOfficialInner()

    async def __aenter__(self) -> _FakeOfficialInner:
        return self.inner

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeTransportCM:
    async def __aenter__(self) -> tuple[Any, Any]:
        return (object(), object())

    async def __aexit__(self, *exc: object) -> bool:
        return False


def fake_official_api(*, fail: bool = False) -> _OfficialAPI:
    class _Session(_FakeClientSession):
        def __init__(self, read: Any, write: Any) -> None:
            super().__init__(read, write)
            self.inner = _FakeOfficialInner(fail=fail)

    return _OfficialAPI(
        ClientSession=_Session,
        StdioServerParameters=lambda **kwargs: kwargs,
        stdio_client=lambda params: _FakeTransportCM(),
        streamable_http_client=lambda url, headers=None: _FakeTransportCM(),
        sse_client=lambda url, headers=None: _FakeTransportCM(),
    )


async def test_official_backend_normalizes_results():
    config = stdio_config(env={"MCP_FIXTURE_SECRET": SECRET})
    client = MCPClient(config, backend="official", official_api=fake_official_api())
    await client.connect()
    try:
        assert client.server_info.name == "official-fake"
        tools = await client.list_tools()
        assert type(tools[0]) is MCPTool
        assert tools[0].read_only is True
        result = await client.call_tool("official")
        assert result.text() == "from-official"
    finally:
        await client.aclose()


async def test_official_error_is_normalized_and_redacted():
    config = stdio_config(env={"MCP_FIXTURE_SECRET": SECRET})
    client = MCPClient(
        config, backend="official", official_api=fake_official_api(fail=True)
    )
    await client.connect()
    try:
        with pytest.raises(MCPTransportError) as info:
            await client.list_tools()
        assert SECRET not in str(info.value)
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Review hardening: redirects, timeouts, same-origin SSE, native default
# ---------------------------------------------------------------------------


async def test_http_client_disables_redirects_and_wires_connect_timeout():
    from nexus.mcp.client import _build_http_client

    config = http_config(connect_timeout_s=7.0)
    client = _build_http_client(config)
    try:
        assert client.follow_redirects is False
        assert client.timeout.connect == 7.0
        assert client.timeout.read is None
    finally:
        await client.aclose()


def test_same_origin_helper():
    from nexus.mcp.client import _same_origin

    assert _same_origin("https://a.example/mcp", "https://a.example:443/x")
    assert _same_origin("http://a.example/mcp", "http://a.example:80/x")
    assert _same_origin("https://a.example/mcp", "https://a.example/other")
    assert not _same_origin("https://a.example/mcp", "https://b.example/x")
    assert not _same_origin("https://a.example/mcp", "http://a.example/x")
    assert not _same_origin("https://a.example/mcp", "https://a.example:8443/x")


async def test_sse_cross_origin_endpoint_is_refused():
    server = _LegacySSEServer(endpoint="https://evil.example/messages")
    config = parse_server_config(
        "ssefake",
        {"transport": "sse", "url": "https://sse.example.invalid/sse"},
        environ={},
    )
    async with httpx.AsyncClient(transport=server) as http:
        client = MCPClient(config, backend="native", http_client=http)
        with pytest.raises(MCPTransportError):
            await client.connect()
        await client.aclose()
    # The credentials were never POSTed to the foreign origin.
    assert server.posts == []


async def test_sse_same_origin_absolute_endpoint_is_accepted():
    server = _LegacySSEServer(endpoint="https://sse.example.invalid/messages")
    config = parse_server_config(
        "ssefake",
        {"transport": "sse", "url": "https://sse.example.invalid/sse"},
        environ={},
    )
    async with httpx.AsyncClient(transport=server) as http:
        client = MCPClient(config, backend="native", http_client=http)
        await client.connect()
        try:
            assert client.server_info.name == "http-fake"
        finally:
            await client.aclose()


def test_auto_backend_prefers_native_for_list_changed():
    from nexus.mcp.client import _NativeSession

    client = MCPClient(stdio_config(), backend="auto")
    assert isinstance(client._build_session(), _NativeSession)


async def test_native_lists_resource_templates():
    client = await connect(stdio_config())
    try:
        templates = await client.list_resource_templates()
        assert templates[0].uri_template == "file:///{path}"
        assert templates[0].name == "file"
    finally:
        await client.aclose()


async def test_unsupported_template_listing_is_a_remote_error_not_a_crash():
    client = await connect(stdio_config(mode="no_templates"))
    try:
        with pytest.raises(MCPError):
            await client.list_resource_templates()
    finally:
        await client.aclose()


async def test_connect_after_close_is_refused():
    client = await connect(stdio_config())
    await client.aclose()
    with pytest.raises(MCPClosed):
        await client.connect()


async def test_streamable_http_redirect_is_refused():
    server = _StreamableServer(fail_status=302)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handler)) as http:
        client = MCPClient(http_config(), backend="native", http_client=http)
        with pytest.raises(MCPTransportError):
            await client.connect()
        await client.aclose()
