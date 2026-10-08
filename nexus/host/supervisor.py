"""Turn scheduling across sessions under one global concurrency cap.

PLAN §14.5: the daemon runs many sessions at once, and without a cap "five
background sessions x parallel tool calls x subagents is a fork bomb". The
supervisor is that cap. It owns:

* **a global maximum** on concurrently *running* turns (``max_concurrent``);
* **a per-session FIFO** so submissions to one session preserve order;
* **fair (round-robin) scheduling** across sessions, so one busy session cannot
  starve the others of the global slots;
* **cancellation** that stops the active turn and drops that session's queue;
* **independence from views** — scheduling never consults a subscriber count, so
  a turn submitted with no viewer runs to completion and is recoverable later.

A session can only run one turn at a time (its own exclusive lease), so the
supervisor holds at most one in-flight turn per session and parks the rest.
``content=None`` means "consume this session's own persisted input queue"; the
supervisor never reads the session log itself.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..util import new_id, redact_secrets

#: Optional sink for ``daemon.session_*`` lifecycle events.
Emit = Callable[[str, dict[str, Any]], None]


@dataclass
class _Pending:
    turn_id: str
    content: Any  # str | list[ContentBlock] | None (consume the session queue)
    queued_id: str = ""


class Supervisor:
    """Schedule detached turns across sessions under a global cap."""

    def __init__(self, *, max_concurrent: int = 4, emit: Emit | None = None) -> None:
        if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int):
            raise TypeError("max_concurrent must be an integer")
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._max = max_concurrent
        self._emit: Emit = emit or (lambda _type, _data: None)
        self._lock = asyncio.Lock()
        self._queues: dict[str, deque[_Pending]] = {}
        self._sessions: dict[str, Any] = {}
        self._active: set[str] = set()
        #: Sessions whose ``_start`` is in flight -- the turn id is marked active
        #: but ``session.start_turn`` has not yet installed a lease. A cancel that
        #: lands in this window must not be lost, so it is recorded in
        #: ``_cancel_requested`` and honoured the moment the lease exists.
        self._starting: dict[str, _Pending] = {}
        #: session id -> (reason, drop_queue, parked_extra) for a cancel that
        #: arrived while the session's start was in flight. Consumed by
        #: ``_start``. ``parked_extra`` is the number of supervisor-only
        #: submissions already removed at cancel time; the durable FIFO is
        #: dropped when the lease exists and the *actual* count reconciled then.
        self._cancel_requested: dict[str, tuple[str, bool, int]] = {}
        self._rotation: deque[str] = deque()
        self._watchers: dict[str, asyncio.Task] = {}
        self._idle = asyncio.Event()
        self._idle.set()

    # -- introspection -----------------------------------------------------

    @property
    def max_concurrent(self) -> int:
        return self._max

    @property
    def running(self) -> int:
        """Number of turns currently in flight across every session."""
        return len(self._active)

    def running_for(self, session: str) -> int:
        return 1 if session in self._active else 0

    def queued_for(self, session: str) -> int:
        queue = self._queues.get(session)
        return len(queue) if queue else 0

    @property
    def queued(self) -> int:
        return sum(len(queue) for queue in self._queues.values())

    def active_sessions(self) -> tuple[str, ...]:
        return tuple(self._active)

    # -- submission --------------------------------------------------------

    async def submit(
        self,
        session_id: str,
        session: Any,
        content: Any = None,
        *,
        queued_id: str = "",
        priority: bool = False,
    ) -> str:
        """Queue one turn for ``session_id``; return its pre-assigned turn id.

        The turn id is assigned up front so a caller can subscribe to it before
        the scheduler actually starts it. ``content=None`` consumes the head of
        the session's own persisted input queue at start time.
        """
        turn_id = new_id()
        self._sessions[session_id] = session
        queue = self._queues.setdefault(session_id, deque())
        (queue.appendleft if priority else queue.append)(
            _Pending(turn_id=turn_id, content=content, queued_id=queued_id)
        )
        if session_id not in self._rotation:
            self._rotation.append(session_id)
        self._idle.clear()
        self._emit(
            "daemon.session_queued",
            {"session": session_id, "turn_id": turn_id, "depth": self.queued_for(session_id)},
        )
        await self._pump()
        return turn_id

    def reorder(self, session_id: str, order: Sequence[str]) -> None:
        """Follow a user reorder of the session's durable FIFO.

        Each parked queued pending consumes the session's queue head when it
        starts and is skipped once its own id has gone, so the parked ids must
        follow the durable order or a moved message would never run.
        """
        queue = self._queues.get(session_id)
        if not queue:
            return
        ranked = {key: rank for rank, key in enumerate(order)}
        slots = [pending for pending in queue if pending.queued_id in ranked]
        for pending, queued_id in zip(slots, sorted((p.queued_id for p in slots), key=ranked.__getitem__)):
            pending.queued_id = queued_id

    async def cancel(
        self, session_id: str, *, reason: str | None = None, drop_queue: bool = True
    ) -> tuple[bool, int]:
        """Cancel ``session_id``'s active turn and (by default) its queue.

        Returns ``(cancelled, dropped)``. Dropping the queue also removes the
        session from the rotation, so no parked submission starts afterwards. A
        cancel that lands while a start is in flight -- before the session has
        installed a lease -- is recorded and honoured the moment ownership
        exists, so it can never be lost. ``dropped`` counts every durable or
        parked submission the cancel removes; the item an in-flight queue consume
        has already taken becomes a cancelled turn rather than a drop.
        """
        session = self._sessions.get(session_id)
        starting = self._starting.get(session_id)
        # Capture the session's own durable FIFO before dropping anything, so the
        # returned count reflects dropped submissions even for a session whose
        # queue is not mirrored in the supervisor's parked queue.
        durable_ids: set[str] = set()
        durable_depth = 0
        if session is not None and drop_queue:
            durable_ids = set(getattr(session, "queued_ids", ()) or ())
            durable_depth = getattr(session, "queue_depth", 0)
        parked_extra = 0
        if drop_queue:
            queue = self._queues.pop(session_id, None)
            parked = list(queue) if queue else []
            self._rotation = deque(name for name in self._rotation if name != session_id)
            # A parked pending that mirrors a durable submission is counted once
            # via the durable queue; the rest are supervisor-only submissions.
            parked_extra = sum(
                1
                for pending in parked
                if not pending.queued_id or pending.queued_id not in durable_ids
            )
        cancelled = False
        if session is not None and getattr(session, "active", False):
            with contextlib.suppress(Exception):
                session.cancel(reason, drop_queue=drop_queue)
                cancelled = True
            dropped = durable_depth + parked_extra
        elif starting is not None:
            # Start in flight: the lease does not exist yet, so cancelling the
            # session now would be lost when ``begin_turn`` installs a fresh
            # cancellation token. Record the request; ``_start`` cancels the turn
            # the moment it owns a lease and drops the queue then, so a queued
            # consume is neither stranded nor silently run.
            self._cancel_requested[session_id] = (
                reason or "cancelled",
                drop_queue,
                parked_extra,
            )
            cancelled = True
            if drop_queue:
                # Pre-honor estimate. Whether the in-flight queue consume has
                # taken its head (and so is not a drop) is only knowable once the
                # start resolves, so this can be one short if the start then
                # fails; ``_start`` reconciles the true count when it honours the
                # request and reports it on ``daemon.session_cancelled``.
                consumed = 1 if (starting.content is None and durable_depth > 0) else 0
                dropped = max(0, durable_depth - consumed) + parked_extra
            else:
                dropped = parked_extra
        elif drop_queue and session is not None and durable_depth:
            # No turn is in flight, but the session still holds persisted queued
            # input (the parked submission was consumed into its durable FIFO).
            # Drop it through the session so it emits ``input.dropped``; left
            # alone, a reload would rehydrate and execute an input a cancel
            # explicitly dropped.
            with contextlib.suppress(Exception):
                session.cancel(reason, drop_queue=True)
            dropped = durable_depth + parked_extra
        else:
            dropped = parked_extra
        if drop_queue and session_id not in self._active:
            # Nothing is in flight for this session and its queue is gone, so
            # the cached handle is no longer needed; drop it. A later ``submit``
            # re-adds it, so forgetting never breaks a session that returns.
            self.forget(session_id)
        self._maybe_idle()
        return cancelled, dropped

    def forget(self, session_id: str) -> bool:
        """Drop a session's cached handle when it has no active/queued work.

        Bounds supervisor memory: the daemon calls this after deleting a
        session, and a queue-dropping cancel calls it once no turn is in flight.
        It refuses while a turn is active or a queue is pending, because the
        scheduler still needs the handle to finish that work. Forgetting is safe
        because :meth:`submit` re-registers a session on its next submission, so
        an idle session that comes back is never broken.
        """
        if session_id in self._active or self.queued_for(session_id):
            return False
        self._sessions.pop(session_id, None)
        self._starting.pop(session_id, None)
        self._cancel_requested.pop(session_id, None)
        self._rotation = deque(name for name in self._rotation if name != session_id)
        return True

    async def wait_idle(self, *, timeout: float | None = None) -> None:
        """Wait until no turn is in flight and no queue holds pending work."""
        while self.running or self.queued:
            self._idle.clear()
            if not self.running and not self.queued:
                break
            if timeout is None:
                await self._idle.wait()
            else:
                await asyncio.wait_for(self._idle.wait(), timeout)

    async def aclose(self) -> None:
        """Cancel every in-flight turn, drop every queue, await the watchers."""
        for session_id in list(self._sessions):
            await self.cancel(session_id, reason="supervisor closing", drop_queue=True)
        watchers = [task for task in self._watchers.values() if not task.done()]
        for task in watchers:
            task.cancel()
        for task in watchers:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._watchers.clear()

    # -- scheduling --------------------------------------------------------

    async def _pump(self) -> None:
        """Start as many queued turns as the global cap allows, fairly."""
        async with self._lock:
            while self.running < self._max and self._rotation:
                session_id = self._rotation.popleft()
                queue = self._queues.get(session_id)
                if not queue:
                    continue
                if session_id in self._active:
                    # Its in-flight turn will re-add it to the rotation when it
                    # completes; re-queuing it here would spin this loop.
                    continue
                session = self._sessions.get(session_id)
                if session is None:
                    continue
                pending = queue.popleft()
                if not queue:
                    self._queues.pop(session_id, None)
                if not pending.queued_id or pending.queued_id in getattr(session, "queued_ids", (pending.queued_id,)):
                    await self._start(session_id, session, pending)
                if self._queues.get(session_id) and session_id not in self._rotation:
                    self._rotation.append(session_id)
            self._maybe_idle()

    async def _start(self, session_id: str, session: Any, pending: _Pending) -> bool:
        """Start one turn; mark the session active and watch it to completion."""
        self._active.add(session_id)
        self._starting[session_id] = pending
        try:
            await session.start_turn(pending.content, turn_id=pending.turn_id)
        except Exception as exc:  # noqa: BLE001 - a bad turn must not wedge the pump
            self._starting.pop(session_id, None)
            self._active.discard(session_id)
            requested = self._cancel_requested.pop(session_id, None)
            if requested is not None:
                reason, drop_queue, parked_extra = requested
                dropped = self._honor_cancel(
                    session, reason, drop_queue, parked_extra
                )
                self._emit(
                    "daemon.session_cancelled",
                    {
                        "session": session_id,
                        "turn_id": pending.turn_id,
                        "reason": reason,
                        "dropped": dropped,
                    },
                )
            message = redact_secrets(f"{type(exc).__name__}: {exc}")
            # A start that raised produced no producer and therefore no terminal
            # event; without one a ``run``/``chat``/JSONL follower would wait
            # forever. Persist a session-scoped ``turn.failed`` for the
            # pre-assigned turn id so every surface terminates.
            fail = getattr(session, "fail_turn", None)
            if callable(fail):
                with contextlib.suppress(Exception):
                    fail(pending.turn_id, message, reason="turn failed to start")
            self._emit(
                "daemon.session_failed",
                {
                    "session": session_id,
                    "turn_id": pending.turn_id,
                    "error": message,
                },
            )
            return False
        except BaseException:
            self._starting.pop(session_id, None)
            self._active.discard(session_id)
            self._cancel_requested.pop(session_id, None)
            raise
        self._starting.pop(session_id, None)
        self._emit(
            "daemon.session_scheduled",
            {"session": session_id, "turn_id": pending.turn_id},
        )
        watcher = asyncio.ensure_future(self._watch(session_id, session, pending.turn_id))
        self._watchers[pending.turn_id] = watcher
        # Honour a cancel that landed while the lease did not yet exist. The turn
        # now owns a lease, so it can be stopped and cancellation is never lost.
        requested = self._cancel_requested.pop(session_id, None)
        if requested is not None:
            reason, drop_queue, parked_extra = requested
            dropped = self._honor_cancel(session, reason, drop_queue, parked_extra)
            self._emit(
                "daemon.session_cancelled",
                {
                    "session": session_id,
                    "turn_id": pending.turn_id,
                    "reason": reason,
                    "dropped": dropped,
                },
            )
        return True

    def _honor_cancel(
        self,
        session: Any,
        reason: str,
        drop_queue: bool,
        parked_extra: int,
    ) -> int:
        """Cancel a session whose start was in flight; return the true drop count.

        The synchronous ``cancel`` cannot know whether the in-flight start will
        consume its queued head (it can still fail, be hook-blocked, or launch),
        so it returns a pre-honor estimate. Here the start has resolved, so the
        durable FIFO is read before and after ``session.cancel`` and the exact
        delta is added to the supervisor-only submissions already removed. This
        is the authoritative count reported on ``daemon.session_cancelled``.
        """
        durable = 0
        if session is not None and drop_queue:
            before = set(getattr(session, "queued_ids", ()) or ())
            with contextlib.suppress(Exception):
                session.cancel(reason, drop_queue=True)
            after = set(getattr(session, "queued_ids", ()) or ())
            durable = len(before - after)
        elif session is not None:
            with contextlib.suppress(Exception):
                session.cancel(reason, drop_queue=False)
        return parked_extra + durable

    async def _watch(self, session_id: str, session: Any, turn_id: str) -> None:
        """Await one detached turn, then free its slot and schedule the next."""
        try:
            with contextlib.suppress(Exception):
                await session.wait_turn(turn_id)
        finally:
            self._watchers.pop(turn_id, None)
            self._active.discard(session_id)
            if self._queues.get(session_id):
                # A finished session goes to the *back* of the rotation, so a
                # backlog on one session cannot starve the others: round-robin
                # fairness needs the just-finished session to yield the slot.
                self._rotation = deque(
                    name for name in self._rotation if name != session_id
                )
                self._rotation.append(session_id)
            with contextlib.suppress(Exception):
                await self._pump()
            # A finished turn with nothing queued leaves the handle unused; drop
            # the supervisor's reference so it is not retained forever. The
            # active/queued constraints mean a session this just re-scheduled
            # (or one that is still winding down) is left alone.
            self.forget(session_id)

    def _maybe_idle(self) -> None:
        if not self.running and not self.queued:
            self._idle.set()


__all__ = ["Supervisor"]
