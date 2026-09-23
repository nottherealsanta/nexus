"""Phase 8b1 UDS transport: framing, handshake, correlation, and streaming.

The daemon's connection handler is exercised in ``test_host_daemon.py``; this
module pins the *client* half and the shared frame codec in isolation, against a
small in-process fake server that speaks the same length-framed JSON. It also
covers the bounds a corrupt peer would otherwise exploit: an oversized length,
a truncated header, and an unknown frame kind.
"""
from __future__ import annotations

import asyncio
import contextlib
import shutil
import struct
import tempfile
from pathlib import Path

import pytest

from nexus.events import Event
from nexus.host import protocol as p
from nexus.host import transports as t
from nexus.host.transports import uds
from nexus.host.transports.uds import UDSClient


@pytest.fixture
def short_dir():
    """A short socket directory; macOS caps a Unix socket path near 104 bytes."""
    path = Path(tempfile.mkdtemp(prefix="nexus-uds-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _sock(directory: Path, name: str = "s.sock") -> Path:
    return directory / name


async def wait_for(predicate, timeout=3.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait(), timeout)


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------


def test_every_frame_kind_round_trips():
    frames = [
        t.Hello(version=1, client_id="c"),
        t.Welcome(version=1, pid=2, workspace="/w", socket="/s", client_id="c"),
        t.Reject(reason="version_mismatch", expected=1, got=2),
        t.CommandFrame(id="1", command=p.SessionStart(session="s", content="hi")),
        t.ResultFrame(id="1", result=p.HealthResult(ok=True)),
        t.EventFrame(id="1", event=Event(type="text", seq=3, session="s")),
        t.SubscribeDone(id="1"),
        t.Unsubscribe(id="1"),
        t.Ping(nonce="n"),
        t.Pong(nonce="n"),
        t.Bye(reason="bye"),
    ]
    assert {type(frame) for frame in frames} == set(t.FRAMES)
    for frame in frames:
        encoded = t.encode_frame(frame)
        assert struct.unpack(">I", encoded[:4])[0] == len(encoded) - 4
        assert t.decode_frame(encoded[4:]) == frame


def test_oversized_frame_is_refused_on_encode(monkeypatch):
    monkeypatch.setattr(t, "MAX_FRAME_BYTES", 8)
    with pytest.raises(t.FrameTooLarge):
        t.encode_frame(t.Ping(nonce="x" * 32))


def test_unknown_frame_kind_is_a_protocol_error():
    with pytest.raises(t.ProtocolError):
        t.decode_frame(b'{"type": "not_a_frame"}')


async def test_read_frame_rejects_an_oversized_length():
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack(">I", t.MAX_FRAME_BYTES + 1))
    reader.feed_eof()
    with pytest.raises(t.FrameTooLarge):
        await t.read_frame(reader)


async def test_read_frame_reports_a_truncated_header():
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x00\x00")
    reader.feed_eof()
    with pytest.raises(t.ProtocolError):
        await t.read_frame(reader)


async def test_read_frame_returns_none_on_clean_eof():
    reader = asyncio.StreamReader()
    reader.feed_eof()
    assert await t.read_frame(reader) is None


async def test_read_write_frame_over_a_socketpair():
    import socket as _socket

    left_sock, right_sock = _socket.socketpair()
    left = await asyncio.open_connection(sock=left_sock)
    right = await asyncio.open_connection(sock=right_sock)
    try:
        await t.write_frame(left[1], t.Ping(nonce="hello"))
        assert await t.read_frame(right[0]) == t.Ping(nonce="hello")
    finally:
        left[1].close()
        right[1].close()


# ---------------------------------------------------------------------------
# Fake server
# ---------------------------------------------------------------------------


class _FakeServer:
    """A minimal length-framed peer for exercising the client in isolation."""

    def __init__(
        self,
        *,
        version: int = p.PROTOCOL_VERSION,
        reject: bool = False,
        events: tuple[Event, ...] = (),
        delay: float = 0.0,
    ) -> None:
        self.version = version
        self.reject = reject
        self.events = events
        self.delay = delay
        self.server: asyncio.AbstractServer | None = None
        self.seen: list[p.Command] = []
        self.byes = 0
        self.unsubscribes: list[str] = []

    async def start(self, path: Path) -> None:
        self.server = await asyncio.start_unix_server(self._handle, path=str(path))

    async def aclose(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        try:
            hello = await t.read_frame(reader)
            if not isinstance(hello, t.Hello):
                return
            if self.reject or hello.version != self.version:
                await t.write_frame(
                    writer,
                    t.Reject(
                        reason="version_mismatch",
                        expected=self.version,
                        got=hello.version,
                    ),
                )
                return
            await t.write_frame(
                writer,
                t.Welcome(version=self.version, pid=1, client_id=hello.client_id or "srv"),
            )
            while True:
                frame = await t.read_frame(reader)
                if frame is None:
                    break
                if isinstance(frame, t.CommandFrame):
                    await self._command(frame, writer)
                elif isinstance(frame, t.Ping):
                    await t.write_frame(writer, t.Pong(nonce=frame.nonce))
                elif isinstance(frame, t.Unsubscribe):
                    self.unsubscribes.append(frame.id)
                elif isinstance(frame, t.Bye):
                    self.byes += 1
                    break
        except (t.TransportError, OSError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _command(self, frame: t.CommandFrame, writer) -> None:
        self.seen.append(frame.command)
        if isinstance(frame.command, p.SessionSubscribe):
            await t.write_frame(
                writer,
                t.ResultFrame(
                    id=frame.id,
                    result=p.SessionSubscribeResult(
                        session=frame.command.session, from_seq=frame.command.from_seq
                    ),
                ),
            )
            for event in self.events:
                if event.seq > frame.command.from_seq:
                    await asyncio.sleep(self.delay)
                    await t.write_frame(writer, t.EventFrame(id=frame.id, event=event))
            await t.write_frame(writer, t.SubscribeDone(id=frame.id))
        elif isinstance(frame.command, p.Health):
            await t.write_frame(
                writer, t.ResultFrame(id=frame.id, result=p.HealthResult(ok=True, sessions=2))
            )
        elif isinstance(frame.command, p.SessionExport):
            await t.write_frame(
                writer,
                t.ResultFrame(
                    id=frame.id,
                    result=p.SessionExportResult(
                        session=frame.command.session,
                        format=frame.command.format,
                        content="{}",
                    ),
                ),
            )
        else:
            await t.write_frame(
                writer,
                t.ResultFrame(id=frame.id, result=p.ErrorResult(kind="x", message="no")),
            )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


async def test_connect_refused_is_daemon_unavailable(short_dir):
    with pytest.raises(t.DaemonUnavailable):
        await UDSClient.connect(_sock(short_dir), timeout=0.5)


async def test_handshake_and_correlated_call(short_dir):
    server = _FakeServer()
    await server.start(_sock(short_dir))
    try:
        client = await UDSClient.connect(_sock(short_dir), client_id="c1")
        assert client.info is not None and client.info.client_id == "c1"
        result = await client.call(p.Health(), timeout=2.0)
        assert isinstance(result, p.HealthResult) and result.sessions == 2
        # An ErrorResult is returned as-is, never raised.
        failed = await client.call(p.SessionCancel(session="s"), timeout=2.0)
        assert isinstance(failed, p.ErrorResult)
        await client.close()
        await wait_for(lambda: server.byes == 1)
    finally:
        await server.aclose()


async def test_version_mismatch_fails_loudly(short_dir):
    server = _FakeServer()
    await server.start(_sock(short_dir))
    try:
        with pytest.raises(t.VersionMismatch) as info:
            await UDSClient.connect(_sock(short_dir), version=p.PROTOCOL_VERSION + 1)
        assert info.value.expected == p.PROTOCOL_VERSION
        assert info.value.got == p.PROTOCOL_VERSION + 1
    finally:
        await server.aclose()


async def test_subscription_streams_then_completes(short_dir):
    events = tuple(
        Event(type="text.delta", data={"text": str(i)}, seq=i, session="s")
        for i in range(1, 4)
    )
    server = _FakeServer(events=events)
    await server.start(_sock(short_dir))
    try:
        client = await UDSClient.connect(_sock(short_dir))
        subscription = await client.subscribe("s", 0, follow=True, timeout=2.0)
        received = [event async for event in subscription]
        assert [event.seq for event in received] == [1, 2, 3]
        # Catch-up honours the exclusive from_seq.
        later = await client.subscribe("s", 2, follow=True, timeout=2.0)
        assert [event.seq async for event in later] == [3]
        await client.close()
    finally:
        await server.aclose()


async def test_unsubscribe_stops_the_stream(short_dir):
    server = _FakeServer(events=(Event(type="text", seq=1, session="s"),), delay=0.2)
    await server.start(_sock(short_dir))
    try:
        client = await UDSClient.connect(_sock(short_dir))
        subscription = await client.subscribe("s", 0, follow=True, timeout=2.0)
        await subscription.aclose()
        await wait_for(lambda: bool(server.unsubscribes))
        assert server.unsubscribes == [subscription.id]
        assert client.subscriptions == ()
        await client.close()
    finally:
        await server.aclose()


async def test_ping_round_trips(short_dir):
    server = _FakeServer()
    await server.start(_sock(short_dir))
    try:
        client = await UDSClient.connect(_sock(short_dir))
        assert await client.ping(timeout=2.0) is True
        await client.close()
    finally:
        await server.aclose()


async def test_calls_after_close_are_unavailable(short_dir):
    server = _FakeServer()
    await server.start(_sock(short_dir))
    try:
        client = await UDSClient.connect(_sock(short_dir))
        await client.close()
        with pytest.raises(t.DaemonUnavailable):
            await client.call(p.Health(), timeout=1.0)
    finally:
        await server.aclose()


async def test_slow_consumer_applies_backpressure_not_unbounded_growth(short_dir):
    events = tuple(Event(type="text", seq=i, session="s") for i in range(1, 200))
    server = _FakeServer(events=events)
    await server.start(_sock(short_dir))
    try:
        client = await UDSClient.connect(_sock(short_dir), buffer=4)
        subscription = await client.subscribe("s", 0, follow=True, timeout=2.0)
        first = await subscription.__anext__()
        assert first.seq == 1
        await subscription.aclose()
        await client.close()
    finally:
        await server.aclose()


async def test_event_subscription_failure_is_raised():
    subscription = uds.EventSubscription(None, "s", 4)
    subscription.finish("boom")
    with pytest.raises(t.DaemonUnavailable):
        await subscription.__anext__()


async def test_finish_on_a_full_queue_never_raises_and_terminates():
    # A bounded queue at capacity must not make finish raise QueueFull, which
    # would otherwise abort a close, read loop, or unsubscribe.
    subscription = uds.EventSubscription(None, "s", 1)
    await subscription.feed("buffered")
    subscription.finish(None)
    # A second finish is a no-op rather than a second QueueFull-prone put.
    subscription.finish("late")
    with pytest.raises(StopAsyncIteration):
        await subscription.__anext__()

    failed = uds.EventSubscription(None, "s", 1)
    await failed.feed("buffered")
    failed.finish("boom")
    with pytest.raises(t.DaemonUnavailable):
        await failed.__anext__()


async def test_finish_unblocks_a_consumer_when_the_queue_is_full():
    subscription = uds.EventSubscription(None, "s", 1)
    await subscription.feed("one")
    blocked = asyncio.create_task(subscription.feed("two"))
    await asyncio.sleep(0)  # let ``feed`` park on the full queue
    subscription.finish(None)  # must not raise
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(subscription.__anext__(), 1.0)
    blocked.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await blocked
