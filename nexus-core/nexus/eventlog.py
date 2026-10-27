"""Durable event identity, cursors, and the replay-to-live stream (PLAN.md Step 11).

**Identity.** Every published event is stored in the `event` table before it is
delivered, and its row `id` is its durable id. The `log` table holds one
`generation` (`log_<hex>`) identifying the database's log. A row id alone is
meaningless across databases, so a cursor is `"<generation>:<event id>"`.
Ordering is the ascending row id, which is also publish order. A replacement or
freshly created database gets a new generation, so old cursors are rejected
instead of silently returning unrelated events. Copying a database copies its
generation, which is correct: it is the same log.

**The replay boundary.** `Stream.open` builds the session filter and registers
the live subscription, then reads the durable head as one synchronous,
lock-protected step (`Bus.open`). Everything at or below that head is replayed
from storage in pages; everything published after it is already queued. Live
events with `id <= head` are skipped, which removes the replay/live overlap
without losing an event published during catch-up. Ordering is stable: replay
is ascending by id and the queue is FIFO.

**Filtering.** A session filter includes the selected session, its descendants,
and every global event (`session_id: null`). Membership is maintained from the
log itself (`session.created` carries `parent_id`; `session.deleted` is a global
tombstone), so a matching relationship that disappears after a deletion does
not break filtering, and a descendant created during catch-up is included.
Numeric gaps are normal on a filtered stream and never mean loss; only
`bus.overflow` (an explicit continuity outcome) does.

**Invalid cursors.** Malformed text, another generation, a cursor ahead of the
head, and (later, with retention) history below the log floor raise
`CursorError` with an actionable resync payload before any stream is opened.
This increment does not expire history: the floor stays 0.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

from .bus import Bus, Subscription
from .store import Store

REPLAY_PAGE = 500
CURSOR_MAX = 128
_CURSOR = re.compile(r"\A([A-Za-z0-9_]+):(0|[1-9][0-9]*)\Z")


def format_cursor(generation: str, event_id: int) -> str:
    return f"{generation}:{event_id}"


def parse_cursor(text: str, *, generation: str, head: int, floor: int = 0) -> int:
    """The event id a cursor resumes after, or a `CursorError` explaining the resync."""
    shown = text if isinstance(text, str) and len(text) <= CURSOR_MAX else repr(text)[:CURSOR_MAX]
    if not isinstance(text, str) or len(text) > CURSOR_MAX or not (match := _CURSOR.match(text)):
        raise InvalidCursor(
            f"cursor {shown!r} is malformed; expected '<log generation>:<event id>', e.g. "
            f"{format_cursor(generation, head)}")
    cursor_generation, raw = match.groups()
    if cursor_generation != generation:
        raise CursorGeneration(
            f"cursor {shown!r} belongs to log generation {cursor_generation}, but this database is {generation}; "
            "the log was replaced, so reload state and resume from the reported head")
    event_id = int(raw)
    if event_id > head:
        raise CursorAhead(
            f"cursor {shown!r} is ahead of this log (head {format_cursor(generation, head)}); "
            "reload state and resume from the reported head")
    if event_id < floor:
        raise HistoryUnavailable(
            f"cursor {shown!r} is older than the retained log (floor {format_cursor(generation, floor)}); "
            "reload state and resume from the reported head")
    return event_id


class CursorError(Exception):
    """A cursor this log cannot serve, with everything a client needs to resync."""

    status = 400
    code = "invalid_cursor"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def payload(self, store: Store) -> dict[str, Any]:
        head = store.log_head()
        floor = store.log_floor()
        generation = store.log_generation()
        return {
            "error": self.message,
            "code": self.code,
            "resync": True,
            "generation": generation,
            "head": head,
            "head_cursor": format_cursor(generation, head),
            "floor": floor,
            "floor_cursor": format_cursor(generation, floor),
        }


class InvalidCursor(CursorError):
    status = 400
    code = "invalid_cursor"


class CursorGeneration(CursorError):
    status = 409
    code = "cursor_generation"


class CursorAhead(CursorError):
    status = 409
    code = "cursor_ahead"


class HistoryUnavailable(CursorError):
    status = 410
    code = "history_unavailable"


class SessionFilter:
    """The sessions a filtered stream includes, maintained from the log itself."""

    def __init__(self, store: Store, root_id: str) -> None:
        self.root_id = root_id
        self._members: set[str] = set()
        self._parents: dict[str, str | None] = {}
        for session_id, parent_id in store.tree_members(root_id):
            self._members.add(session_id)
            self._parents[session_id] = parent_id

    def observe(self, event: dict[str, Any]) -> None:
        """Update membership from an event, including ones the filter then drops."""
        data = event.get("data")
        if event["type"] == "session.created" and event.get("session_id"):
            session_id = event["session_id"]
            parent_id = data.get("parent_id") if isinstance(data, dict) else None
            if session_id == self.root_id or parent_id in self._members:
                self._members.add(session_id)
                self._parents[session_id] = parent_id
        elif event["type"] == "session.deleted" and isinstance(data, dict):
            gone = data.get("id")
            if gone in self._members:
                for session_id in [s for s in self._members if self._within(s, gone)]:
                    self._members.discard(session_id)
                    self._parents.pop(session_id, None)

    def _within(self, session_id: str, ancestor: str) -> bool:
        seen: set[str] = set()
        current: str | None = session_id
        while current is not None and current not in seen:
            if current == ancestor:
                return True
            seen.add(current)
            current = self._parents.get(current)
        return False

    def match(self, event: dict[str, Any]) -> bool:
        session_id = event.get("session_id")
        return session_id is None or session_id in self._members


class Stream:
    """One atomic replay-then-live event stream."""

    def __init__(self, store: Store, *, after: int, subscription: Subscription,
                 filter: SessionFilter | None) -> None:
        self.store = store
        self.subscription = subscription
        self.generation = subscription.generation
        self.head = subscription.head
        # No cursor means "start now": the boundary is the head read when the subscription opened.
        # Callers validate a supplied cursor (`parse_cursor`); this class never substitutes one silently.
        self.after = after
        self.filter = filter
        self.replayed = 0
        self.replay_pages = 0
        self._closed = False

    @classmethod
    def open(cls, store: Store, bus: Bus, *, after: int | None = None, session_id: str | None = None) -> Stream:
        filter_ = SessionFilter(store, session_id) if session_id is not None else None
        subscription = bus.open()
        return cls(store, after=subscription.head if after is None else after,
                   subscription=subscription, filter=filter_)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.subscription.close()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        try:
            last = self.after
            while last < self.head:
                rows = self.store.replay(last, self.head, REPLAY_PAGE)
                if not rows:
                    break  # the remaining rows were deleted; nothing left below the head
                self.replay_pages += 1
                for row in rows:
                    last = row["id"]
                    event = _with_cursor(row, self.generation)
                    if self._keep(event):
                        self.replayed += 1
                        yield event
                await asyncio.sleep(0)  # let a publisher commit; its event queues up for the live phase
            while True:
                event = await self.subscription.get()
                if event is None:
                    return
                if event["type"] == "bus.overflow":
                    yield event
                    return
                if event.get("id") is not None and event["id"] <= self.head:
                    continue  # already replayed: the overlap at the handoff
                if self._keep(event):
                    yield _with_cursor(event, self.generation)
        finally:
            self.close()

    def _keep(self, event: dict[str, Any]) -> bool:
        if self.filter is None:
            return True
        self.filter.observe(event)
        return self.filter.match(event)


def _with_cursor(event: dict[str, Any], generation: str) -> dict[str, Any]:
    event_id = event.get("id")
    return event if event_id is None else {**event, "cursor": format_cursor(generation, event_id)}
