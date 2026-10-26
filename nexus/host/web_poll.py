"""Long-poll transport for session view patches (`GET /v1/web/session-poll`).

A generic, client-neutral alternative to the SSE `session-events` stream for
clients that cannot read a streaming response body. Each poll returns the
frames `HostFacade.subscribe_web` yields, as one JSON document.

Why a feed instead of one subscription per request: every `subscribe_web`
iteration attaches a viewer, and attach/detach are recorded as presence events
in the durable session log. Subscribing per request would write events on every
round and make each poll wake the next one. So one feed (one subscription)
lives per (browser session, session id) across polls:

* A background pump reads the iterator into a bounded buffer.
* `from_seq` doubles as the acknowledgement: frames with `seq <= from_seq` are
  dropped from the buffer, the rest are (re)sent, so a lost response is simply
  fetched again.
* A feed idle longer than `FEED_IDLE` is closed (the viewer detaches). If
  `from_seq` is behind what the buffer retained, a feed fails, or it overflows,
  the answer is a `resync: true` frame and the client re-snapshots.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import deque

import msgspec

POLL_MAX_WAIT = 25.0  # seconds; long-poll ceiling
POLL_DRAIN_WINDOW = 0.03  # seconds to let further ready frames arrive after the first
POLL_MAX_FRAMES = 64
POLL_MAX_BYTES = 4 * 1024 * 1024
FRAME_MAX_BYTES = 256 * 1024  # larger frames become a resync stub (same as the SSE route)
FEED_BUFFER_BYTES = 8 * 1024 * 1024
FEED_IDLE = 60.0  # seconds without a poll before a feed is closed
MAX_FEEDS = 64


def _resync(session: str, seq: int) -> bytes:
    return msgspec.json.encode({"schema_version": 1, "session": session, "seq": seq, "resync": True})


class _Feed:
    def __init__(self, facade, session: str, from_seq: int):
        self.session = session
        self.trimmed = from_seq  # frames with seq <= trimmed are gone
        self.frames: deque[tuple[int, bytes]] = deque()
        self.size = 0
        self.changed = asyncio.Event()
        self.failed = False  # iterator raised, ended or overflowed: next poll resyncs
        self._idle: asyncio.TimerHandle | None = None
        self._iterator = facade.subscribe_web(session, from_seq).__aiter__()
        self.task = asyncio.create_task(self._pump())
        self.touch()

    def touch(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
        self._idle = asyncio.get_running_loop().call_later(FEED_IDLE, self.task.cancel)

    async def _pump(self) -> None:
        try:
            async for frame in self._iterator:
                seq = int(frame["seq"])
                body = msgspec.json.encode(frame)
                if len(body) > FRAME_MAX_BYTES:
                    body = _resync(self.session, seq)
                self.frames.append((seq, body))
                self.size += len(body)
                self.changed.set()
                if self.size > FEED_BUFFER_BYTES:
                    break
            self.failed = True  # ended or overflowed
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - any subscribe failure becomes a resync
            self.failed = True
        finally:
            self.changed.set()
            with contextlib.suppress(Exception):
                await asyncio.shield(self._iterator.aclose())

    def ack(self, seq: int) -> None:
        while self.frames and self.frames[0][0] <= seq:
            self.size -= len(self.frames.popleft()[1])
        self.trimmed = max(self.trimmed, seq)

    async def close(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.task


class PollHub:
    """Feeds for one `BrowserRoutes`; `aclose` releases every viewer."""

    def __init__(self, facade):
        self.facade = facade
        self._feeds: dict[tuple[str, str], _Feed] = {}

    async def poll(self, owner: str, session: str, from_seq: int, wait: float) -> bytes:
        key = (owner, session)
        feed = self._feeds.get(key)
        if feed is not None and from_seq < feed.trimmed:
            # The client is behind what the buffer kept: replay from its cursor.
            self._feeds.pop(key, None)
            await feed.close()
            feed = None
        if feed is None:
            while len(self._feeds) >= MAX_FEEDS:
                oldest = next(iter(self._feeds))
                await self._feeds.pop(oldest).close()
            try:
                feed = self._feeds[key] = _Feed(self.facade, session, from_seq)
            except Exception:  # noqa: BLE001 - subscribe failure is a resync, not an HTTP error
                return self._wrap(session, [_resync(session, from_seq)])
        feed.touch()
        feed.ack(from_seq)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while not feed.frames and not feed.failed:
            feed.changed.clear()
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(feed.changed.wait(), remaining)
        if feed.frames and not feed.failed:
            await asyncio.sleep(POLL_DRAIN_WINDOW)  # let simultaneous frames land
        frames: list[bytes] = []
        total = 0
        for _, body in feed.frames:
            if len(frames) >= POLL_MAX_FRAMES or total + len(body) > POLL_MAX_BYTES and frames:
                break
            frames.append(body)
            total += len(body)
        if feed.failed and len(frames) == len(feed.frames):
            # Everything buffered is delivered; then tell the client to re-snapshot.
            frames.append(_resync(session, int(msgspec.json.decode(frames[-1])["seq"]) if frames else from_seq))
            self._feeds.pop(key, None)
            await feed.close()
        feed.touch()
        return self._wrap(session, frames)

    @staticmethod
    def _wrap(session: str, frames: list[bytes]) -> bytes:
        head = b'{"schema_version":1,"session":' + msgspec.json.encode(session) + b',"frames":['
        return head + b",".join(frames) + b"]}"

    async def aclose(self) -> None:
        feeds, self._feeds = list(self._feeds.values()), {}
        for feed in feeds:
            await feed.close()
