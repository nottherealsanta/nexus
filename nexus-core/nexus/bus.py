"""In-process event bus: every event the daemon emits, fanned out to subscribers.

An event is `{"seq", "time", "type", "session_id", "data"}`. `seq` increases by
one per event for the life of the daemon, so a client can tell if it missed
any. Each subscriber has a bounded queue. A subscriber that falls more than
`QUEUE_MAX` events behind is cut off with a final `bus.overflow` event, never
silently skipped. `sink`, when set, sees every event first (the daemon uses it to
store session events).
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

QUEUE_MAX = 10_000


class Bus:
    def __init__(self) -> None:
        self.sink: Callable[[dict[str, Any]], None] | None = None
        self._seq = 0
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()

    def publish(self, type_: str, data: Any = None, *, session_id: str | None = None) -> dict[str, Any]:
        self._seq += 1
        event = {"seq": self._seq, "time": time.time(), "type": type_, "session_id": session_id, "data": data}
        if self.sink is not None:
            self.sink(event)
        for queue in list(self._subscribers):
            if queue.qsize() >= QUEUE_MAX - 1:
                self._subscribers.discard(queue)
                queue.put_nowait({**event, "type": "bus.overflow", "data": {"dropped_after": event["seq"] - 1}})
                queue.put_nowait(None)
            else:
                queue.put_nowait(event)
        return event

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            self._subscribers.discard(queue)
