"""In-process event bus and the durable event log it publishes through.

An event is `{"id", "seq", "time", "type", "session_id", "data"}`. `id` is the
row id in the `event` table: stable across daemon restarts and the canonical
reconnect identity (`nexus/eventlog.py` turns it into a cursor). `seq` is only
this daemon's publish order; it resets on restart and is never a cursor.

**Persist before publish.** `publish` commits the event with the attached log
before any subscriber queue receives it, and gives the event its durable `id`.
If the log raises, the exception reaches the caller and nothing is delivered: a
subscriber never sees an event that was not stored. A session event whose
session row is already gone cannot be stored; `append_event` returns `None`,
and the bus does not deliver it (the session's deletion event covers it).

Each subscriber has a bounded queue. A subscriber that falls more than
`queue_max` events behind is cut off with a final `bus.overflow` event carrying
its last enqueued id; it is never silently skipped. `open()` registers a
subscriber and reads the durable head inside one lock, so replay can start from
a watermark with no publish/replay race (`Stream.open`).

`subscribe()` is the live-only iterator the runner tests use; the HTTP stream
uses `open()` together with `nexus/eventlog.py`.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol

QUEUE_MAX = 10_000


class EventLog(Protocol):
    """What the bus needs from storage (`nexus.store.Store`)."""

    def append_event(self, event: dict[str, Any]) -> int | None: ...

    def log_head(self) -> int: ...

    def log_generation(self) -> str: ...


class Subscription:
    """One registered live subscriber, opened at a durable watermark."""

    def __init__(self, bus: Bus, queue: asyncio.Queue[dict[str, Any] | None], head: int, generation: str) -> None:
        self._bus = bus
        self._queue = queue
        self.head = head
        self.generation = generation
        self.closed = False

    async def get(self) -> dict[str, Any] | None:
        """The next event, or `None` once the subscription is cut off."""
        return await self._queue.get()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._bus._drop(self._queue)
            self._queue.put_nowait(None)  # wake a reader blocked on get()


class Bus:
    def __init__(self, queue_max: int = QUEUE_MAX) -> None:
        self.queue_max = queue_max
        self._log: EventLog | None = None
        self._seq = 0
        self._lock = threading.Lock()  # publish and open() are atomic with respect to each other
        self._subscribers: dict[asyncio.Queue[dict[str, Any] | None], int | None] = {}  # queue -> last enqueued id

    def attach(self, log: EventLog) -> None:
        """Publish through `log`, so every event is durable before delivery."""
        self._log = log

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, type_: str, data: Any = None, *, session_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            if type_ == "session.deleted":
                # A deletion is global in storage and live delivery alike: its session row
                # may be gone, and the tombstone must survive the cascade.
                session_id = None
            event: dict[str, Any] = {"seq": self._seq, "time": time.time(), "type": type_,
                                     "session_id": session_id, "data": data}
            if self._log is not None:
                event_id = self._log.append_event(event)
                if event_id is None:
                    # The subject session is gone: there is no durable identity to resume from.
                    return event
                event["id"] = event_id
            self._deliver(event)
            return event

    def open(self) -> Subscription:
        """Register a subscriber and read the durable head as one atomic boundary."""
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        with self._lock:
            self._subscribers[queue] = None
            head = self._log.log_head() if self._log is not None else 0
            generation = self._log.log_generation() if self._log is not None else ""
        return Subscription(self, queue, head, generation)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Live events only, from the subscription boundary on."""
        subscription = self.open()
        try:
            while (event := await subscription.get()) is not None:
                yield event
        finally:
            subscription.close()

    # -- delivery --------------------------------------------------------------

    def _deliver(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            if queue.qsize() >= self.queue_max:
                last = self._subscribers.pop(queue)
                queue.put_nowait({
                    "time": time.time(), "type": "bus.overflow", "session_id": None, "id": None,
                    "data": {"dropped_after": last, "generation": self._log.log_generation() if self._log else "",
                             "head": self._log.log_head() if self._log else 0},
                })
                queue.put_nowait(None)
            else:
                self._subscribers[queue] = event.get("id")
                queue.put_nowait(event)

    def _drop(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            self._subscribers.pop(queue, None)
