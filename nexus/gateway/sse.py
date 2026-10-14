"""Server-Sent Event framing helpers: split an upstream byte stream, buffer for a socket.

``FrameParser`` turns the host's SSE bytes into whole ``\\n\\n``-terminated frames
without interpreting them (the gateway is never a reducer). ``FrameQueue`` is
the bounded, non-blocking hand-off between the upstream reader and a possibly
slow client socket: the producer is told ``False`` the moment the backlog is
full, and the stream is then ended instead of buffering without limit.
"""
from __future__ import annotations

import asyncio
from collections import deque


class FrameTooLarge(Exception):
    """One SSE frame exceeded the per-frame bound."""


class FrameParser:
    def __init__(self, max_frame_bytes: int) -> None:
        self._buffer = bytearray()
        self._max = max_frame_bytes

    def feed(self, data: bytes) -> list[bytes]:
        """Return the complete frames in ``data`` (each ends with a blank line)."""
        self._buffer += data.replace(b"\r\n", b"\n")
        frames: list[bytes] = []
        while True:
            end = self._buffer.find(b"\n\n")
            if end < 0:
                break
            frame = bytes(self._buffer[: end + 2])
            del self._buffer[: end + 2]
            if len(frame) > self._max:
                raise FrameTooLarge
            frames.append(frame)
        if len(self._buffer) > self._max:
            raise FrameTooLarge
        return frames


def is_relayed(frame: bytes) -> bool:
    """Upstream comments and ``retry:`` hints are replaced by the gateway's own."""
    return not frame.startswith((b":", b"retry:"))


def event_name(frame: bytes) -> str:
    for line in frame.split(b"\n"):
        if line.startswith(b"event:"):
            return line[6:].strip().decode("utf-8", "replace")
    return "message"


def sse_event(name: str, data: str, event_id: str | None = None) -> bytes:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {name}\ndata: {data}\n\n".encode()


class FrameQueue:
    """Bounded by event count and bytes; ``push`` never blocks."""

    def __init__(self, max_events: int, max_bytes: int) -> None:
        self._items: deque[bytes] = deque()
        self._bytes = 0
        self._max_events = max(1, max_events)
        self._max_bytes = max(1, max_bytes)
        self._signal = asyncio.Event()
        self._state = "open"

    def push(self, frame: bytes) -> bool:
        if self._state != "open":
            return False
        if len(self._items) >= self._max_events or self._bytes + len(frame) > self._max_bytes:
            self._state = "overflow"
            self._signal.set()
            return False
        self._items.append(frame)
        self._bytes += len(frame)
        self._signal.set()
        return True

    def close(self) -> None:
        if self._state == "open":
            self._state = "closed"
        self._signal.set()

    async def get(self, timeout: float) -> tuple[str, bytes | None]:
        """``("frame", data)``, ``("timeout", None)``, ``("end", None)`` or ``("overflow", None)``."""
        while True:
            if self._items:
                frame = self._items.popleft()
                self._bytes -= len(frame)
                return "frame", frame
            if self._state == "overflow":
                return "overflow", None
            if self._state == "closed":
                return "end", None
            self._signal.clear()
            if self._items or self._state != "open":
                continue
            try:
                await asyncio.wait_for(self._signal.wait(), timeout)
            except TimeoutError:
                return "timeout", None
