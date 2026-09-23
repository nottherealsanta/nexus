"""The local Unix-socket transport: length-framed JSON for the CLI (PLAN §14.9).

``uds`` is the client half of the local link. The daemon owns the listening
socket and the connection lifecycle (:mod:`nexus.host.daemon`); this module owns
the wire: connect, handshake, request/response correlation, and the streamed
subscription an event flow needs.

A connection is one socket with a single writer lock. Commands and results are
correlated by a fresh id; streamed events carry the id of the subscription they
belong to. One background reader dispatches every inbound frame, so a long-lived
subscription never blocks an unrelated command. The subscription queue is
bounded and applies backpressure to the reader, so a slow consumer stalls only
its own stream instead of growing without limit.

The framing (a 4-byte big-endian length plus compact JSON, capped at
:data:`~nexus.host.transports.MAX_FRAME_BYTES`) is shared with the daemon through
:mod:`nexus.host.transports`.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Self

from ...util import new_id
from .. import protocol as p
from . import (
    Bye,
    CommandFrame,
    DaemonUnavailable,
    EventFrame,
    Hello,
    Ping,
    Pong,
    ProtocolError,
    Reject,
    ResultFrame,
    SubscribeDone,
    TransportError,
    Unsubscribe,
    VersionMismatch,
    Welcome,
    read_frame,
    write_frame,
)

#: Bound on one subscription's pending events; full means the reader waits, which
#: applies backpressure through the socket rather than buffering without limit.
DEFAULT_SUBSCRIPTION_BUFFER = 1024


class EventSubscription:
    """One live event stream returned by :meth:`UDSClient.subscribe`."""

    def __init__(self, client: UDSClient, sub_id: str, buffer: int):
        self._client = client
        self.id = sub_id
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max(1, buffer))
        self._done = False

    async def feed(self, event: Any) -> None:
        if not self._done:
            await self._queue.put(event)

    def finish(self, error: str | None = None) -> None:
        """Terminate the stream without ever raising.

        A full bounded queue must not make shutdown, a reader loop, or an
        unsubscribe abort: buffered events are dropped and the terminal marker
        is placed unconditionally, so a consumer blocked in ``__anext__`` is
        always unblocked.
        """
        if self._done:
            return
        self._done = True
        marker = _END if error is None else _Failure(error)
        if self._queue.full():
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover - drained
            self._queue.put_nowait(marker)

    def __aiter__(self) -> EventSubscription:
        return self

    async def __anext__(self) -> Any:
        item = await self._queue.get()
        if item is _END:
            raise StopAsyncIteration
        if isinstance(item, _Failure):
            raise DaemonUnavailable(item.reason)
        return item

    async def aclose(self) -> None:
        await self._client.unsubscribe(self)


class _Failure:
    __slots__ = ("reason",)

    def __init__(self, reason: str):
        self.reason = reason


_END = object()


class UDSClient:
    """An async client for one daemon socket, with a correlated reader loop."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        client_id: str,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.client_id = client_id
        self.info: Welcome | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._subs: dict[str, EventSubscription] = {}
        self._pongs: dict[str, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._closing_reason = ""

    # -- construction ------------------------------------------------------

    @classmethod
    async def connect(
        cls,
        socket_path: Any,
        *,
        client_id: str | None = None,
        version: int = p.PROTOCOL_VERSION,
        client: str = "",
        timeout: float = 5.0,
        buffer: int = DEFAULT_SUBSCRIPTION_BUFFER,
    ) -> UDSClient:
        """Connect and complete the version handshake.

        A different protocol revision raises :class:`VersionMismatch` before any
        command is sent; a missing or dead socket raises :class:`DaemonUnavailable`.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(socket_path)), timeout
            )
        except (TimeoutError, OSError) as exc:
            raise DaemonUnavailable(f"cannot connect to {socket_path}: {exc}") from exc
        instance = cls(reader, writer, client_id=client_id or new_id())
        instance._buffer = buffer
        try:
            await instance._handshake(version=version, client=client, timeout=timeout)
        except BaseException:
            await instance._abort()
            raise
        instance._reader_task = asyncio.ensure_future(instance._read_loop())
        return instance

    async def _handshake(self, *, version: int, client: str, timeout: float) -> None:
        await self._write(Hello(version=version, client_id=self.client_id, client=client))
        try:
            frame = await asyncio.wait_for(read_frame(self._reader), timeout)
        except TimeoutError as exc:
            raise DaemonUnavailable("handshake timed out") from exc
        if frame is None:
            raise DaemonUnavailable("daemon closed the connection during handshake")
        if isinstance(frame, Reject):
            raise VersionMismatch(frame.expected or p.PROTOCOL_VERSION, frame.got or version)
        if not isinstance(frame, Welcome):
            raise ProtocolError(f"unexpected handshake frame {type(frame).__name__}")
        if frame.version != version:
            raise VersionMismatch(frame.version, version)
        self.info = frame
        if frame.client_id:
            self.client_id = frame.client_id

    # -- properties --------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def subscriptions(self) -> tuple[str, ...]:
        return tuple(self._subs)

    # -- requests ----------------------------------------------------------

    async def call(self, command: p.Command, *, timeout: float | None = None) -> p.Result:
        """Send one command and await its correlated result."""
        frame_id = new_id()
        return await self._send(command, frame_id, timeout=timeout)

    async def _send(
        self, command: p.Command, frame_id: str, *, timeout: float | None
    ) -> p.Result:
        if self._closed:
            raise DaemonUnavailable(self._closing_reason or "connection closed")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[frame_id] = future
        try:
            await self._write(CommandFrame(id=frame_id, command=command))
        except BaseException:
            self._pending.pop(frame_id, None)
            raise
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(frame_id, None)

    async def ping(self, *, timeout: float = 5.0) -> bool:
        nonce = new_id()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pongs[nonce] = future
        try:
            await self._write(Ping(nonce=nonce))
            await asyncio.wait_for(future, timeout)
            return True
        finally:
            self._pongs.pop(nonce, None)

    # -- subscriptions -----------------------------------------------------

    async def subscribe(
        self,
        session: str,
        from_seq: int = 0,
        *,
        follow: bool = True,
        timeout: float | None = None,
    ) -> EventSubscription:
        """Catch up from ``from_seq`` and follow, returning a live event stream."""
        sub_id = new_id()
        subscription = EventSubscription(self, sub_id, getattr(self, "_buffer", 1024))
        self._subs[sub_id] = subscription
        command = p.SessionSubscribe(session=session, from_seq=from_seq, follow=follow)
        try:
            result = await self._send(command, sub_id, timeout=timeout)
        except BaseException:
            self._subs.pop(sub_id, None)
            raise
        if isinstance(result, p.ErrorResult):
            self._subs.pop(sub_id, None)
            raise DaemonUnavailable(result.message or "subscription failed")
        return subscription

    async def unsubscribe(self, subscription: EventSubscription) -> None:
        self._subs.pop(subscription.id, None)
        subscription.finish(None)
        if not self._closed:
            with contextlib.suppress(TransportError, OSError):
                await self._write(Unsubscribe(id=subscription.id))

    # -- teardown ----------------------------------------------------------

    async def close(self) -> None:
        """Gracefully close: a ``bye`` frame, then release the socket."""
        if not self._closed:
            self._closed = True
            self._closing_reason = "client closed"
            with contextlib.suppress(TransportError, OSError):
                await self._write(Bye(reason="client closing"))
            self._finish("client closed")
        await self._shutdown()

    async def _abort(self) -> None:
        self._closed = True
        self._finish("connection aborted")
        await self._shutdown()

    async def _shutdown(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        await self.close()
        return False

    # -- internals ---------------------------------------------------------

    async def _write(self, frame: Any) -> None:
        async with self._write_lock:
            await write_frame(self._writer, frame)

    def _finish(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DaemonUnavailable(reason))
        self._pending.clear()
        for subscription in self._subs.values():
            subscription.finish(reason)
        self._subs.clear()
        for future in self._pongs.values():
            if not future.done():
                future.set_result(False)
        self._pongs.clear()

    async def _read_loop(self) -> None:
        reason = "connection closed"
        try:
            while True:
                frame = await read_frame(self._reader)
                if frame is None:
                    break
                if isinstance(frame, ResultFrame):
                    future = self._pending.get(frame.id)
                    if future is not None and not future.done():
                        future.set_result(frame.result)
                elif isinstance(frame, EventFrame):
                    subscription = self._subs.get(frame.id)
                    if subscription is not None:
                        await subscription.feed(frame.event)
                elif isinstance(frame, SubscribeDone):
                    subscription = self._subs.pop(frame.id, None)
                    if subscription is not None:
                        subscription.finish(frame.error or None)
                elif isinstance(frame, Pong):
                    future = self._pongs.get(frame.nonce)
                    if future is not None and not future.done():
                        future.set_result(True)
                elif isinstance(frame, Bye):
                    reason = frame.reason or "daemon closed"
                    break
                elif isinstance(frame, Reject):
                    reason = f"rejected: {frame.reason}"
                    break
        except asyncio.CancelledError:
            raise
        except (TransportError, OSError) as exc:
            reason = f"transport error: {exc}"
        finally:
            self._closed = True
            self._closing_reason = reason
            self._finish(reason)


__all__ = ["DEFAULT_SUBSCRIPTION_BUFFER", "EventSubscription", "UDSClient"]
