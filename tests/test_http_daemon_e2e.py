"""Phase 8c end-to-end: the opt-in HTTP/SSE surface on a real daemon process.

This is the criterion §14.15/11 evidence the ledger marked missing: a *terminal*
UDS view and an *HTTP* view are simultaneous subscribers to one live session
served by one daemon. The daemon is a real subprocess (scripted provider, no
network) launched through :func:`ensure_daemon`; the HTTP surface is discovered
from the owner-only ``.http`` file the daemon publishes beside its socket.

Covered: loopback-only bind, strong bearer token and strict ``Origin`` allowlist
over real HTTP, terminal + HTTP views sharing one session, a zero-view turn that
survives with no subscriber, ``Last-Event-ID`` resume against the authoritative
log, and a graceful ``SIGTERM`` that closes the surface, removes the token file,
and leaves a restart to mint a fresh token.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import msgspec
import pytest

from nexus.events import Event
from nexus.host import protocol as p
from nexus.host.daemon import ensure_daemon, logs, read_http_endpoint, stop
from nexus.host.transports.http_sse import COMMAND_PATH, EVENTS_PATH
from nexus.host.transports.uds import UDSClient
from nexus.model.http import SSEDecoder

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The daemon auto-start launches. It reads the HTTP switch from the environment
#: (``ensure_daemon(http=True)`` sets ``NEXUS_HTTP``), so one script serves every
#: test; the scripted provider's delay/text come from the environment too.
DRIVER = '''
import argparse
import asyncio
import os
from pathlib import Path

from nexus.config import Config
from nexus.config.schema import (
    AgentSection, ConfigV2, ModelSection, PermissionsSection, ToolsSection,
)
from nexus.host.daemon import Daemon
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime


def make_runtime(workspace, **kwargs):
    delay = float(os.environ.get("NEXUS_TEST_DELAY", "0"))
    text = os.environ.get("NEXUS_TEST_TEXT", "ok")

    async def step(request):
        if delay:
            await asyncio.sleep(delay)
        return text_response(text)

    provider = ScriptedProvider(*([[step]] * 64))
    config = Config(model="scripted/m", version=2, v2=ConfigV2(
        model=ModelSection(default="scripted/m"),
        agent=AgentSection(profile="coding"),
        permissions=PermissionsSection(mode="ask", on_unattended="deny"),
        tools=ToolsSection(),
    ))
    return Runtime(workspace, config=config, providers={"scripted": provider})


def _origins():
    raw = os.environ.get("NEXUS_HTTP_ORIGINS")
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--socket", default=None)
    parser.add_argument("--home", default=None)
    parser.add_argument("--idle-timeout", type=float, default=300.0)
    parser.add_argument("--max-concurrent-turns", type=int, default=4)
    args = parser.parse_args()
    daemon = Daemon(
        Path(args.workspace),
        home=args.home,
        socket_path=args.socket,
        idle_timeout=args.idle_timeout,
        max_concurrent_turns=args.max_concurrent_turns,
        runtime_factory=make_runtime,
        http=os.environ.get("NEXUS_HTTP") == "1",
        http_port=int(os.environ.get("NEXUS_HTTP_PORT", "0")),
        http_origins=_origins(),
    )
    return daemon.run()


if __name__ == "__main__":
    raise SystemExit(main())
'''


@pytest.fixture
def short_dir():
    path = Path(tempfile.mkdtemp(prefix="nexus-http-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def daemon_env(short_dir):
    workspace = short_dir / "ws"
    workspace.mkdir()
    driver = short_dir / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    processes: list[subprocess.Popen] = []

    def spawn(
        workspace_,
        path,
        *,
        home=None,
        idle_timeout=None,
        max_concurrent_turns=None,
        environ=None,
    ):
        argv = [
            sys.executable,
            str(driver),
            "--workspace",
            str(workspace_),
            "--socket",
            str(path),
        ]
        if idle_timeout is not None:
            argv += ["--idle-timeout", str(idle_timeout)]
        if max_concurrent_turns is not None:
            argv += ["--max-concurrent-turns", str(max_concurrent_turns)]
        process = subprocess.Popen(
            argv,
            cwd=str(workspace_),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **dict(environ or {})},
        )
        processes.append(process)
        return process

    yield SimpleNamespace(
        workspace=workspace,
        socket=short_dir / "daemon.sock",
        driver=driver,
        spawn=spawn,
        processes=processes,
    )

    for process in processes:
        if process.poll() is None:
            process.terminate()
            with contextlib.suppress(Exception):
                process.wait(timeout=5)


async def wait_for(predicate, timeout=10.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout)


async def _wait_turn_complete(client: UDSClient, session: str = "s", timeout=10.0) -> None:
    """Poll the authoritative view until a turn reaches a terminal phase."""

    async def done() -> bool:
        result = await client.call(p.SessionState(session=session))
        if not isinstance(result, p.SessionStateResult):
            return False
        return any(
            turn.get("phase") in ("completed", "failed", "cancelled")
            for turn in result.view.get("turns", [])
        )

    async def _wait():
        while not await done():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout)


# ---------------------------------------------------------------------------
# Raw HTTP/SSE helpers over real loopback sockets
# ---------------------------------------------------------------------------


def _request_bytes(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    connection: str = "close",
) -> bytes:
    lines = [f"{method} {path} HTTP/1.1"]
    out = {"Host": "127.0.0.1", "Connection": connection}
    if body:
        out["Content-Length"] = str(len(body))
    out.update(headers or {})
    lines.extend(f"{name}: {value}" for name, value in out.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def _parse_head(head: bytes) -> tuple[int, dict[str, str]]:
    lines = head[:-4].split(b"\r\n")
    status = int(lines[0].split(b" ", 2)[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(b":")
        headers[name.strip().lower().decode("latin-1")] = value.strip().decode("latin-1")
    return status, headers


async def _http_send(port: int, raw: bytes) -> bytes:
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 3.0)
    try:
        writer.write(raw)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 3.0)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _http_command(
    endpoint: dict[str, Any], command: p.Command, *, origin: str, token: str | None = None
) -> tuple[int, bytes]:
    presented = endpoint["token"] if token is None else token
    raw = await _http_send(
        endpoint["port"],
        _request_bytes(
            "POST",
            COMMAND_PATH,
            headers={
                "Authorization": f"Bearer {presented}",
                "Origin": origin,
                "Content-Type": "application/json",
            },
            body=p.encode_command(command),
        ),
    )
    head, _, body = raw.partition(b"\r\n\r\n")
    status, _ = _parse_head(head + b"\r\n\r\n")
    return status, body


async def _http_open_sse(
    endpoint: dict[str, Any],
    *,
    origin: str,
    session: str = "s",
    token: str | None = None,
    headers: dict[str, str] | None = None,
    **params: Any,
):
    presented = endpoint["token"] if token is None else token
    query = "&".join(f"{key}={value}" for key, value in params.items())
    path = f"{EVENTS_PATH}?session={session}" + (f"&{query}" if query else "")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", endpoint["port"]), 3.0
    )
    writer.write(
        _request_bytes(
            "GET",
            path,
            headers={
                "Origin": origin,
                "Authorization": f"Bearer {presented}",
                "Connection": "keep-alive",
                **(headers or {}),
            },
            connection="keep-alive",
        )
    )
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3.0)
    status, response_headers = _parse_head(head)
    return reader, writer, status, response_headers


async def _http_close(reader, writer) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    del reader


async def _http_collect_sse(reader, *, stop=None, timeout: float = 10.0) -> list[Any]:
    decoder = SSEDecoder()
    out: list[Any] = []
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(65536), timeout)
        except TimeoutError:
            break
        if not chunk:
            break
        for frame in decoder.feed_bytes(chunk):
            decoded: Any = frame
            if frame.data:
                try:
                    decoded = msgspec.json.decode(frame.data, type=Event)
                except msgspec.DecodeError:
                    decoded = frame
            out.append(decoded)
            if stop is not None and stop(decoded):
                return out
    return out


def _origin(endpoint: dict[str, Any]) -> str:
    return f"http://127.0.0.1:{endpoint['port']}"


async def _open_http_daemon(daemon_env, **environ) -> tuple[UDSClient, dict[str, Any]]:
    client = await ensure_daemon(
        daemon_env.workspace,
        socket_path=daemon_env.socket,
        spawn=daemon_env.spawn,
        timeout=15.0,
        http=True,
        environ={"NEXUS_TEST_DELAY": "0.05", "NEXUS_TEST_TEXT": "pong", **environ},
    )
    await wait_for(
        lambda: read_http_endpoint(
            daemon_env.workspace, socket_path=daemon_env.socket
        )
        is not None
    )
    endpoint = read_http_endpoint(daemon_env.workspace, socket_path=daemon_env.socket)
    assert endpoint is not None
    return client, endpoint


# ---------------------------------------------------------------------------
# Shared session: terminal + HTTP
# ---------------------------------------------------------------------------


async def test_terminal_uds_and_http_views_share_one_session(daemon_env):
    client, endpoint = await _open_http_daemon(daemon_env)
    origin = _origin(endpoint)
    try:
        await client.call(p.SessionOpen(session="s"))
        terminal_sub = await client.subscribe("s", 0, follow=True)
        reader, writer, status, _ = await _http_open_sse(endpoint, origin=origin)
        assert status == 200

        async def collect_terminal():
            seen = []
            async for event in terminal_sub:
                seen.append(event.type)
                if event.type == "turn.completed":
                    return seen
            return seen

        async def collect_http():
            events = await _http_collect_sse(
                reader,
                stop=lambda event: isinstance(event, Event)
                and event.type == "turn.completed",
            )
            return [event.type for event in events if isinstance(event, Event)]

        terminal_task = asyncio.ensure_future(collect_terminal())
        http_task = asyncio.ensure_future(collect_http())
        # Start the turn over HTTP; both views (each replaying from seq 0 and
        # following) must observe it, proving one session, two transports.
        started = await _http_command(
            endpoint, p.SessionStart(session="s", content="go"), origin=origin
        )
        assert started[0] == 200
        terminal_seen, http_seen = await asyncio.gather(terminal_task, http_task)
        for seen in (terminal_seen, http_seen):
            assert "text.delta" in seen
            assert "turn.completed" in seen

        await _http_close(reader, writer)
        await terminal_sub.aclose()
    finally:
        await client.close()
        await stop(daemon_env.workspace, socket_path=daemon_env.socket)


# ---------------------------------------------------------------------------
# Auth and Origin
# ---------------------------------------------------------------------------


async def test_http_auth_and_origin_are_enforced(daemon_env):
    client, endpoint = await _open_http_daemon(daemon_env)
    origin = _origin(endpoint)
    try:
        ok = await _http_command(endpoint, p.Health(), origin=origin)
        assert ok[0] == 200

        wrong = await _http_command(
            endpoint, p.Health(), origin=origin, token="wrong-token-000000000000"
        )
        assert wrong[0] == 401

        foreign = await _http_command(endpoint, p.Health(), origin="http://evil.example")
        assert foreign[0] == 403

        sse = await _http_open_sse(endpoint, origin="http://evil.example")
        assert sse[2] == 403
        await _http_close(sse[0], sse[1])

        # A strong token is generated and never echoed.
        assert len(endpoint["token"]) >= 32
        assert endpoint["token"] not in wrong[1].decode("latin-1")
    finally:
        await client.close()
        await stop(daemon_env.workspace, socket_path=daemon_env.socket)


# ---------------------------------------------------------------------------
# Zero-view turn and SSE resume
# ---------------------------------------------------------------------------


async def test_zero_view_http_turn_is_replayed_to_a_late_sse_view(daemon_env):
    client, endpoint = await _open_http_daemon(daemon_env)
    origin = _origin(endpoint)
    try:
        await _http_command(endpoint, p.SessionOpen(session="s"), origin=origin)
        started = await _http_command(
            endpoint, p.SessionStart(session="s", content="long"), origin=origin
        )
        assert started[0] == 200
        # Nothing is attached while the turn runs; a late SSE view still gets it.
        await _wait_turn_complete(client)
        reader, writer, status, _ = await _http_open_sse(
            endpoint, origin=origin, follow="false"
        )
        assert status == 200
        events = await _http_collect_sse(
            reader,
            stop=lambda event: isinstance(event, Event)
            and event.type == "turn.completed",
        )
        await _http_close(reader, writer)
        kinds = [event.type for event in events if isinstance(event, Event)]
        assert "turn.started" in kinds and "turn.completed" in kinds
    finally:
        await client.close()
        await stop(daemon_env.workspace, socket_path=daemon_env.socket)


async def test_sse_last_event_id_resumes_without_a_gap(daemon_env):
    client, endpoint = await _open_http_daemon(daemon_env)
    origin = _origin(endpoint)
    try:
        await _http_command(endpoint, p.SessionOpen(session="s"), origin=origin)
        await _http_command(
            endpoint, p.SessionStart(session="s", content="go"), origin=origin
        )
        await _wait_turn_complete(client)

        reader, writer, status, _ = await _http_open_sse(
            endpoint, origin=origin, follow="false"
        )
        assert status == 200
        first = await _http_collect_sse(reader)
        await _http_close(reader, writer)
        seqs = [event.seq for event in first if isinstance(event, Event)]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
        cursor = seqs[len(seqs) // 2]

        resumed, writer2, status2, _ = await _http_open_sse(
            endpoint,
            origin=origin,
            follow="false",
            headers={"Last-Event-ID": str(cursor)},
        )
        assert status2 == 200
        rest = await _http_collect_sse(resumed)
        await _http_close(resumed, writer2)
        rest_seqs = [event.seq for event in rest if isinstance(event, Event)]
        assert rest_seqs and min(rest_seqs) > cursor
    finally:
        await client.close()
        await stop(daemon_env.workspace, socket_path=daemon_env.socket)


# ---------------------------------------------------------------------------
# Graceful lifecycle and restart
# ---------------------------------------------------------------------------


async def test_sigterm_closes_http_and_removes_the_token_file(daemon_env):
    client, _endpoint = await _open_http_daemon(daemon_env)
    pid = client.info.pid
    token_file = daemon_env.socket.with_name(daemon_env.socket.stem + ".http")
    assert token_file.exists()
    await client.close()

    os.kill(pid, signal.SIGTERM)
    await wait_for(lambda: not daemon_env.socket.exists(), timeout=10.0)
    assert not token_file.exists()
    assert read_http_endpoint(
        daemon_env.workspace, socket_path=daemon_env.socket
    ) is None
    text = logs(daemon_env.workspace, log_path=daemon_env.socket.with_suffix(".log"))
    assert "daemon.stopping" in text and "daemon.stopped" in text


async def test_restart_mints_a_fresh_token_and_keeps_the_session(daemon_env):
    client, endpoint = await _open_http_daemon(daemon_env)
    await client.call(p.SessionOpen(session="s"))
    await client.call(p.SessionStart(session="s", content="Persist this session"))
    first_token = endpoint["token"]
    await client.close()
    await stop(daemon_env.workspace, socket_path=daemon_env.socket)
    await wait_for(lambda: not daemon_env.socket.exists(), timeout=10.0)

    second, endpoint2 = await _open_http_daemon(daemon_env)
    try:
        assert endpoint2["token"] != first_token
        # The session log is on disk, so the restarted daemon still knows it.
        view = await second.call(p.SessionState(session="s"))
        assert isinstance(view, p.SessionStateResult)
        assert view.session == "s"
    finally:
        await second.close()
        await stop(daemon_env.workspace, socket_path=daemon_env.socket)
