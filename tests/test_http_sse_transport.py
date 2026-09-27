"""Phase 8c1 HTTP/SSE transport: real sockets, auth, origin, bounds, streaming.

The server is driven over real loopback sockets with raw HTTP/1.1 bytes, so the
tests pin the wire itself rather than a helper: the request parser and its
bounds, the bearer token compared in constant time, the strict ``Origin``
allowlist, CORS without credentials, the JSON command endpoint, and the SSE
stream including ``Last-Event-ID`` resume, keep-alive, event parity with the
session log, and the drop of a slow consumer without stalling its producer.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
from pathlib import Path
from typing import Any

import msgspec
import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.events import Event
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.host.transports.http_sse import (
    COMMAND_PATH,
    EVENTS_PATH,
    HTTPSSEServer,
    loopback_origins,
)
from nexus.model.http import SSEDecoder
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.model.stream import MessageStart, MessageStop, TextDelta
from nexus.runtime import Runtime
from nexus.session.manager import SessionSummary

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSPORT = REPO_ROOT / "nexus" / "host" / "transports" / "http_sse.py"
TOKEN = "test-token-0123456789abcdef"


# ---------------------------------------------------------------------------
# Raw HTTP helpers (real sockets)
# ---------------------------------------------------------------------------


async def wait_for(predicate, timeout=5.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), timeout)


async def _open(port: int, host: str = "127.0.0.1"):
    return await asyncio.wait_for(asyncio.open_connection(host, port), 3.0)


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


def _parse(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    status, headers = _parse_head(head + b"\r\n\r\n")
    return status, headers, body


async def _send_raw(port: int, raw: bytes) -> bytes:
    reader, writer = await _open(port)
    try:
        writer.write(raw)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 3.0)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _read_response(reader: asyncio.StreamReader):
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3.0)
    status, headers = _parse_head(head)
    length = int(headers.get("content-length", "0"))
    body = b""
    if length:
        body = await asyncio.wait_for(reader.readexactly(length), 3.0)
    return status, headers, body


def _auth_headers(origin: str, token: str = TOKEN) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Origin": origin,
        "Content-Type": "application/json",
    }


async def _call(port: int, command: p.Command, *, origin: str, token: str = TOKEN):
    raw = await _send_raw(
        port,
        _request_bytes(
            "POST",
            COMMAND_PATH,
            headers=_auth_headers(origin, token),
            body=p.encode_command(command),
        ),
    )
    return _parse(raw)


async def _open_sse(
    port: int,
    *,
    origin: str,
    session: str = "s",
    token: str | None = TOKEN,
    headers: dict[str, str] | None = None,
    **params: Any,
):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    path = f"{EVENTS_PATH}?session={session}" + (f"&{query}" if query else "")
    merged = {"Origin": origin, **(headers or {})}
    if token is not None:
        merged["Authorization"] = f"Bearer {token}"
    reader, writer = await _open(port)
    writer.write(_request_bytes("GET", path, headers=merged, connection="keep-alive"))
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3.0)
    status, response_headers = _parse_head(head)
    return reader, writer, status, response_headers


async def _collect_sse(reader, *, stop=None, timeout=5.0) -> list[Any]:
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


async def _close(reader, writer) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    del reader


# ---------------------------------------------------------------------------
# Fake facade (deterministic streams, injected failures)
# ---------------------------------------------------------------------------


class _FakeFacade:
    def __init__(
        self,
        *,
        events: tuple[Event, ...] = (),
        delay: float = 0.0,
        infinite: bool = False,
        secret_error: bool = False,
    ) -> None:
        self.commands: list[p.Command] = []
        self.events = list(events)
        self.delay = delay
        self.infinite = infinite
        self.secret_error = secret_error
        self.subscriptions = 0
        self.yielded = 0
        self.closed = asyncio.Event()

    async def handle(self, command: p.Command) -> p.Result:
        self.commands.append(command)
        if self.secret_error:
            raise RuntimeError("api_key=sk-live-supersecret123456")
        if isinstance(command, p.Health):
            return p.HealthResult(ok=True, version=p.PROTOCOL_VERSION, sessions=1)
        if isinstance(command, p.SessionOpen):
            return p.SessionOpenResult(session=SessionSummary(id=command.session))
        if isinstance(command, p.SessionStart):
            return p.SessionStartResult(session=command.session, turn_id="t")
        if isinstance(command, p.LogsRead):
            return p.LogsReadResult(
                daemon=p.DaemonLogPage(), session=p.SessionLogPage()
            )
        if isinstance(command, p.Shutdown):
            return p.ShutdownResult(stopping=True, reason=command.reason)
        return p.ErrorResult(kind="unknown", message="no")

    async def subscribe(self, session, from_seq=0, *, follow=True, client_id=None):
        self.subscriptions += 1
        try:
            for event in self.events:
                if event.seq <= from_seq:
                    continue
                if self.delay:
                    await asyncio.sleep(self.delay)
                self.yielded += 1
                yield event
            if self.infinite:
                while True:
                    await asyncio.sleep(0.01)
        finally:
            self.closed.set()


@contextlib.asynccontextmanager
async def running_server(facade, **kwargs):
    server = HTTPSSEServer(facade, token=TOKEN, **kwargs)
    await server.start()
    try:
        yield server
    finally:
        await server.aclose()


def _events(count: int) -> tuple[Event, ...]:
    return tuple(
        Event(type="text.delta", data={"text": str(i)}, seq=i, session="s")
        for i in range(1, count + 1)
    )


# ---------------------------------------------------------------------------
# Bind and token
# ---------------------------------------------------------------------------


def test_only_loopback_hosts_are_accepted():
    for host in ("0.0.0.0", "example.com", "10.0.0.1", "::"):
        with pytest.raises(ValueError):
            HTTPSSEServer(_FakeFacade(), host=host)
    assert HTTPSSEServer(_FakeFacade(), host="127.0.0.1").host == "127.0.0.1"
    assert HTTPSSEServer(_FakeFacade(), host="localhost").host == "localhost"


async def test_server_binds_loopback_and_reports_its_port():
    async with running_server(_FakeFacade()) as server:
        assert server.started
        assert server.port > 0
        reader, writer = await _open(server.port)
        await _close(reader, writer)


def test_generated_token_is_strong_and_unique():
    first = HTTPSSEServer(_FakeFacade()).token
    second = HTTPSSEServer(_FakeFacade()).token
    assert len(first) >= 32
    assert first != second


async def test_default_origins_cover_the_loopback_aliases():
    async with running_server(_FakeFacade()) as server:
        assert server.origins == loopback_origins(server.port)
        assert f"http://127.0.0.1:{server.port}" in server.origins
        assert f"http://localhost:{server.port}" in server.origins


# ---------------------------------------------------------------------------
# Auth and Origin
# ---------------------------------------------------------------------------


async def test_missing_or_wrong_token_is_unauthorized():
    async with running_server(_FakeFacade()) as server:
        origin = f"http://127.0.0.1:{server.port}"
        missing = await _send_raw(
            server.port,
            _request_bytes(
                "POST",
                COMMAND_PATH,
                headers={"Origin": origin, "Content-Type": "application/json"},
                body=p.encode_command(p.Health()),
            ),
        )
        status, headers, _ = _parse(missing)
        assert status == 401
        assert headers["www-authenticate"] == 'Bearer realm="nexus"'
        assert TOKEN not in missing.decode("latin-1")

        wrong = await _call(
            server.port, p.Health(), origin=origin, token="wrong-token-000000000000"
        )
        assert wrong[0] == 401
        assert TOKEN not in wrong[2].decode("latin-1")


async def test_valid_token_returns_the_correlated_result():
    async with running_server(_FakeFacade()) as server:
        origin = f"http://127.0.0.1:{server.port}"
        status, headers, body = await _call(server.port, p.Health(), origin=origin)
        assert status == 200
        assert headers["content-type"] == "application/json"
        assert headers["access-control-allow-origin"] == origin
        assert headers["vary"] == "Origin"
        assert "access-control-allow-credentials" not in headers
        assert isinstance(p.decode_result(body), p.HealthResult)


async def test_logs_read_peer_command_keeps_bearer_and_origin_authentication():
    async with running_server(_FakeFacade()) as server:
        origin = f"http://127.0.0.1:{server.port}"
        status, _, body = await _call(
            server.port, p.LogsRead(session="s"), origin=origin
        )
        assert status == 200
        assert isinstance(p.decode_result(body), p.LogsReadResult)
        wrong_token = await _call(
            server.port, p.LogsRead(session="s"), origin=origin, token="wrong"
        )
        assert wrong_token[0] == 401
        wrong_origin = await _send_raw(
            server.port,
            _request_bytes(
                "POST",
                COMMAND_PATH,
                headers=_auth_headers("https://evil.example"),
                body=p.encode_command(p.LogsRead(session="s")),
            ),
        )
        assert _parse(wrong_origin)[0] == 403


async def test_origin_is_required_and_allowlisted_on_every_request():
    async with running_server(_FakeFacade()) as server:
        body = p.encode_command(p.Health())

        no_origin = await _send_raw(
            server.port,
            _request_bytes(
                "POST",
                COMMAND_PATH,
                headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                body=body,
            ),
        )
        assert _parse(no_origin)[0] == 403

        foreign = await _call(server.port, p.Health(), origin="http://evil.example")
        assert foreign[0] == 403
        assert "access-control-allow-origin" not in foreign[1]

        # The SSE endpoint enforces the same allowlist.
        sse = await _open_sse(server.port, origin="http://evil.example")
        assert sse[2] == 403
        await _close(sse[0], sse[1])


async def test_explicit_origin_allowlist_replaces_the_defaults():
    async with running_server(
        _FakeFacade(), allowed_origins=["http://app.example"]
    ) as server:
        allowed = await _call(server.port, p.Health(), origin="http://app.example")
        assert allowed[0] == 200
        loopback = await _call(
            server.port, p.Health(), origin=f"http://127.0.0.1:{server.port}"
        )
        assert loopback[0] == 403


async def test_preflight_is_allowed_without_a_token_but_needs_origin():
    async with running_server(_FakeFacade()) as server:
        good = f"http://127.0.0.1:{server.port}"
        preflight = await _send_raw(
            server.port,
            _request_bytes(
                "OPTIONS",
                COMMAND_PATH,
                headers={
                    "Origin": good,
                    "Access-Control-Request-Method": "POST",
                },
            ),
        )
        status, headers, _ = _parse(preflight)
        assert status == 204
        assert headers["access-control-allow-origin"] == good
        assert "POST" in headers["access-control-allow-methods"]
        assert "Authorization" in headers["access-control-allow-headers"]
        assert "access-control-allow-credentials" not in headers

        rejected = await _send_raw(
            server.port,
            _request_bytes(
                "OPTIONS", COMMAND_PATH, headers={"Origin": "http://evil.example"}
            ),
        )
        assert _parse(rejected)[0] == 403


# ---------------------------------------------------------------------------
# Command endpoint: bodies, malformed input, and bounds
# ---------------------------------------------------------------------------


async def test_malformed_command_body_is_rejected_without_echo():
    async with running_server(_FakeFacade()) as server:
        origin = f"http://127.0.0.1:{server.port}"
        payload = b'{"type": "SessionOpen", "secret": "sk-live-supersecret123456"'
        raw = await _send_raw(
            server.port,
            _request_bytes(
                "POST", COMMAND_PATH, headers=_auth_headers(origin), body=payload
            ),
        )
        status, _, body = _parse(raw)
        assert status == 400
        assert b"sk-live-supersecret123456" not in body


async def test_unknown_command_tag_is_rejected():
    async with running_server(_FakeFacade()) as server:
        origin = f"http://127.0.0.1:{server.port}"
        raw = await _send_raw(
            server.port,
            _request_bytes(
                "POST",
                COMMAND_PATH,
                headers=_auth_headers(origin),
                body=b'{"type": "DoesNotExist"}',
            ),
        )
        assert _parse(raw)[0] == 400


async def test_wrong_content_type_is_unsupported_media():
    async with running_server(_FakeFacade()) as server:
        origin = f"http://127.0.0.1:{server.port}"
        raw = await _send_raw(
            server.port,
            _request_bytes(
                "POST",
                COMMAND_PATH,
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Origin": origin,
                    "Content-Type": "text/plain",
                },
                body=p.encode_command(p.Health()),
            ),
        )
        assert _parse(raw)[0] == 415


async def test_large_valid_body_is_read_across_the_stream_limit():
    async with running_server(_FakeFacade()) as server:
        origin = f"http://127.0.0.1:{server.port}"
        command = p.SessionStart(session="s", content="x" * 200_000)
        status, _, body = await _call(server.port, command, origin=origin)
        assert status == 200
        assert isinstance(p.decode_result(body), p.SessionStartResult)


async def test_repeated_connections_are_reaped_without_leaking_descriptors():
    async with running_server(_FakeFacade()) as server:
        origin = f"http://127.0.0.1:{server.port}"
        await wait_for(lambda: server.connection_count == 0, timeout=2.0)
        before = _open_descriptors()
        for _ in range(20):
            status, _, _ = await _call(server.port, p.Health(), origin=origin)
            assert status == 200
        await wait_for(lambda: server.connection_count == 0, timeout=2.0)
        after = _open_descriptors()
        if before is not None and after is not None:
            assert after <= before + 5


def _open_descriptors() -> int | None:
    if not Path("/dev/fd").is_dir():
        return None
    return len(list(Path("/dev/fd").iterdir()))


async def test_oversized_body_is_rejected_before_it_is_read():
    async with running_server(_FakeFacade(), max_body_bytes=64) as server:
        origin = f"http://127.0.0.1:{server.port}"
        raw = await _send_raw(
            server.port,
            _request_bytes(
                "POST",
                COMMAND_PATH,
                headers=_auth_headers(origin),
                body=b"x" * 4096,
            ),
        )
        assert _parse(raw)[0] == 413


async def test_oversized_headers_are_rejected():
    async with running_server(_FakeFacade(), max_header_bytes=256) as server:
        origin = f"http://127.0.0.1:{server.port}"
        raw = await _send_raw(
            server.port,
            _request_bytes(
                "POST",
                COMMAND_PATH,
                headers={"X-Pad": "a" * 1024, **_auth_headers(origin)},
                body=p.encode_command(p.Health()),
            ),
        )
        assert _parse(raw)[0] in (400, 431)


async def test_duplicate_and_chunked_framing_are_rejected():
    async with running_server(_FakeFacade()) as server:
        origin = f"http://127.0.0.1:{server.port}"
        duplicate = (
            "POST /v1/command HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            f"Origin: {origin}\r\n"
            f"Authorization: Bearer {TOKEN}\r\n"
            "Content-Length: 0\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        assert _parse(await _send_raw(server.port, duplicate))[0] == 400

        chunked = (
            "POST /v1/command HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            f"Origin: {origin}\r\n"
            f"Authorization: Bearer {TOKEN}\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Connection: close\r\n\r\n"
            "0\r\n\r\n"
        ).encode("latin-1")
        assert _parse(await _send_raw(server.port, chunked))[0] == 400


async def test_bad_request_line_and_unknown_route():
    async with running_server(_FakeFacade()) as server:
        origin = f"http://127.0.0.1:{server.port}"
        assert _parse(await _send_raw(server.port, b"not-http\r\n\r\n"))[0] == 400

        missing = await _send_raw(
            server.port,
            _request_bytes(
                "GET", "/nope", headers={"Origin": origin, "Authorization": f"Bearer {TOKEN}"}
            ),
        )
        assert _parse(missing)[0] == 404

        wrong_method = await _send_raw(
            server.port,
            _request_bytes(
                "GET",
                COMMAND_PATH,
                headers={"Origin": origin, "Authorization": f"Bearer {TOKEN}"},
            ),
        )
        assert _parse(wrong_method)[0] == 405


async def test_facade_failures_never_leak_a_secret():
    async with running_server(_FakeFacade(secret_error=True)) as server:
        origin = f"http://127.0.0.1:{server.port}"
        status, _, body = await _call(server.port, p.Health(), origin=origin)
        assert status == 500
        text = body.decode("utf-8")
        assert "sk-live-supersecret123456" not in text
        assert TOKEN not in text


async def test_keep_alive_serves_several_requests_and_is_bounded():
    async with running_server(
        _FakeFacade(), max_requests_per_connection=2
    ) as server:
        origin = f"http://127.0.0.1:{server.port}"
        reader, writer = await _open(server.port)
        try:
            for _ in range(2):
                writer.write(
                    _request_bytes(
                        "POST",
                        COMMAND_PATH,
                        headers=_auth_headers(origin),
                        body=p.encode_command(p.Health()),
                        connection="keep-alive",
                    )
                )
                await writer.drain()
                status, headers, body = await _read_response(reader)
                assert status == 200
                assert headers["connection"] == "keep-alive"
                assert isinstance(p.decode_result(body), p.HealthResult)
            # The third request exceeds the per-connection bound; the server
            # stops reading and closes, so the socket reaches EOF.
            writer.write(
                _request_bytes(
                    "POST",
                    COMMAND_PATH,
                    headers=_auth_headers(origin),
                    body=p.encode_command(p.Health()),
                    connection="keep-alive",
                )
            )
            with contextlib.suppress(Exception):
                await writer.drain()
            assert await asyncio.wait_for(reader.read(), 3.0) == b""
        finally:
            await _close(reader, writer)


async def test_connection_cap_is_enforced():
    async with running_server(_FakeFacade(), max_connections=2) as server:
        held = []
        try:
            for _ in range(2):
                held.append(await _open(server.port))
            await wait_for(lambda: server.connection_count == 2, timeout=3.0)
            raw = await _send_raw(server.port, b"")
            status, _, _ = _parse(raw)
            assert status == 503
        finally:
            for reader, writer in held:
                await _close(reader, writer)


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


async def test_sse_streams_events_with_seq_as_the_event_id():
    fake = _FakeFacade(events=_events(3))
    async with running_server(fake) as server:
        origin = f"http://127.0.0.1:{server.port}"
        reader, writer, status, headers = await _open_sse(server.port, origin=origin)
        try:
            assert status == 200
            assert headers["content-type"] == "text/event-stream"
            assert headers["access-control-allow-origin"] == origin
            events = await _collect_sse(reader)
        finally:
            await _close(reader, writer)
        assert [event.seq for event in events] == [1, 2, 3]
        assert all(isinstance(event, Event) for event in events)
        assert fake.subscriptions == 1


async def test_sse_replays_then_ends_when_follow_is_false():
    fake = _FakeFacade(events=_events(2))
    async with running_server(fake) as server:
        origin = f"http://127.0.0.1:{server.port}"
        reader, writer, status, _ = await _open_sse(
            server.port, origin=origin, follow="false"
        )
        assert status == 200
        events = await _collect_sse(reader, timeout=3.0)
        await _close(reader, writer)
        assert [event.seq for event in events] == [1, 2]
        assert fake.closed.is_set()


async def test_sse_last_event_id_resumes_without_a_gap():
    fake = _FakeFacade(events=_events(5))
    async with running_server(fake) as server:
        origin = f"http://127.0.0.1:{server.port}"
        first, writer, _, _ = await _open_sse(server.port, origin=origin)
        seen = await _collect_sse(
            first, stop=lambda event: getattr(event, "seq", 0) >= 2, timeout=3.0
        )
        assert [event.seq for event in seen] == [1, 2]
        await _close(first, writer)

        resumed, writer2, _, _ = await _open_sse(
            server.port, origin=origin, headers={"Last-Event-ID": "2"}
        )
        rest = await _collect_sse(resumed, timeout=3.0)
        await _close(resumed, writer2)
        assert [event.seq for event in rest] == [3, 4, 5]


async def test_sse_query_token_is_accepted_for_eventsource():
    fake = _FakeFacade(events=_events(1))
    async with running_server(fake) as server:
        origin = f"http://127.0.0.1:{server.port}"
        reader, writer, status, _ = await _open_sse(
            server.port, origin=origin, token=None, access_token=TOKEN, follow="false"
        )
        assert status == 200
        events = await _collect_sse(reader, timeout=3.0)
        await _close(reader, writer)
        assert [event.seq for event in events] == [1]


async def test_query_token_is_not_accepted_on_the_command_endpoint():
    async with running_server(_FakeFacade()) as server:
        origin = f"http://127.0.0.1:{server.port}"
        raw = await _send_raw(
            server.port,
            _request_bytes(
                "POST",
                f"{COMMAND_PATH}?access_token={TOKEN}",
                headers={"Origin": origin, "Content-Type": "application/json"},
                body=p.encode_command(p.Health()),
            ),
        )
        assert _parse(raw)[0] == 401


async def test_sse_requires_auth_and_valid_parameters():
    async with running_server(_FakeFacade()) as server:
        origin = f"http://127.0.0.1:{server.port}"

        unauthenticated = await _open_sse(server.port, origin=origin, token=None)
        assert unauthenticated[2] == 401
        await _close(unauthenticated[0], unauthenticated[1])

        no_session = await _open_sse(server.port, origin=origin, session="")
        assert no_session[2] == 400
        await _close(no_session[0], no_session[1])

        bad_seq = await _open_sse(server.port, origin=origin, from_seq="-1")
        assert bad_seq[2] == 400
        await _close(bad_seq[0], bad_seq[1])

        bad_follow = await _open_sse(server.port, origin=origin, follow="maybe")
        assert bad_follow[2] == 400
        await _close(bad_follow[0], bad_follow[1])


async def test_sse_keepalive_is_a_comment_line():
    fake = _FakeFacade(infinite=True)
    async with running_server(fake, keepalive=0.05) as server:
        origin = f"http://127.0.0.1:{server.port}"
        reader, writer, status, _ = await _open_sse(server.port, origin=origin)
        assert status == 200
        seen = bytearray()
        deadline = asyncio.get_running_loop().time() + 1.0
        while b": keepalive" not in seen and asyncio.get_running_loop().time() < deadline:
            try:
                chunk = await asyncio.wait_for(reader.read(256), 0.5)
            except TimeoutError:
                break
            if not chunk:
                break
            seen.extend(chunk)
        await _close(reader, writer)
        assert b": keepalive" in seen


async def test_slow_sse_client_is_dropped_without_blocking_the_producer():
    fake = _FakeFacade(events=_events(5000))
    async with running_server(
        fake,
        sse_queue_events=4,
        sse_queue_bytes=4096,
        write_timeout=0.2,
        keepalive=0.2,
    ) as server:
        origin = f"http://127.0.0.1:{server.port}"
        reader, writer, status, _ = await _open_sse(server.port, origin=origin)
        assert status == 200
        # Never read the body: the producer must detach rather than block.
        await asyncio.wait_for(fake.closed.wait(), 5.0)
        assert fake.yielded < len(fake.events)
        await wait_for(lambda: server.connection_count == 0, timeout=5.0)
        await _close(reader, writer)


# ---------------------------------------------------------------------------
# Real HostFacade: parity and no turn blocking
# ---------------------------------------------------------------------------


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


def _runtime(tmp_path, provider) -> Runtime:
    return Runtime(tmp_path, config=_config(), providers={"scripted": provider})


async def test_sse_event_parity_with_the_session_log(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("hello")))
    facade = HostFacade(runtime)
    facade.open_session("s")
    async with running_server(facade) as server:
        origin = f"http://127.0.0.1:{server.port}"
        reader, writer, status, _ = await _open_sse(server.port, origin=origin)
        assert status == 200
        started = await _call(
            server.port, p.SessionStart(session="s", content="hi"), origin=origin
        )
        assert started[0] == 200
        events = await _collect_sse(
            reader,
            stop=lambda event: isinstance(event, Event)
            and event.type == "turn.completed",
        )
        await _close(reader, writer)
        await facade.wait_idle(timeout=5.0)

        log = {event.seq: event for event in runtime.session("s").events}
        decoded = [event for event in events if isinstance(event, Event)]
        assert any(event.type == "turn.completed" for event in decoded)
        for event in decoded:
            assert log[event.seq].type == event.type
        seqs = [event.seq for event in decoded]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs))
    await runtime.aclose()


async def test_slow_client_never_blocks_a_real_turn(tmp_path):
    script = [
        MessageStart(model="scripted-model", provider="scripted"),
        *[TextDelta(text="x") for _ in range(4000)],
        MessageStop(stop_reason="end_turn"),
    ]
    runtime = _runtime(tmp_path, ScriptedProvider(script))
    facade = HostFacade(runtime)
    facade.open_session("s")
    async with running_server(
        facade,
        sse_queue_events=4,
        sse_queue_bytes=4096,
        write_timeout=0.2,
        keepalive=0.2,
    ) as server:
        origin = f"http://127.0.0.1:{server.port}"
        reader, writer, status, _ = await _open_sse(server.port, origin=origin)
        assert status == 200
        await _call(server.port, p.SessionStart(session="s", content="go"), origin=origin)
        # The non-reading view must not hold the turn open.
        await asyncio.wait_for(facade.wait_idle(timeout=15.0), 20.0)
        await wait_for(lambda: server.connection_count == 0, timeout=5.0)
        await _close(reader, writer)
    await runtime.aclose()


async def test_http_validate_refuses_an_unmanaged_target(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider(text_response("ok")))
    facade = HostFacade(runtime)
    sentinel = tmp_path / "pwned"
    payload = tmp_path / "payload.py"
    payload.write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    async with running_server(facade) as server:
        origin = f"http://127.0.0.1:{server.port}"
        status, _, body = await _call(
            server.port, p.ExtensionsValidate(target=str(payload)), origin=origin
        )
        assert status == 200
        assert isinstance(p.decode_result(body), p.ErrorResult)
        assert not sentinel.exists()
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Graceful close
# ---------------------------------------------------------------------------


async def test_aclose_closes_connections_and_is_idempotent():
    server = HTTPSSEServer(_FakeFacade(), token=TOKEN)
    await server.start()
    port = server.port
    reader, writer = await _open(port)
    await wait_for(lambda: server.connection_count == 1, timeout=3.0)
    await server.aclose()
    await server.aclose()
    assert server.connection_count == 0
    assert not server.started
    with pytest.raises((ConnectionError, OSError)):
        probe_reader, probe_writer = await _open(port)
        await _close(probe_reader, probe_writer)
    del reader, writer


# ---------------------------------------------------------------------------
# Layering and budget
# ---------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def test_http_sse_imports_downward_only():
    forbidden = (
        "nexus.ui",
        "nexus.cli",
        "nexus.runtime",
        "nexus.core",
        "nexus.session",
        "nexus.tools",
        "nexus.model",
        "nexus.context",
        "nexus.agents",
        "nexus.skills",
        "nexus.mcp",
        "nexus.hooks",
        "nexus.ext",
        "nexus.config",
    )
    violations = sorted(
        module for module in _imports(TRANSPORT) if module.startswith(forbidden)
    )
    assert not violations, f"http_sse.py imports {violations}"


def test_http_sse_stays_within_its_line_budget():
    physical = len(TRANSPORT.read_text(encoding="utf-8").splitlines())
    assert physical < 1000, f"http_sse.py grew to {physical} lines"
