"""Phase 8b1 daemon: lifecycle, socket, handshake, auto-start, and streaming.

Two halves. The **in-process** half starts a real :class:`~nexus.host.daemon.Daemon`
around a real offline :class:`~nexus.runtime.Runtime` (a scripted provider, no
network) and pins the behaviours that do not need a second OS process: version
handshake, subscriptions, concurrent clients, a zero-view turn that outlives its
client, stale-socket reclaim, duplicate refusal, permissions, and idle shutdown.
The **subprocess** half launches a real daemon through :func:`ensure_daemon` with
a driver script, proving auto-start readiness, bounded failure, graceful
``SIGTERM``, and that a second daemon for the same workspace refuses to start.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
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

import nexus.host.daemon as daemon_module
from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.events import Event
from nexus.host import protocol as p
from nexus.host import transports as t
from nexus.host.daemon import (
    Daemon,
    DaemonError,
    default_http_path,
    default_socket_path,
    ensure_daemon,
    logs,
    read_http_endpoint,
    status,
    stop,
    workspace_hash,
)
from nexus.host.transports.http_sse import COMMAND_PATH, EVENTS_PATH, loopback_origins
from nexus.host.transports.uds import UDSClient
from nexus.model.http import SSEDecoder
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.util import new_id

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The driver auto-start launches. It builds a runtime with a scripted provider
#: whose per-call delay/text come from the environment, so one script serves
#: every timing-sensitive test without regenerating it.
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
    )
    return daemon.run()


if __name__ == "__main__":
    raise SystemExit(main())
'''


@pytest.fixture
def short_dir():
    path = Path(tempfile.mkdtemp(prefix="nexus-daemon-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def wait_for(predicate, timeout=5.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), timeout)


def _config() -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="ask", on_unattended="deny"),
            tools=ToolsSection(),
        ),
    )


def _factory(delay: float = 0.0, text: str = "ok"):
    def make(workspace, **kwargs):
        async def step(request):
            if delay:
                await asyncio.sleep(delay)
            return text_response(text)

        provider = ScriptedProvider(*([[step]] * 64))
        return Runtime(workspace, config=_config(), providers={"scripted": provider})

    return make


@contextlib.asynccontextmanager
async def running_daemon(
    short_dir: Path,
    *,
    idle_timeout: float = 300.0,
    delay: float = 0.0,
    text: str = "ok",
    max_concurrent: int = 4,
    http: bool = False,
    http_host: str = "127.0.0.1",
    http_port: int = 0,
    http_origins: list[str] | None = None,
    http_token: str | None = None,
):
    workspace = short_dir / f"ws-{new_id()[:8]}"
    workspace.mkdir()
    sock = short_dir / f"d-{new_id()[:8]}.sock"
    daemon = Daemon(
        workspace,
        socket_path=sock,
        idle_timeout=idle_timeout,
        max_concurrent_turns=max_concurrent,
        runtime_factory=_factory(delay, text),
        http=http,
        http_host=http_host,
        http_port=http_port,
        http_origins=http_origins,
        http_token=http_token,
    )
    task = asyncio.ensure_future(daemon.serve_forever())
    try:
        await wait_for(lambda: daemon.started or task.done())
        if task.done():
            task.result()
        if http:
            await wait_for(lambda: daemon.http_endpoint is not None or task.done())
            if task.done():
                task.result()
        yield daemon, sock, workspace
    finally:
        daemon.request_stop("test teardown")
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, 15)


# ---------------------------------------------------------------------------
# Raw HTTP/SSE helpers (real loopback sockets)
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


async def _http_send(port: int, raw: bytes, host: str = "127.0.0.1") -> bytes:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 3.0)
    try:
        writer.write(raw)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 3.0)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _http_command(
    port: int, command: p.Command, *, token: str, origin: str
) -> tuple[int, bytes]:
    raw = await _http_send(
        port,
        _request_bytes(
            "POST",
            COMMAND_PATH,
            headers={
                "Authorization": f"Bearer {token}",
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
    port: int,
    *,
    token: str,
    origin: str,
    session: str,
    headers: dict[str, str] | None = None,
    **params: Any,
):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    path = f"{EVENTS_PATH}?session={session}" + (f"&{query}" if query else "")
    request_headers = {
        "Origin": origin,
        "Authorization": f"Bearer {token}",
        "Connection": "keep-alive",
        **(headers or {}),
    }
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), 3.0
    )
    writer.write(_request_bytes("GET", path, headers=request_headers, connection="keep-alive"))
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3.0)
    status, response_headers = _parse_head(head)
    return reader, writer, status, response_headers


async def _http_close(reader, writer) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    del reader


async def _http_collect_sse(reader, *, stop=None, timeout: float = 5.0) -> list[Any]:
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


# ---------------------------------------------------------------------------
# In-process daemon
# ---------------------------------------------------------------------------


async def test_status_stop_and_logs(short_dir):
    async with running_daemon(short_dir) as (daemon, sock, workspace):
        report = await status(workspace, socket_path=sock)
        assert report["running"] is True
        assert report["pid"] == os.getpid()
        assert Path(report["workspace"]).resolve() == workspace.resolve()
        assert Path(report["socket"]).resolve() == sock.resolve()

        daemon._log("custom", token="sk-live-supersecret123456")
        text = logs(workspace, log_path=sock.with_suffix(".log"))
        assert "daemon.started" in text
        assert "sk-live-supersecret123456" not in text

    assert (await status(workspace, socket_path=sock))["running"] is False
    assert not sock.exists()


async def test_handshake_version_mismatch_fails_loudly(short_dir):
    async with running_daemon(short_dir) as (_daemon, sock, _workspace):
        with pytest.raises(t.VersionMismatch) as info:
            await UDSClient.connect(sock, version=p.PROTOCOL_VERSION + 1)
        assert info.value.expected == p.PROTOCOL_VERSION
        assert info.value.got == p.PROTOCOL_VERSION + 1


async def test_zero_view_turn_survives_client_disconnect(short_dir):
    async with running_daemon(short_dir, delay=0.4) as (_daemon, sock, _workspace):
        client = await UDSClient.connect(sock)
        await client.call(p.SessionOpen(session="s"))
        await client.call(p.SessionStart(session="s", content="long"))
        # No subscription was ever opened; the client now vanishes mid-turn.
        await client.close()

        await asyncio.sleep(1.2)
        late = await UDSClient.connect(sock)
        view = await late.call(p.SessionState(session="s"))
        assert isinstance(view, p.SessionStateResult)
        events = [
            event
            async for event in await late.subscribe("s", 0, follow=False)
        ]
        assert any(event.type == "turn.completed" for event in events)
        await late.close()


async def test_concurrent_clients_share_one_session(short_dir):
    async with running_daemon(short_dir, delay=0.1) as (_daemon, sock, _workspace):
        first = await UDSClient.connect(sock, client_id="view-a")
        second = await UDSClient.connect(sock, client_id="view-b")
        sub_a = await first.subscribe("s", 0, follow=True)
        sub_b = await second.subscribe("s", 0, follow=True)
        await first.call(p.SessionStart(session="s", content="go"))
        # Both views are attached, so presence sees two distinct clients.
        await wait_for(lambda: _daemon.facade.presence.total_viewers() == 2)

        async def collect(subscription):
            seen = []
            async for event in subscription:
                seen.append(event.type)
                if event.type == "turn.completed":
                    break
            return seen

        seen_a, seen_b = await asyncio.gather(collect(sub_a), collect(sub_b))
        assert "turn.completed" in seen_a and "turn.completed" in seen_b
        assert "text.delta" in seen_a and "text.delta" in seen_b
        await first.close()
        await second.close()


async def test_reconnect_replays_from_the_log(short_dir):
    async with running_daemon(short_dir) as (_daemon, sock, _workspace):
        client = await UDSClient.connect(sock)
        await client.call(p.SessionOpen(session="s"))
        subscription = await client.subscribe("s", 0, follow=True)
        await client.call(p.SessionStart(session="s", content="hello"))
        async for event in subscription:
            if event.type == "turn.completed":
                break
        await client.close()

        reconnected = await UDSClient.connect(sock)
        events = [
            event async for event in await reconnected.subscribe("s", 0, follow=False)
        ]
        kinds = [event.type for event in events]
        assert "turn.started" in kinds and "turn.completed" in kinds
        assert kinds.index("turn.started") < kinds.index("turn.completed")
        await reconnected.close()


async def test_stale_socket_is_reclaimed(short_dir):
    workspace = short_dir / "ws"
    workspace.mkdir()
    sock = short_dir / "stale.sock"
    sock.write_text("not a socket")
    sock.with_suffix(".pid").write_text('{"pid": 999999999, "workspace": "x"}')

    daemon = Daemon(workspace, socket_path=sock, runtime_factory=_factory())
    task = asyncio.ensure_future(daemon.serve_forever())
    try:
        await wait_for(lambda: daemon.started or task.done())
        if task.done():
            task.result()
        assert daemon.started
        assert sock.is_socket()
    finally:
        daemon.request_stop("done")
        await asyncio.wait_for(task, 15)


async def test_second_daemon_for_the_same_workspace_refuses(short_dir):
    async with running_daemon(short_dir) as (_daemon, sock, workspace):
        duplicate = Daemon(workspace, socket_path=sock, runtime_factory=_factory())
        with pytest.raises(DaemonError):
            await duplicate.start()
        await duplicate.aclose()


async def test_socket_and_directory_permissions(short_dir):
    async with running_daemon(short_dir) as (_daemon, sock, _workspace):
        assert sock.stat().st_mode & 0o777 == 0o600
        assert sock.parent.stat().st_mode & 0o777 == 0o700


async def test_secrets_are_redacted_from_error_results(short_dir):
    workspace = short_dir / "ws"
    workspace.mkdir()
    sock = short_dir / "secret.sock"

    def secret_factory(workspace_, **kwargs):
        class _SecretRuntime:
            sessions = SimpleNamespace()

            def session(self, session_id, **kwargs):
                from nexus.errors import SessionError

                raise SessionError("api_key=sk-live-supersecret123456")

        return _SecretRuntime()

    daemon = Daemon(workspace, socket_path=sock, runtime_factory=secret_factory)
    task = asyncio.ensure_future(daemon.serve_forever())
    try:
        await wait_for(lambda: daemon.started or task.done())
        if task.done():
            task.result()
        client = await UDSClient.connect(sock)
        result = await client.call(p.SessionOpen(session="s"))
        assert isinstance(result, p.ErrorResult)
        assert "sk-live-supersecret123456" not in result.message
        await client.close()
    finally:
        daemon.request_stop("done")
        await asyncio.wait_for(task, 15)


async def test_idle_shutdown_never_fires_with_a_turn_in_flight(short_dir):
    async with running_daemon(short_dir, idle_timeout=0.3, delay=0.8) as (
        daemon,
        sock,
        _workspace,
    ):
        client = await UDSClient.connect(sock)
        await client.call(p.SessionStart(session="s", content="work"))
        await client.close()
        # No viewer, but a turn is running, so the idle policy must hold off.
        await asyncio.sleep(0.5)
        assert daemon.facade.supervisor.running == 1
        assert not daemon.stopped
        await wait_for(lambda: daemon.facade.supervisor.running == 0, timeout=5.0)
        await wait_for(lambda: not sock.exists(), timeout=5.0)
        assert daemon.stopped


async def test_idle_shutdown_waits_for_the_last_viewer(short_dir):
    async with running_daemon(short_dir, idle_timeout=0.4) as (daemon, sock, _workspace):
        client = await UDSClient.connect(sock)
        subscription = await client.subscribe("s", 0, follow=True)
        await asyncio.sleep(0.8)
        assert not daemon.stopped
        await subscription.aclose()
        await client.close()
        await wait_for(lambda: not sock.exists(), timeout=5.0)
        assert daemon.stopped


async def test_idle_shutdown_completes_with_an_idle_connected_client(short_dir):
    """A connected-but-quiet client must not wedge idle shutdown.

    Regression: since Python 3.12's ``asyncio.Server.wait_closed`` does not
    return while an accepted connection is still open, so a daemon that closed
    its listener before tearing down live connections never reached
    ``daemon.stopped``. It kept the exclusive ``flock`` and removed its socket,
    so the next ``nexus chat`` auto-start saw exit code 3 (``another daemon
    already owns ...``) and failed with ``daemon exited with code 3 before
    readiness``.
    """
    async with running_daemon(short_dir, idle_timeout=0.4) as (daemon, sock, _workspace):
        # Connect, then stop feeding the subscription: the connection is idle yet
        # live, exactly like a shell parked on the prompt past its idle window.
        client = await UDSClient.connect(sock)
        subscription = await client.subscribe("s", 0, follow=True)
        await wait_for(lambda: daemon.client_count == 1, timeout=2.0)
        await asyncio.sleep(0.8)
        assert not daemon.stopped, "a follower should keep the daemon alive"
        # Stop viewing without closing the connection: no viewer remains, so the
        # idle policy fires, but the accepted socket is still open. ``aclose`` must
        # still return promptly -- not block on ``wait_closed`` behind the live
        # connection -- and release the socket so the next start can proceed.
        await subscription.aclose()
        await asyncio.wait_for(_wait_stopped(daemon, sock), timeout=3.0)
        assert daemon.stopped
        assert daemon._lock_fd is None, "the exclusive lock must be released"
        # A fresh daemon must be able to take the workspace over: this is the
        # exact observable failure of the regression (auto-start got exit 3).
        replacement = Daemon(
            daemon.workspace,
            socket_path=sock,
            idle_timeout=0.4,
            runtime_factory=_factory(),
        )
        await asyncio.wait_for(replacement.start(), timeout=3.0)
        try:
            assert replacement.started
        finally:
            await replacement.aclose()
        await client.close()


async def _wait_stopped(daemon: Daemon, sock: Path) -> None:
    # Socket removal is no longer a release proxy: since Python 3.13
    # ``asyncio.Server.close()`` unlinks the Unix socket path before the
    # daemon has finished shutting the facade down. Wait for the lock itself,
    # which is what the assertions below actually pin.
    while not (daemon.stopped and daemon._lock_fd is None and not sock.exists()):
        await asyncio.sleep(0.01)


async def test_shutdown_releases_lock_when_teardown_raises(short_dir, monkeypatch):
    """A raising per-connection teardown must not skip ``_release``.

    Regression: ``aclose`` awaited each connection in one unguarded loop, so a
    single ``_teardown`` exception escaped before ``_release`` and leaked the
    exclusive ``flock``.
    """
    async with running_daemon(short_dir) as (daemon, sock, _workspace):
        client = await UDSClient.connect(sock)
        await wait_for(lambda: daemon.client_count == 1, timeout=2.0)
        connection = next(iter(daemon._clients))

        async def boom():
            raise RuntimeError("teardown exploded")

        monkeypatch.setattr(connection, "_teardown", boom)
        await asyncio.wait_for(daemon.aclose(), timeout=2.0)
        assert daemon._lock_fd is None, "the exclusive lock must be released"
        assert not sock.exists()
        await client.close()


async def test_shutdown_bounds_a_stuck_writer_close(short_dir, monkeypatch):
    """A peer that never finishes closing must not pin shutdown.

    The per-connection teardown time-boxes ``writer.wait_closed``; a wedged
    transport is logged and abandoned so ``aclose`` still returns and releases
    the lock.
    """
    monkeypatch.setattr(daemon_module, "TEARDOWN_TIMEOUT", 0.05)
    async with running_daemon(short_dir) as (daemon, sock, _workspace):
        client = await UDSClient.connect(sock)
        await wait_for(lambda: daemon.client_count == 1, timeout=2.0)
        connection = next(iter(daemon._clients))

        async def never_closes():
            await asyncio.Event().wait()

        monkeypatch.setattr(connection.writer, "wait_closed", never_closes)
        await asyncio.wait_for(daemon.aclose(), timeout=1.0)
        assert daemon._lock_fd is None, "the exclusive lock must be released"
        assert not sock.exists()
        await client.close()


async def test_shutdown_cancellation_still_releases_lock(short_dir, monkeypatch):
    """Cancelling ``aclose`` mid-teardown must still release the lock."""
    async with running_daemon(short_dir) as (daemon, sock, _workspace):
        client = await UDSClient.connect(sock)
        await wait_for(lambda: daemon.client_count == 1, timeout=2.0)
        connection = next(iter(daemon._clients))
        entered = asyncio.Event()

        async def wedged():
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(connection, "_teardown", wedged)
        task = asyncio.ensure_future(daemon.aclose())
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        assert daemon._lock_fd is None, "the exclusive lock must be released"
        assert not sock.exists()
        await client.close()


async def test_late_handshake_after_shutdown_is_not_left_active(short_dir):
    """A handshake completing after ``aclose`` must not register a client.

    Regression: ``aclose`` snapshotted and cleared ``_clients``, so a
    connection finishing its handshake in that window could call
    ``_client_joined`` afterwards and be left active with the lock already
    released.
    """
    workspace = short_dir / "ws"
    workspace.mkdir()
    sock = short_dir / "late.sock"
    daemon = Daemon(workspace, socket_path=sock, runtime_factory=_factory())
    await daemon.start()
    reader, writer = await asyncio.open_unix_connection(str(sock))
    try:
        # Accepted but not yet handshaked: not a registered client.
        assert daemon.client_count == 0
        await daemon.aclose()
        assert daemon._lock_fd is None
        # Completing the handshake now must not register an active client.
        await t.write_frame(writer, t.Hello(version=p.PROTOCOL_VERSION))
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader.read(), timeout=2.0)
        assert daemon.client_count == 0
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


# ---------------------------------------------------------------------------
# Opt-in HTTP/SSE surface over the shared facade
# ---------------------------------------------------------------------------


async def test_http_is_off_by_default(short_dir):
    async with running_daemon(short_dir) as (daemon, sock, workspace):
        assert daemon.http_enabled is False
        assert daemon.http_endpoint is None
        assert daemon.http_port == 0
        assert not daemon.http_file.exists()
        assert read_http_endpoint(workspace, socket_path=sock) is None


async def test_http_binds_loopback_and_publishes_owner_only_discovery(short_dir):
    async with running_daemon(short_dir, http=True) as (daemon, sock, workspace):
        endpoint = daemon.http_endpoint
        assert endpoint is not None
        assert endpoint["host"] == "127.0.0.1"
        assert endpoint["port"] > 0
        assert len(endpoint["token"]) >= 32
        assert set(endpoint["origins"]) == set(loopback_origins(endpoint["port"]))

        # The discovery file is owner-only and reproducible from the socket path.
        assert daemon.http_file.stat().st_mode & 0o777 == 0o600
        discovered = read_http_endpoint(workspace, socket_path=sock)
        assert discovered is not None
        assert discovered["port"] == endpoint["port"]
        assert discovered["token"] == endpoint["token"]


async def test_http_token_never_reaches_logs_or_status(short_dir):
    async with running_daemon(short_dir, http=True) as (daemon, sock, workspace):
        token = daemon.http_endpoint["token"]
        daemon._log("probe", detail="x")
        text = logs(workspace, log_path=daemon.log_file)
        assert "daemon.http_started" in text
        assert token not in text

        report = await status(workspace, socket_path=sock)
        assert report["running"] is True
        assert token not in json.dumps(report)


async def test_http_refuses_a_non_loopback_host(short_dir):
    workspace = short_dir / "ws"
    workspace.mkdir()
    sock = short_dir / "public.sock"
    daemon = Daemon(
        workspace,
        socket_path=sock,
        runtime_factory=_factory(),
        http=True,
        http_host="0.0.0.0",
    )
    try:
        with pytest.raises(DaemonError):
            await daemon.start()
        assert not sock.exists()
    finally:
        await daemon.aclose()


async def test_http_origin_allowlist_and_auth_on_every_request(short_dir):
    async with running_daemon(
        short_dir, http=True, http_origins=["http://app.example"]
    ) as (daemon, _sock, _workspace):
        port = daemon.http_endpoint["port"]
        token = daemon.http_endpoint["token"]

        allowed = await _http_command(
            port, p.Health(), token=token, origin="http://app.example"
        )
        assert allowed[0] == 200
        loopback = await _http_command(
            port, p.Health(), token=token, origin=f"http://127.0.0.1:{port}"
        )
        assert loopback[0] == 403
        wrong_token = await _http_command(
            port, p.Health(), token="wrong-token-000000000000", origin="http://app.example"
        )
        assert wrong_token[0] == 401

        sse = await _http_open_sse(
            port, token=token, origin="http://evil.example", session="s"
        )
        assert sse[2] == 403
        await _http_close(sse[0], sse[1])


async def test_uds_and_http_views_share_one_live_session(short_dir):
    async with running_daemon(short_dir, delay=0.05, http=True) as (
        daemon,
        sock,
        _workspace,
    ):
        endpoint = daemon.http_endpoint
        port, token = endpoint["port"], endpoint["token"]
        origin = f"http://127.0.0.1:{port}"

        terminal = await UDSClient.connect(sock, client_id="terminal")
        await terminal.call(p.SessionOpen(session="s"))
        terminal_sub = await terminal.subscribe("s", 0, follow=True)
        reader, writer, status, _ = await _http_open_sse(
            port, token=token, origin=origin, session="s"
        )
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
        # Both surfaces attach to the same session and count as two viewers.
        await wait_for(lambda: daemon.facade.presence.total_viewers() == 2)

        # Start the turn over HTTP; the terminal view must see it too.
        started = await _http_command(
            port, p.SessionStart(session="s", content="go"), token=token, origin=origin
        )
        assert started[0] == 200
        terminal_seen, http_seen = await asyncio.gather(terminal_task, http_task)
        for seen in (terminal_seen, http_seen):
            assert "text.delta" in seen
            assert "turn.completed" in seen

        await _http_close(reader, writer)
        await terminal_sub.aclose()
        await terminal.close()


async def test_http_zero_view_turn_survives_and_late_sse_replays(short_dir):
    async with running_daemon(short_dir, delay=0.3, http=True) as (
        daemon,
        _sock,
        _workspace,
    ):
        endpoint = daemon.http_endpoint
        port, token = endpoint["port"], endpoint["token"]
        origin = f"http://127.0.0.1:{port}"

        opened = await _http_command(
            port, p.SessionOpen(session="s"), token=token, origin=origin
        )
        assert opened[0] == 200
        started = await _http_command(
            port, p.SessionStart(session="s", content="long"), token=token, origin=origin
        )
        assert started[0] == 200
        # No view is attached while the turn runs; it must still complete.
        assert daemon.facade.presence.total_viewers() == 0
        await wait_for(lambda: not daemon.facade.supervisor.running, timeout=10.0)

        reader, writer, status, _ = await _http_open_sse(
            port, token=token, origin=origin, session="s", follow="false"
        )
        assert status == 200
        events = await _http_collect_sse(reader, timeout=5.0)
        await _http_close(reader, writer)
        kinds = [event.type for event in events if isinstance(event, Event)]
        assert "turn.started" in kinds and "turn.completed" in kinds


async def test_http_sse_last_event_id_resumes_without_a_gap(short_dir):
    async with running_daemon(short_dir, delay=0.05, http=True) as (
        daemon,
        _sock,
        _workspace,
    ):
        endpoint = daemon.http_endpoint
        port, token = endpoint["port"], endpoint["token"]
        origin = f"http://127.0.0.1:{port}"

        await _http_command(port, p.SessionOpen(session="s"), token=token, origin=origin)
        await _http_command(
            port, p.SessionStart(session="s", content="go"), token=token, origin=origin
        )
        await wait_for(lambda: not daemon.facade.supervisor.running, timeout=10.0)

        reader, writer, status, _ = await _http_open_sse(
            port, token=token, origin=origin, session="s", follow="false"
        )
        assert status == 200
        first = await _http_collect_sse(reader, timeout=5.0)
        await _http_close(reader, writer)
        seqs = [event.seq for event in first if isinstance(event, Event)]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
        cursor = seqs[len(seqs) // 2]

        resumed, writer2, status2, _ = await _http_open_sse(
            port,
            token=token,
            origin=origin,
            session="s",
            follow="false",
            headers={"Last-Event-ID": str(cursor)},
        )
        assert status2 == 200
        rest = await _http_collect_sse(resumed, timeout=5.0)
        await _http_close(resumed, writer2)
        rest_seqs = [event.seq for event in rest if isinstance(event, Event)]
        assert rest_seqs and min(rest_seqs) > cursor


async def test_http_discovery_is_removed_on_stop_and_a_restart_mints_a_new_token(
    short_dir,
):
    workspace = short_dir / "ws"
    workspace.mkdir()
    sock = short_dir / "restart.sock"

    first = Daemon(workspace, socket_path=sock, runtime_factory=_factory(), http=True)
    first_task = asyncio.ensure_future(first.serve_forever())
    await wait_for(lambda: first.http_endpoint is not None or first_task.done())
    first_token = first.http_endpoint["token"]
    assert first.http_file.exists()

    first.request_stop("restart")
    await asyncio.wait_for(first_task, 15)
    assert not first.http_file.exists()
    assert not sock.exists()

    second = Daemon(workspace, socket_path=sock, runtime_factory=_factory(), http=True)
    second_task = asyncio.ensure_future(second.serve_forever())
    try:
        await wait_for(lambda: second.http_endpoint is not None or second_task.done())
        assert second.http_endpoint is not None
        assert second.http_endpoint["token"] != first_token
        assert second.http_file.exists()
    finally:
        second.request_stop("done")
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(second_task, 15)


# ---------------------------------------------------------------------------
# Subprocess daemon (real process, auto-start)
# ---------------------------------------------------------------------------


@pytest.fixture
def subprocess_env(short_dir):
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


async def test_autostart_spawns_a_daemon_and_serves_commands(subprocess_env):
    client = await ensure_daemon(
        subprocess_env.workspace,
        socket_path=subprocess_env.socket,
        spawn=subprocess_env.spawn,
        timeout=15.0,
        environ={"NEXUS_TEST_DELAY": "0.05", "NEXUS_TEST_TEXT": "pong"},
    )
    try:
        assert client.info is not None and client.info.pid > 0
        assert client.info.pid != os.getpid()
        await client.call(p.SessionOpen(session="s"))
        await client.call(p.SessionStart(session="s", content="hi"))
        subscription = await client.subscribe("s", 0, follow=True)
        kinds = []
        async for event in subscription:
            kinds.append(event.type)
            if event.type == "turn.completed":
                break
        assert "turn.completed" in kinds
    finally:
        await client.close()
    await stop(subprocess_env.workspace, socket_path=subprocess_env.socket)
    await wait_for(lambda: not subprocess_env.socket.exists(), timeout=5.0)


async def test_autostart_reclaims_a_stale_socket(subprocess_env):
    subprocess_env.socket.write_text("stale")
    client = await ensure_daemon(
        subprocess_env.workspace,
        socket_path=subprocess_env.socket,
        spawn=subprocess_env.spawn,
        timeout=15.0,
    )
    try:
        assert subprocess_env.socket.is_socket()
        assert client.info.pid > 0
    finally:
        await client.close()
    await stop(subprocess_env.workspace, socket_path=subprocess_env.socket)


async def test_autostart_version_mismatch_is_not_retried(subprocess_env):
    with pytest.raises(t.VersionMismatch):
        await ensure_daemon(
            subprocess_env.workspace,
            socket_path=subprocess_env.socket,
            spawn=subprocess_env.spawn,
            version=p.PROTOCOL_VERSION + 1,
            timeout=15.0,
        )
    # Exactly one spawn: a mismatch is terminal, never a retry loop.
    assert len(subprocess_env.processes) == 1


async def test_autostart_readiness_is_bounded(subprocess_env):
    def exits_immediately(*args, **kwargs):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        subprocess_env.processes.append(process)
        return process

    started = asyncio.get_running_loop().time()
    with pytest.raises(t.DaemonUnavailable):
        await ensure_daemon(
            subprocess_env.workspace,
            socket_path=subprocess_env.socket,
            spawn=exits_immediately,
            timeout=1.0,
        )
    assert asyncio.get_running_loop().time() - started < 5.0


async def test_second_subprocess_daemon_exits_nonzero(subprocess_env):
    client = await ensure_daemon(
        subprocess_env.workspace,
        socket_path=subprocess_env.socket,
        spawn=subprocess_env.spawn,
        timeout=15.0,
    )
    try:
        duplicate = await asyncio.create_subprocess_exec(
            sys.executable,
            str(subprocess_env.driver),
            "--workspace",
            str(subprocess_env.workspace),
            "--socket",
            str(subprocess_env.socket),
            cwd=str(subprocess_env.workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(duplicate.communicate(), timeout=15)
        assert duplicate.returncode != 0
        assert "already owns" in stderr.decode()
    finally:
        await client.close()
        await stop(subprocess_env.workspace, socket_path=subprocess_env.socket)


async def test_concurrent_cold_starts_share_one_daemon(subprocess_env):
    """Two racing auto-starts must converge on one daemon, never fight."""
    first, second = await asyncio.gather(
        ensure_daemon(
            subprocess_env.workspace,
            socket_path=subprocess_env.socket,
            spawn=subprocess_env.spawn,
            timeout=15.0,
        ),
        ensure_daemon(
            subprocess_env.workspace,
            socket_path=subprocess_env.socket,
            spawn=subprocess_env.spawn,
            timeout=15.0,
        ),
    )
    try:
        # Both clients reached a ready daemon; a loser that failed the lock was
        # tolerated because the winner became ready within the deadline.
        assert first.info is not None and second.info is not None
        assert first.info.pid > 0 and first.info.pid == second.info.pid
        assert subprocess_env.socket.is_socket()
    finally:
        await first.close()
        await second.close()
    await stop(subprocess_env.workspace, socket_path=subprocess_env.socket)


async def test_stop_does_not_signal_an_unrelated_pid(short_dir):
    workspace = short_dir / "ws"
    workspace.mkdir()
    sock = short_dir / "stale.sock"
    pid_file = sock.with_suffix(".pid")
    pid_file.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "workspace": str(workspace),
                "socket": str(sock),
            }
        ),
        encoding="utf-8",
    )
    # The pid is alive (this test process) but is not a Nexus daemon, so a
    # stop must refuse to signal it rather than risk killing an unrelated
    # process that reused the pid.
    assert await stop(workspace, socket_path=sock) is False
    assert pid_file.exists()


@pytest.mark.parametrize(
    "failure",
    [ConnectionResetError, t.DaemonUnavailable, t.TransportError],
)
async def test_stop_tolerates_disconnect_during_shutdown(tmp_path, monkeypatch, failure):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sock = tmp_path / "daemon.sock"
    state = {"connected": False, "command": None, "closed": False}

    class ConnectedClient:
        async def call(self, command, *, timeout):
            assert state["connected"]
            state["command"] = command
            raise failure("daemon closed during shutdown")

        async def close(self):
            state["closed"] = True

    client = ConnectedClient()

    async def connect(*args, **kwargs):
        state["connected"] = True
        return client

    monkeypatch.setattr(UDSClient, "connect", connect)

    result = await stop(workspace, socket_path=sock)
    assert state["connected"]
    assert isinstance(state["command"], p.Shutdown)
    assert state["closed"]
    assert result is True


async def test_sigterm_is_a_graceful_close(subprocess_env):
    client = await ensure_daemon(
        subprocess_env.workspace,
        socket_path=subprocess_env.socket,
        spawn=subprocess_env.spawn,
        timeout=15.0,
    )
    pid = client.info.pid
    await client.close()
    os.kill(pid, signal.SIGTERM)
    await wait_for(lambda: not subprocess_env.socket.exists(), timeout=10.0)
    log_path = subprocess_env.socket.with_suffix(".log")
    await wait_for(
        lambda: "daemon.stopped" in logs(subprocess_env.workspace, log_path=log_path),
        timeout=10.0,
    )
    text = logs(subprocess_env.workspace, log_path=log_path)
    assert "daemon.stopping" in text
    assert "daemon.stopped" in text


async def test_subprocess_idle_shutdown(short_dir):
    workspace = short_dir / "ws"
    workspace.mkdir()
    driver = short_dir / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    sock = short_dir / "idle.sock"
    spawned: list[subprocess.Popen] = []

    def spawn(workspace_, path, *, home=None, idle_timeout=None, **kwargs):
        process = subprocess.Popen(
            [
                sys.executable,
                str(driver),
                "--workspace",
                str(workspace_),
                "--socket",
                str(path),
                "--idle-timeout",
                "0.4",
            ],
            cwd=str(workspace_),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        spawned.append(process)
        return process

    try:
        client = await ensure_daemon(
            workspace, socket_path=sock, spawn=spawn, timeout=15.0
        )
        try:
            assert (await status(workspace, socket_path=sock))["running"] is True
        finally:
            await client.close()
        await wait_for(lambda: not sock.exists(), timeout=10.0)
        assert (await status(workspace, socket_path=sock))["running"] is False
    finally:
        for process in spawned:
            if process.poll() is None:
                process.terminate()
                with contextlib.suppress(Exception):
                    process.wait(timeout=5)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def test_socket_path_is_deterministic_and_per_workspace(tmp_path):
    # Keep this explicit home short so this test exercises the ordinary layout.
    home = Path("/tmp") / f"nx-{os.getpid()}-{tmp_path.name[-8:]}"
    first = default_socket_path(tmp_path / "a", home=home)
    second = default_socket_path(tmp_path / "a", home=home)
    other = default_socket_path(tmp_path / "b", home=home)
    assert first == second
    assert first != other
    assert first.parent == home / ".nexus" / "daemon"
    assert first.name == f"{workspace_hash(tmp_path / 'a')}.sock"


def test_socket_path_uses_nexus_home_environment(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    first_home = tmp_path / ("state-a-" + "a" * 100)
    second_home = tmp_path / ("state-b-" + "b" * 100)

    monkeypatch.setenv("NEXUS_HOME", str(first_home))
    first = default_socket_path(workspace)
    repeated = default_socket_path(workspace)
    monkeypatch.setenv("NEXUS_HOME", str(second_home))
    second = default_socket_path(workspace)

    assert first != second
    assert first == repeated
    assert first.parent != second.parent
    assert first.name == second.name == f"{workspace_hash(workspace)}.sock"
    assert len(str(first).encode("utf-8")) <= 100
    assert len(str(second).encode("utf-8")) <= 100
    assert first.parent.stat().st_mode & 0o777 == 0o700
    assert second.parent.stat().st_mode & 0o777 == 0o700


def test_socket_path_rejects_insecure_fallback_directory(tmp_path):
    home = tmp_path / ("state-" + "x" * 100)
    fallback = daemon_module._fallback_daemon_dir(home)
    fallback.mkdir(mode=0o755)

    with pytest.raises(DaemonError, match="mode 0700"):
        default_socket_path(tmp_path / "workspace", home=home)


def test_http_path_is_deterministic_and_colocated(tmp_path):
    home = tmp_path / "home"
    first = default_http_path(tmp_path / "a", home=home)
    assert first == default_http_path(tmp_path / "a", home=home)
    assert first != default_http_path(tmp_path / "b", home=home)
    assert first.parent == default_socket_path(tmp_path / "a", home=home).parent
    assert first.name == f"{workspace_hash(tmp_path / 'a')}.http"


def test_layering_daemon_does_not_import_a_ui():
    import ast

    root = REPO_ROOT / "nexus" / "host"
    for name in (
        "daemon.py",
        "transports/uds.py",
        "transports/http_sse.py",
        "transports/__init__.py",
    ):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            assert not any(mod.startswith(("nexus.ui", "nexus.cli")) for mod in names), name
