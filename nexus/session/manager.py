"""SessionManager: open/migrate/recover/fork/replay (plan section 5.1).

``open`` validates the ID, migrates a legacy ``<id>.json`` v1 session on first
open, and (by default) runs dangling-tool recovery. ``fork`` branches an exact
log prefix into a new session. ``replay`` streams persisted events read-only for
a UI rebuild.

Deliberately **not** implemented in this packet: ``list``/``delete``. Phase 3's
exact scope is durability plus fork/replay, and both need product decisions
(what a ``SessionInfo`` exposes; whether delete is trash-and-retain or unlink;
whether delete refuses while a turn is active). They are deferred rather than
guessed, and there is no CLI surface for them here.

Concurrency contract
--------------------
Turn execution holds the exclusive ``flock``. ``fork``/``replay`` are read-only
and never wait on (and never disturb) an active writer: they try a non-blocking
*shared* lock and, if a writer holds the exclusive lock, fall back to a lock-free
read. Because the log is append-only and the reader tolerates a crash tail, that
yields a consistent valid prefix either way.

Event-loop affinity
-------------------
``open``/``evict``/``close_all`` are synchronous and thread-safe (guarded by an
``RLock``), so several threads may open or close handles concurrently. A live
:class:`~nexus.session.session.Session` handle, however, is bound to the single
asyncio event loop that runs its turns: its bus, input queue, and active turn
task are loop-bound. Drive one handle from one loop only. Do not start a turn on
one loop and await it from another, and do not share a handle between two loops;
open a second handle (or evict and reopen) instead.
"""
from __future__ import annotations

import secrets
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from ..core.turn import TurnLimits
from ..errors import SessionBusy, SessionError
from ..events import Event
from . import snapshot as snapshot_mod
from .ids import validate_session_id
from .lock import SessionLock
from .migrate import MigrationResult, migrate_session, should_migrate
from .session import DEFAULT_EVENT_BUFFER, Session
from .store import EventRecord, ReadResult, SessionStore

#: Length of the random suffix appended to a fork's generated child id.
_FORK_TOKEN_BYTES = 6


