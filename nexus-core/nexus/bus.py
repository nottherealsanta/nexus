"""In-process event bus and the durable event log it publishes through.

An event is `{"id", "seq", "time", "type", "session_id", "data"}`. `id` is the
row id in the `event` table: stable across daemon restarts and the canonical
reconnect identity (`nexus/eventlog.py` turns it into a cursor). `seq` is only
this daemon's publish order; it resets on restart and is never a cursor.

**Persist before publish.** For durable events, `publish` commits to the attached log
before any subscriber queue receives it, and gives the event its durable `id`.
If the log raises, the exception reaches the caller and nothing is delivered: a
durable subscriber never sees an event that was not stored. `durable=False`
delivers live only with `id: None`, preserving the last durable watermark. A session event whose
session row is already gone cannot be stored; `append_event` returns `None`,
and the bus does not deliver it (the session's deletion event covers it).

Each subscriber has a bounded queue. A subscriber that falls more than
`queue_max` events behind is cut off with a final `bus.overflow` event carrying
its last enqueued id; it is never silently skipped. `open()` registers a
subscriber and reads the durable head inside one lock, so replay can start from
a watermark with no publish/replay race (`Stream.open`).

`subscribe()` is the live-only iterator the runner tests use; the HTTP stream
uses `open()` together with `nexus/eventlog.py`.

`publish_threadsafe(loop, ...)` is the synchronous form for a caller on another
thread (the recorder host): it commits on that thread, appends to `_pending`
under the bus lock and wakes `loop` with `call_soon_threadsafe(self.flush)`. The
event is durable before the call returns; delivery is on `loop`, in event-id
order with loop-side `publish` and `apublish`. A durable event whose loop is
already closed does not raise (the row is replayed by a reconnecting stream); a
live-only event cannot be replayed, so it raises `RuntimeError` instead of
silently succeeding. Either way the undeliverable event is dropped from
`_pending`, so a later flush cannot deliver it stale. A storage failure raises
with nothing delivered.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
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
        # Events committed on the store's writer thread, waiting for the loop to deliver them. Appended under the
        # lock in commit (id) order; every loop-side delivery drains this first, so delivery order is id order.
        self._pending: deque[dict[str, Any]] = deque()

    def attach(self, log: EventLog) -> None:
        """Attach storage for durable publishes."""
        self._log = log

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, type_: str, data: Any = None, *, session_id: str | None = None,
                turn_id: str | None = None, durable: bool = True) -> dict[str, Any]:
        with self._lock:
            event, stored = self._store_event(type_, data, session_id, turn_id, durable)
            if stored:
                self._drain()
                self._deliver(event)
            return event

    def _store_event(self, type_: str, data: Any, session_id: str | None, turn_id: str | None,
                     durable: bool) -> tuple[dict[str, Any], bool]:
        """Number and (when durable) commit one event. Holds `_lock`.

        The flag is false when the subject session is gone: there is no durable identity to resume from,
        so the event is not delivered.
        """
        self._seq += 1
        if type_ == "session.deleted":
            # A deletion is global in storage and live delivery alike: its session row
            # may be gone, and the tombstone must survive the cascade.
            session_id = None
        event: dict[str, Any] = {"seq": self._seq, "time": time.time(), "type": type_,
                                 "session_id": session_id, "data": data}
        if not durable:
            event["id"] = None
        if self._log is not None and durable:
            # Internal storage metadata: the store records it for payload
            # retention, and it is removed before any subscriber sees it.
            if turn_id is not None:
                event["_turn_id"] = turn_id
            event_id = self._log.append_event(event)
            event.pop("_turn_id", None)
            if event_id is None:
                return event, False
            event["id"] = event_id
        return event, True

    async def apublish(self, type_: str, data: Any = None, *, session_id: str | None = None,
                       turn_id: str | None = None, durable: bool = True) -> dict[str, Any]:
        """`publish` with the commit on the store's writer thread; the loop is free while it runs.

        Same contract: persisted before any subscriber sees it, and durable when this returns. Delivery
        happens on the loop, in id order with every other publish (see `_pending`).
        """
        if self._log is None or not durable or not hasattr(self._log, "w"):
            return self.publish(type_, data, session_id=session_id, turn_id=turn_id, durable=durable)
        loop = asyncio.get_running_loop()

        def job(_store: Any) -> dict[str, Any]:
            with self._lock:
                event, stored = self._store_event(type_, data, session_id, turn_id, durable)
                if stored:
                    self._pending.append(event)
            if stored:
                loop.call_soon_threadsafe(self.flush)
            return event

        event = await self._log.w.run(job)
        self.flush()
        return event

    def publish_threadsafe(self, loop: asyncio.AbstractEventLoop, type_: str, data: Any = None, *,
                           session_id: str | None = None, turn_id: str | None = None,
                           durable: bool = True) -> dict[str, Any]:
        """`publish` for a caller on another thread; delivery happens on `loop`.

        Same contract: a durable event is committed before this returns, and a
        storage failure raises with nothing delivered. Live-only events take the
        same ordered path with `id: None`. The loop is only woken after the lock
        is released. If the loop is already closed, a durable event is returned
        (its row is replayed, so raising would cause a duplicate republish), but
        a live-only event raises `RuntimeError` because it cannot be replayed.
        The stale `_pending` entry is removed in both cases.
        """
        with self._lock:
            event, stored = self._store_event(type_, data, session_id, turn_id, durable)
            if stored:
                self._pending.append(event)
        if stored:
            try:
                loop.call_soon_threadsafe(self.flush)
            except RuntimeError:  # loop closed while shutting down
                self._discard_pending(event)
                if not durable:
                    raise RuntimeError(
                        "cannot deliver a live-only event: the event loop is closed"
                    ) from None
        return event

    def _discard_pending(self, event: dict[str, Any]) -> None:
        """Remove one undelivered event from `_pending`."""
        with self._lock:
            for index, pending in enumerate(self._pending):
                if pending is event:
                    del self._pending[index]
                    return

    def flush(self) -> None:
        """Deliver events committed on the writer thread, in order."""
        with self._lock:
            self._drain()

    def _drain(self) -> None:
        while self._pending:
            self._deliver(self._pending.popleft())

    def deliver_stored(self, event: dict[str, Any]) -> None:
        """Deliver an announcement already committed atomically with maintenance."""
        with self._lock:
            self._drain()
            self._deliver(event)

    def open(self) -> Subscription:
        """Register a subscriber and read the durable head as one atomic boundary."""
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        with self._lock:
            self._drain()  # committed before the head read below, so it must not reach this subscriber as live
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
                if event.get("id") is not None:
                    self._subscribers[queue] = event["id"]
                queue.put_nowait(event)

    def _drop(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            self._subscribers.pop(queue, None)
