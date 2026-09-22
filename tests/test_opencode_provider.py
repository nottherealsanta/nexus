"""Offline tests for the OpenCode ACP subprocess adapter.

Everything runs against a fake ``opencode acp`` server fixture (a real
subprocess, real stdio, real process groups) with no network and no model. The
fake is a documented-surface stand-in: it speaks newline-delimited JSON-RPC 2.0
and implements ``initialize`` / ``session/new`` / ``session/prompt`` /
``session/close``.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from nexus.errors import ConfigError, ProviderError
from nexus.model.message import (
    Document,
    Image,
    Message,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)
from nexus.model.provider import Provider
from nexus.model.providers.opencode import (
    OpenCodeAuthRequired,
    OpenCodeProvider,
    SubprocessAgentProvider,
    build_child_env,
    render_prompt,
)
from nexus.model.request import ModelRequest, ToolSchema
from nexus.model.stream import (
    MessageStart,
    MessageStop,
    Raw,
    TextDelta,
    ThinkingDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

FIXTURES = Path(__file__).parent / "fixtures" / "opencode"
FAKE_AGENT = FIXTURES / "fake_acp_agent.py"

SECRET = "sk-ant-CONFORMANCE-SECRET-0123456789abcdef"


def _command(*extra: str) -> list[str]:
    return [sys.executable, str(FAKE_AGENT), *extra]


def _request() -> ModelRequest:
    return ModelRequest(messages=[Message("user", [Text("hi")])])


async def _read_trace(path: Path) -> dict:
    for _ in range(300):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                pass
        await asyncio.sleep(0.01)
    raise AssertionError(f"trace was never written: {path}")


async def _assert_dead(pid: int) -> None:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"process {pid} survived adapter teardown")


# ---------------------------------------------------------------------------
# Protocol shape and declared limitations
# ---------------------------------------------------------------------------


def test_is_a_provider_with_declared_limitations(tmp_path):
    provider = OpenCodeProvider(workspace=tmp_path)
    assert isinstance(provider, Provider)
    assert isinstance(provider, SubprocessAgentProvider)
    assert provider.name == "opencode"
    caps = provider.capabilities("anything")
    assert caps.streaming is True
    assert caps.tools is False
    assert caps.parallel_tool_calls is False
    assert caps.thinking is True
    assert caps.degradation == {
        "thinking": "drop",
        "vision": "to_text",
        "documents": "to_text",
    }
    limitations = provider.describe_limitations()
    assert limitations
    assert any("Nexus tools" in item for item in limitations)


def test_default_command_is_the_documented_acp_argv(tmp_path):
    provider = OpenCodeProvider(workspace=tmp_path)
    argv = provider.server_argv()
    assert argv[0] == "opencode"
    assert argv[1] == "acp"
    assert argv[-2:] == ["--cwd", str(tmp_path)]


def test_command_override_skips_injected_cwd(tmp_path):
    provider = OpenCodeProvider(command=["/opt/bin/opencode", "acp"], workspace=tmp_path)
    assert provider.server_argv() == ["/opt/bin/opencode", "acp"]


def test_unknown_permission_policy_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        OpenCodeProvider(workspace=tmp_path, permission_policy="maybe")


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_render_prompt_metadata_override():
    request = ModelRequest(messages=[], metadata={"prompt": "raw prompt"})
    assert render_prompt(request) == "raw prompt"


def test_render_prompt_flattens_history_and_marks_tools_uncallable():
    request = ModelRequest(
        messages=[
            Message("user", [Text("first")]),
            Message("assistant", [Text("reply")]),
            Message("user", [Text("latest")]),
        ],
        system="sys",
        tools=[ToolSchema(name="Read", description="r", input_schema={"type": "object"})],
    )
    text = render_prompt(request)
    assert "sys" in text
    assert "user: first" in text
    assert "assistant: reply" in text
    assert text.rstrip().endswith("latest")
    assert "not callable here: Read" in text


def test_render_prompt_degrades_non_text_blocks_deterministically():
    request = ModelRequest(
        messages=[
            Message(
                "assistant",
                [
                    Thinking("secret reasoning", signature="sig"),
                    ToolUse("c1", "Read", {"path": "a.txt", "z": 1}),
                    ToolResult("c1", [Text("contents")], is_error=True),
                    Image("image/png", data=b"xx"),
                    Document("application/pdf", b"%PDF", title="notes"),
                ],
            ),
            Message("user", [Text("go")]),
        ]
    )
    text = render_prompt(request)
    assert "secret reasoning" not in text
    assert '[tool_use Read {"path": "a.txt", "z": 1}]' in text
    assert "[tool_result_error contents]" in text
    assert "[image data:image/png]" in text
    assert "[document application/pdf notes]" in text


# ---------------------------------------------------------------------------
# End-to-end stream normalization
# ---------------------------------------------------------------------------


async def test_stream_normalizes_text_usage_and_stop(tmp_path):
    provider = OpenCodeProvider(
        command=_command("--scenario", "text"), workspace=tmp_path, model="m"
    )
    try:
        events = [event async for event in provider.stream(_request())]
    finally:
        await provider.aclose()
    assert isinstance(events[0], MessageStart)
    assert events[0].provider == "opencode"
    assert events[0].model == "m"
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "hello world"
    usage = next(e for e in events if isinstance(e, Usage))
    assert (usage.input, usage.output) == (5, 7)
    assert isinstance(events[-1], MessageStop)
    assert events[-1].stop_reason == "end_turn"


async def test_request_is_sent_as_one_text_prompt(tmp_path):
    prompt_trace = tmp_path / "prompts.ndjson"
    provider = OpenCodeProvider(
        command=_command("--scenario", "text", "--prompt-trace", str(prompt_trace)),
        workspace=tmp_path,
    )
    request = ModelRequest(
        messages=[
            Message("user", [Text("first")]),
            Message("assistant", [Text("reply")]),
            Message("user", [Text("latest")]),
        ],
        system="sys",
        tools=[ToolSchema(name="Read", description="r", input_schema={"type": "object"})],
    )
    try:
        [event async for event in provider.stream(request)]
    finally:
        await provider.aclose()
    params = json.loads(prompt_trace.read_text().splitlines()[0])
    assert params["sessionId"] == "sess_fake"
    assert len(params["prompt"]) == 1
    assert params["prompt"][0]["type"] == "text"
    rendered = params["prompt"][0]["text"]
    assert "sys" in rendered
    assert "latest" in rendered
    assert "not callable here: Read" in rendered


async def test_thinking_is_streamed(tmp_path):
    provider = OpenCodeProvider(
        command=_command("--scenario", "thinking"), workspace=tmp_path
    )
    try:
        events = [event async for event in provider.stream(_request())]
    finally:
        await provider.aclose()
    assert any(isinstance(e, ThinkingDelta) and e.text == "pondering" for e in events)


async def test_agent_tools_are_never_claimed_as_nexus_tool_calls(tmp_path):
    provider = OpenCodeProvider(
        command=_command("--scenario", "tools"), workspace=tmp_path
    )
    try:
        events = [event async for event in provider.stream(_request())]
    finally:
        await provider.aclose()
    assert not any(isinstance(e, (ToolCallStart, ToolCallEnd)) for e in events)
    raw = [e for e in events if isinstance(e, Raw)]
    assert any(e.data["sessionUpdate"] == "tool_call" for e in raw)


# ---------------------------------------------------------------------------
# Permission requests
# ---------------------------------------------------------------------------


async def test_permission_denied_by_default(tmp_path):
    provider = OpenCodeProvider(
        command=_command("--scenario", "permission"), workspace=tmp_path
    )
    try:
        events = [event async for event in provider.stream(_request())]
    finally:
        await provider.aclose()
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert '"optionId": "reject"' in text


async def test_permission_allow_policy(tmp_path):
    provider = OpenCodeProvider(
        command=_command("--scenario", "permission"),
        workspace=tmp_path,
        permission_policy="allow",
    )
    try:
        events = [event async for event in provider.stream(_request())]
    finally:
        await provider.aclose()
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert '"optionId": "allow"' in text


# ---------------------------------------------------------------------------
# Credentials: explicit allowlist only, never the agent's store
# ---------------------------------------------------------------------------


def test_build_child_env_copies_only_the_allowlist():
    source = {"PATH": "/bin", "HOME": "/home/x", "SECRET_TOKEN": SECRET, "OTHER": "y"}
    child = build_child_env(source)
    assert child["PATH"] == "/bin"
    assert "SECRET_TOKEN" not in child
    assert "OTHER" not in child
    allowlisted = build_child_env(source, inherit_env=("SECRET_TOKEN",))
    assert allowlisted["SECRET_TOKEN"] == SECRET


async def test_credentials_are_not_inherited_without_an_allowlist(tmp_path):
    trace = tmp_path / "trace.json"
    provider = OpenCodeProvider(
        command=_command("--scenario", "text", "--trace", str(trace)),
        workspace=tmp_path,
        environ={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "ANTHROPIC_API_KEY": SECRET},
    )
    try:
        [event async for event in provider.stream(_request())]
    finally:
        await provider.aclose()
    recorded = await _read_trace(trace)
    assert "ANTHROPIC_API_KEY" not in recorded["env"]


async def test_credentials_are_inherited_when_allowlisted(tmp_path):
    trace = tmp_path / "trace.json"
    provider = OpenCodeProvider(
        command=_command("--scenario", "text", "--trace", str(trace)),
        workspace=tmp_path,
        environ={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "ANTHROPIC_API_KEY": SECRET},
        inherit_env=("ANTHROPIC_API_KEY",),
    )
    try:
        [event async for event in provider.stream(_request())]
    finally:
        await provider.aclose()
    recorded = await _read_trace(trace)
    assert recorded["env"]["ANTHROPIC_API_KEY"] == SECRET


def test_module_never_reads_the_credential_store():
    import nexus.model.providers.opencode as module

    # The adapter must not touch OpenCode's on-disk credential store: no file is
    # opened, and the only mention of it is the documented promise below.
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "open(" not in source
    assert "read_text" not in source
    assert "read_bytes" not in source
    assert source.count("auth.json") == 1


# ---------------------------------------------------------------------------
# Failures: actionable, redacted, and terminating
# ---------------------------------------------------------------------------


async def test_missing_binary_is_actionable(tmp_path):
    missing = tmp_path / "no-such-opencode"
    provider = OpenCodeProvider(command=[str(missing)], workspace=tmp_path)
    with pytest.raises(ProviderError) as excinfo:
        [event async for event in provider.stream(_request())]
    message = str(excinfo.value)
    assert "no-such-opencode" in message
    assert "PATH" in message


async def test_auth_required_is_actionable(tmp_path):
    provider = OpenCodeProvider(
        command=_command("--scenario", "auth"), workspace=tmp_path
    )
    with pytest.raises(OpenCodeAuthRequired) as excinfo:
        [event async for event in provider.stream(_request())]
    message = str(excinfo.value)
    assert "opencode auth login" in message
    assert "auth.json" not in message


async def test_malformed_protocol_line_fails(tmp_path):
    provider = OpenCodeProvider(
        command=_command("--scenario", "malformed"), workspace=tmp_path
    )
    with pytest.raises(ProviderError) as excinfo:
        [event async for event in provider.stream(_request())]
    assert "malformed ACP message" in str(excinfo.value)


async def test_stderr_is_redacted_on_crash(tmp_path):
    provider = OpenCodeProvider(
        command=_command("--scenario", "crash"), workspace=tmp_path
    )
    with pytest.raises(ProviderError) as excinfo:
        [event async for event in provider.stream(_request())]
    message = str(excinfo.value)
    assert SECRET not in message
    assert "boom" in message


async def test_timeout_terminates_the_agent(tmp_path):
    trace = tmp_path / "trace.json"
    provider = OpenCodeProvider(
        command=_command("--scenario", "park", "--trace", str(trace)),
        workspace=tmp_path,
        timeout_seconds=0.3,
    )
    with pytest.raises(ProviderError) as excinfo:
        [event async for event in provider.stream(_request())]
    assert "exceeded" in str(excinfo.value)
    recorded = await _read_trace(trace)
    await _assert_dead(recorded["pid"])


async def test_early_stream_close_kills_the_process_group(tmp_path):
    trace = tmp_path / "trace.json"
    provider = OpenCodeProvider(
        command=_command("--scenario", "park", "--trace", str(trace)),
        workspace=tmp_path,
    )
    stream = provider.stream(_request())
    first = await asyncio.wait_for(anext(stream), timeout=5)
    assert isinstance(first, MessageStart)
    # Consume until the agent has written its trace and parked, then close early.
    while True:
        event = await asyncio.wait_for(anext(stream), timeout=5)
        if isinstance(event, TextDelta):
            break
    await asyncio.wait_for(stream.aclose(), timeout=5)
    recorded = await _read_trace(trace)
    await _assert_dead(recorded["pid"])


async def test_provider_close_terminates_an_active_stream(tmp_path):
    trace = tmp_path / "trace.json"
    provider = OpenCodeProvider(
        command=_command("--scenario", "park", "--trace", str(trace)),
        workspace=tmp_path,
    )
    stream = provider.stream(_request())
    assert isinstance(await asyncio.wait_for(anext(stream), timeout=5), MessageStart)
    while not isinstance(await asyncio.wait_for(anext(stream), timeout=5), TextDelta):
        pass
    await asyncio.wait_for(provider.aclose(), timeout=5)
    recorded = await _read_trace(trace)
    await _assert_dead(recorded["pid"])
    await asyncio.wait_for(stream.aclose(), timeout=5)


async def test_close_is_idempotent_and_blocks_streaming(tmp_path):
    provider = OpenCodeProvider(
        command=_command("--scenario", "text"), workspace=tmp_path
    )
    assert await provider.count_tokens(_request()) is None
    await provider.aclose()
    await provider.aclose()
    with pytest.raises(ProviderError):
        [event async for event in provider.stream(_request())]


# ---------------------------------------------------------------------------
# ACP protocol negotiation, close, and cancellation robustness
# ---------------------------------------------------------------------------


def _read_methods(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        json.loads(line)["method"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def test_protocol_version_mismatch_is_rejected(tmp_path):
    from nexus.model.providers.opencode import OpenCodeError

    provider = OpenCodeProvider(
        command=_command("--scenario", "badversion"), workspace=tmp_path
    )
    with pytest.raises(OpenCodeError, match="protocolVersion"):
        [event async for event in provider.stream(_request())]
    await provider.aclose()


async def test_unknown_stop_reason_normalizes_to_error(tmp_path):
    provider = OpenCodeProvider(
        command=_command("--scenario", "unknown_stop"), workspace=tmp_path
    )
    try:
        events = [event async for event in provider.stream(_request())]
    finally:
        await provider.aclose()
    stops = [e for e in events if isinstance(e, MessageStop)]
    assert stops and stops[-1].stop_reason == "error"


async def test_session_close_failure_does_not_fail_a_successful_turn(tmp_path):
    methods = tmp_path / "methods.ndjson"
    provider = OpenCodeProvider(
        command=_command(
            "--scenario", "close_error", "--method-trace", str(methods)
        ),
        workspace=tmp_path,
    )
    try:
        events = [event async for event in provider.stream(_request())]
    finally:
        await provider.aclose()
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "hello world"
    assert events[-1].stop_reason == "end_turn"
    assert "session/close" in _read_methods(methods)


async def test_slow_close_is_bounded_and_keeps_success(tmp_path, monkeypatch):
    import time

    import nexus.model.providers.opencode as module

    monkeypatch.setattr(module, "_CLOSE_TIMEOUT_SECONDS", 0.2)
    provider = OpenCodeProvider(
        command=_command("--scenario", "close_hang"), workspace=tmp_path
    )
    start = time.monotonic()
    try:
        events = [event async for event in provider.stream(_request())]
    finally:
        await provider.aclose()
    elapsed = time.monotonic() - start
    assert events[-1].stop_reason == "end_turn"
    assert elapsed < 3.0


async def test_close_is_not_requested_without_the_capability(tmp_path):
    methods = tmp_path / "methods.ndjson"
    provider = OpenCodeProvider(
        command=_command("--scenario", "nocap", "--method-trace", str(methods)),
        workspace=tmp_path,
    )
    try:
        [event async for event in provider.stream(_request())]
    finally:
        await provider.aclose()
    recorded = _read_methods(methods)
    assert "session/prompt" in recorded
    assert "session/close" not in recorded


async def test_cancellation_sends_session_cancel_then_kills(tmp_path):
    trace = tmp_path / "trace.json"
    methods = tmp_path / "methods.ndjson"
    provider = OpenCodeProvider(
        command=_command(
            "--scenario",
            "park",
            "--trace",
            str(trace),
            "--method-trace",
            str(methods),
        ),
        workspace=tmp_path,
    )
    events: list = []

    async def consume() -> None:
        async for event in provider.stream(_request()):
            events.append(event)

    task = asyncio.create_task(consume())
    try:
        for _ in range(500):
            if any(isinstance(e, TextDelta) for e in events):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await provider.aclose()
    recorded = await _read_trace(trace)
    await _assert_dead(recorded["pid"])
    assert "session/cancel" in _read_methods(methods)


async def test_close_start_race_guard_refuses_a_late_process(tmp_path):
    provider = OpenCodeProvider(
        command=_command("--scenario", "text"), workspace=tmp_path
    )
    await provider.aclose()

    class _Dummy:
        async def aclose(self) -> None:
            return None

    assert await provider._register(_Dummy()) is False


async def test_pending_future_is_cleaned_up_when_send_fails():
    from nexus.model.providers.opencode import OpenCodeError, _JsonRpcProcess

    process = _JsonRpcProcess(
        command=["true"], env={}, cwd=None, request_handler=None
    )

    async def boom(message) -> None:
        raise OpenCodeError("send failed")

    process._send = boom  # type: ignore[method-assign]
    with pytest.raises(OpenCodeError):
        await process.request("initialize", {})
    assert process._pending == {}