class SessionManager:
    """Owns a sessions directory and hands out :class:`Session` handles."""

    def __init__(
        self,
        directory: str | Path,
        *,
        store: SessionStore | None = None,
        assemble: Callable[[object], Any] | None = None,
        provider_for: Callable[..., Any] | None = None,
        limits: TurnLimits | Callable[[], TurnLimits] | None = None,
        tools: Callable[..., Any] | None = None,
        attended: bool = False,
        event_buffer: int = DEFAULT_EVENT_BUFFER,
        snapshot_every: int | Callable[[], int] | None = None,
        unattended_decision: object | None = None,
        auto_start_queued: bool = True,
        ensure_ready: Callable[[], Any] | None = None,
        turn_cleanup: Callable[[str, str], None] | None = None,
    ):
        self.directory = Path(directory)
        self.store = store if store is not None else SessionStore(self.directory)
        # Loop dependencies are opaque callables forwarded to each handle, so
        # this layer stays free of context/router/tools imports.
        self._assemble = assemble
        self._provider_for = provider_for
        self._limits = limits
        self._tools = tools
        self._attended = bool(attended)
        self._event_buffer = event_buffer
        # Snapshot cadence, forwarded to each handle. A callable lets the
        # integration packet read the effective config per turn without this
        # layer importing configuration.
        self._snapshot_every = snapshot_every
        # Decision applied when the last viewer leaves with an approval pending.
        # A plain value (string/Decision) so this layer stays tools-agnostic.
        self._unattended_decision = unattended_decision
        # Whether a completed turn auto-consumes the next queued submission.
        self._auto_start_queued = auto_start_queued
        # Opaque readiness/cleanup seams forwarded to each handle (a runtime
        # bootstrapping extensions and dropping session/turn-local state).
        self._ensure_ready = ensure_ready
        self._turn_cleanup = turn_cleanup
        #: One live :class:`Session` handle per id, so every caller that reaches
        #: the same session through this manager shares one bus, presence count,
        #: input queue, and active turn. Guarded for thread safety because
        #: ``open`` is synchronous and may be called from several threads (or
        #: several event loops); the asyncio side is naturally serialized by it.
        self._handles: dict[str, Session] = {}
        self._handles_lock = threading.RLock()

    def path(self, session_id: str) -> Path:
        return self.store.log_path(session_id)

    def exists(self, session_id: str) -> bool:
        return self.store.exists(session_id)

    def open(
        self,
        session_id: str,
        *,
        create: bool = True,
        recover: bool = True,
        migrate: bool = True,
    ) -> Session:
        """Open a session, reusing the live handle for an id when one exists.

        ``create=False`` raises when the session has no log. With ``recover`` the
        handle's dangling-tool crash recovery runs (idempotently) before it is
        returned, whether it was just built or already cached.

        Exactly one :class:`Session` handle exists per id per manager, so
        repeated ``Runtime.session(id)`` calls share the same bus, presence
        count, input queue, and active turn. Migration and recovery are both
        idempotent, so re-opening never duplicates their effects.
        """
        session_id = validate_session_id(session_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        if migrate:
            self.migrate(session_id)
        with self._handles_lock:
            cached = self._handles.get(session_id)
            if cached is not None:
                # Re-opening an already-live handle still honors ``recover``:
                # crash recovery is idempotent and never writes while this handle
                # (or another process) holds the exclusive turn lock.
                if recover:
                    cached.recover_dangling_tool_uses()
                return cached
            if not self.store.exists(session_id):
                if not create:
                    raise SessionError(f"Session {session_id!r} does not exist")
                # Create only after migration so a legacy file is never shadowed.
                self.store.create(session_id)
            session = Session(
                session_id,
                store=self.store,
                assemble=self._assemble,
                provider_for=self._provider_for,
                limits=self._limits,
                tools=self._tools,
                attended=self._attended,
                event_buffer=self._event_buffer,
                snapshot_every=self._snapshot_every,
                unattended_decision=self._unattended_decision,
                auto_start_queued=self._auto_start_queued,
                ensure_ready=self._ensure_ready,
                turn_cleanup=self._turn_cleanup,
            )
            if recover:
                session.recover_dangling_tool_uses()
            self._handles[session_id] = session
            return session

    def evict(self, session_id: str) -> Session | None:
        """Drop the cached live handle for an idle id; the log is untouched.

        The eviction seam for a host that needs to release or replace a handle
        (for example before reloading it with new wiring). Fork/replay never
        depend on a cached handle, so evicting can never corrupt them. Returns
        the evicted handle, or ``None`` when the id was not cached.

        Refuses (``SessionBusy``) to evict a handle that is in use — one with an
        active turn or live subscribers — because dropping it would orphan the
        turn task or leave viewers attached to a bus nothing owns. Cancel the
        turn / detach the views first, or use :meth:`close_all` at shutdown.
        """
        session_id = validate_session_id(session_id)
        with self._handles_lock:
            session = self._handles.get(session_id)
            if session is None:
                return None
            if session.active or session.viewers > 0:
                raise SessionBusy(
                    f"Session {session_id!r} is in use "
                    f"(active={session.active}, viewers={session.viewers}); "
                    "refusing to evict"
                )
            return self._handles.pop(session_id)

    def close(self, session_id: str) -> bool:
        """Alias for :meth:`evict` returning whether a handle was dropped."""
        return self.evict(session_id) is not None

    def close_all(self) -> list[Session]:
        """Evict every cached handle; returns them in insertion order.

        This is the shutdown seam and deliberately **forces** eviction even for
        handles that are active or viewed; the caller is expected to have torn
        those down first.
        """
        with self._handles_lock:
            handles = list(self._handles.values())
            self._handles.clear()
            return handles

    @property
    def live_sessions(self) -> tuple[str, ...]:
        """Ids with a cached live handle, in insertion order."""
        with self._handles_lock:
            return tuple(self._handles)

    def migrate(self, session_id: str) -> MigrationResult | None:
        """Migrate one session if needed, serialized by the session lock."""
        session_id = validate_session_id(session_id)
        if not should_migrate(self.directory, session_id):
            return None
        lock = SessionLock.for_session(self.directory, session_id)
        try:
            with lock.exclusive(blocking=False):
                return migrate_session(self.directory, session_id)
        except SessionBusy:
            # Another opener is migrating or running. If it finished, we are
            # done; otherwise surface the contention instead of corrupting.
            if should_migrate(self.directory, session_id):
                raise
            return None

    # -- fork --------------------------------------------------------------

    def fork(
        self,
        source_id: str,
        at_seq: int | None = None,
        *,
        new_id: str | None = None,
    ) -> Session:
        """Branch ``source_id`` at ``at_seq`` (default: the end) into a new log.

        The destination receives the source's records **through** the boundary
        with their event/message payloads, ids, timestamps, and sequence numbers
        preserved. Its next append therefore continues monotonically from the
        boundary, and the two sessions diverge independently afterwards.

        ``at_seq`` must be an existing record boundary (or ``None`` for the last
        one); ``0``, a value past the end, and a non-boundary value are rejected.
        When ``new_id`` is omitted a collision-resistant child id is generated;
        an explicit or generated id that already has session artifacts is never
        overwritten. A snapshot is built for the child only when the source has a
        valid snapshot at or before the boundary; fork never fails because the
        derived snapshot cache could not be written.
        """
        source_id = validate_session_id(source_id)
        if not self.store.exists(source_id):
            raise SessionError(f"Session {source_id!r} does not exist")
        read = self._consistent_read(source_id)
        boundary = read.next_seq if at_seq is None else _check_boundary(at_seq, read)
        records = [record for record in read.records if record.seq <= boundary]
        child_id = self._child_id(source_id, new_id)
        self.store.create_from_records(child_id, records)
        self._inherit_snapshot(source_id, child_id, read, records, boundary)
        # ``recover=False`` keeps the copied prefix byte-for-byte exact; a
        # dangling tool use is recovered on the next ordinary ``open``.
        return self.open(child_id, create=False, recover=False, migrate=False)

    def _inherit_snapshot(
        self,
        source_id: str,
        child_id: str,
        read: ReadResult,
        records: list,
        boundary: int,
    ) -> None:
        if boundary <= 0:
            return
        valid = snapshot_mod.load(self.directory, source_id, read)
        if valid is None or valid.seq > boundary:
            return
        try:
            built = snapshot_mod.build_from_records(child_id, records, boundary)
            snapshot_mod.write(
                self.directory, child_id, built, fsync=self.store.fsync
            )
        except OSError:
            # Derived cache only; the copied log is the authoritative result.
            pass

    def _child_id(self, source_id: str, new_id: str | None) -> str:
        if new_id is not None:
            candidate = validate_session_id(new_id)
            if self._artifacts_exist(candidate):
                raise SessionError(f"Session {candidate!r} already exists")
            return candidate
        for _ in range(32):
            token = secrets.token_hex(_FORK_TOKEN_BYTES)
            prefix = source_id[: 80 - len(token) - 1]
            candidate = f"{prefix}-{token}"
            if not self._artifacts_exist(candidate):
                return candidate
        raise SessionError("Could not generate a unique fork session id")  # pragma: no cover

    def _artifacts_exist(self, session_id: str) -> bool:
        base = self.directory / session_id
        return any(
            candidate.exists()
            for candidate in (
                base.with_suffix(".jsonl"),
                base.with_suffix(".json"),
                base.with_suffix(".snap.json"),
            )
        )

    # -- replay ------------------------------------------------------------

    async def replay(self, session_id: str) -> AsyncIterator[Event]:
        """Yield persisted events in sequence order, read-only.

        Only events are yielded; their ids, timestamps, types (including unknown
        types), and order are exactly as persisted. No migration or recovery
        runs, so replay has no log side effects.
        """
        session_id = validate_session_id(session_id)
        if not self.store.exists(session_id):
            raise SessionError(f"Session {session_id!r} does not exist")
        result = self._consistent_read(session_id)
        for record in result.records:
            if isinstance(record, EventRecord):
                yield record.event

    # -- shared-read helper ------------------------------------------------

    def _consistent_read(self, session_id: str) -> ReadResult:
        """Read a consistent log prefix without disrupting an active writer.

        A non-blocking shared lock is attempted first. If a turn holds the
        exclusive lock, fall back to a lock-free read: the append-only log plus
        crash-tail tolerance still yields a valid prefix, and the writer is never
        made to fail or wait.
        """
        lock = SessionLock.for_session(self.directory, session_id)
        try:
            lock.acquire(shared=True, blocking=False)
        except SessionBusy:
            return self.store.read(session_id)
        try:
            return self.store.read(session_id)
        finally:
            lock.release()


def _check_boundary(at_seq: object, read: ReadResult) -> int:
    if type(at_seq) is not int or at_seq < 1:
        raise ValueError("at_seq must be a positive integer")
    if read.next_seq == 0:
        raise SessionError("cannot fork an empty session at a sequence")
    if at_seq > read.next_seq:
        raise SessionError(f"at_seq {at_seq} is beyond the log end {read.next_seq}")
    if not any(record.seq == at_seq for record in read.records):
        raise SessionError(f"at_seq {at_seq} is not a record boundary")
    return at_seq


__all__ = ["SessionManager"]
