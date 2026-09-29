"""SessionManager: open/fork/replay/list/archive/delete/export (STATE_PLAN §5.1).

Sessions live in the shared SQLite state database (:mod:`nexus.session.db`):
one row per session in the ``sessions`` table (denormalized title/activity/
message-count/parent/fork columns updated inside every append transaction),
records in ``records``, a derived cache in ``snapshots``. This manager is the
thin orchestration layer over that store: it owns the in-process
:class:`~nexus.session.session.Session` handle cache and the cross-process
turn-exclusivity lock (:class:`~nexus.session.lock.SessionLock`), and
translates every operation into one bounded SQL transaction.

Concurrency contract
-------------------
Turn execution holds the exclusive ``flock``. ``fork``/``replay``/``list``/
``export`` are read-only: they read the database directly with no lock at all,
because a WAL reader always observes a consistent *committed* prefix and never
blocks (or is blocked by) a writer (STATE_PLAN §2.4) -- the old
"shared-lock-or-lock-free-read" dance the JSONL store needed is gone.

``delete``/``restore`` still take the exclusive lock non-blocking, so they fail
fast (``SessionBusy``) rather than racing a turn. Deletion **never** cancels a
turn: an active handle is refused, and the caller must cancel explicitly first.

Trash keeps a session's row and history in place with ``trash_id`` set (STATE_PLAN
§4.2): there is no longer a separate move-to-another-directory step, so the
crash-mid-move hazard the old store guarded against (staging directories,
``meta.json`` recovery sweeps) cannot occur -- the whole transition is one SQL
transaction. One consequence: an id cannot be reused for a *new* session while
its old occupant sits in trash (the primary key is the same row); restore or
``purge_expired`` first.

Secrecy boundary
----------------
Only session state (the database's session/record/snapshot rows) is ever read
or mutated. Lock files, workspace configuration, credentials, and environment
variables are never touched, so an export or a summary cannot leak anything
beyond the session content contract.

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

import asyncio
import contextlib
import secrets
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

import msgspec

from ..config.paths import project_key
from ..core.turn import TurnLimits
from ..errors import SessionBusy, SessionError
from ..events import Event
from . import export as export_mod
from . import snapshot as snapshot_mod
from .db import SqliteSessionStore, StateDatabase
from .ids import validate_session_id
from .lock import SessionLock
from .session import DEFAULT_EVENT_BUFFER, Session
from .store import EventRecord, ReadResult

#: Length of the random suffix appended to a fork's generated child id.
_FORK_TOKEN_BYTES = 6

#: Trash metadata format version (kept for the :class:`TrashRecord` wire shape).
TRASH_VERSION = 1

#: Default retention: one week, matching the extension trash policy (plan §2.3).
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60

#: Bound on one ``archive_stale`` sweep, matching the old file-store cap.
_ARCHIVE_MAX_SWEEP = 500

SessionState = Literal["idle", "running", "awaiting_input", "awaiting_permission"]


class SessionSummary(msgspec.Struct, frozen=True):
    """Transport-neutral projection of one session (plan §14.4).

    Every field is a primitive, so the same summary can be encoded to JSON for a
    wire protocol, rendered in a terminal, or compared in a test with no
    dependency on the live handle. ``last_seq`` lets a view diff what it has
    already rendered against the current log; ``viewers`` and ``state`` make
    background work legible without polling the session itself.
    """

    id: str
    title: str = ""
    state: SessionState = "idle"
    last_activity: float = 0.0
    last_seq: int = 0
    viewers: int = 0
    message_count: int = 0
    created_at: float = 0.0
    parent_id: str = ""
    fork_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)


class TrashRecord(msgspec.Struct, frozen=True):
    """Durable metadata for one trashed session (retention + restore)."""

    trash_id: str
    session_id: str
    trashed_at: float
    delete_after: float
    files: tuple[str, ...] = ()
    title: str = ""
    last_seq: int = 0
    reason: str = ""
    v: int = TRASH_VERSION

    @property
    def expired(self) -> bool:
        return self.delete_after <= time.time()

    def to_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)


class ArchiveRecord(msgspec.Struct, frozen=True):
    """Durable archive metadata stored outside the append-only session log."""

    session_id: str
    archived_at: float
    reason: Literal["auto", "user"] = "user"

    def to_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)


def _state_for(handle: Session | None) -> SessionState:
    if handle is None:
        return "idle"
    if handle.pending_permissions:
        return "awaiting_permission"
    if handle.active:
        return "running"
    if handle.queue_depth > 0:
        return "awaiting_input"
    return "idle"


class SessionManager:
    """Owns one project's session rows and hands out :class:`Session` handles."""

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        db: StateDatabase | None = None,
        project: str | Path | None = None,
        namespace: str = "main",
        store: SqliteSessionStore | None = None,
        lock_dir: str | Path | None = None,
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
        hooks: Callable[[], Any] | None = None,
        trash_dir: str | Path | None = None,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    ):
        if store is not None:
            self.store = store
            self.directory = Path(directory) if directory is not None else Path(
                getattr(store, "lock_dir", None) or "."
            )
        elif db is not None:
            if project is None:
                raise ValueError("project is required when db is given")
            project_id = project_key(project)
            resolved_lock_dir = Path(lock_dir) if lock_dir is not None else Path(project)
            self.store = SqliteSessionStore(
                db, project_id, namespace, root=str(project), lock_dir=resolved_lock_dir
            )
            self.directory = Path(directory) if directory is not None else Path(project)
        else:
            # Test-compatibility path: a plain directory opens a private,
            # per-directory database at ``<directory>/nexus.db`` (STATE_PLAN §5.1).
            base = Path(directory) if directory is not None else Path(".")
            base.mkdir(parents=True, exist_ok=True)
            private_db = StateDatabase(base / "nexus.db")
            project_id = project_key(base)
            resolved_lock_dir = Path(lock_dir) if lock_dir is not None else base
            self.store = SqliteSessionStore(
                private_db, project_id, namespace, root=str(base), lock_dir=resolved_lock_dir
            )
            self.directory = base
        # ``trash_dir`` is accepted for constructor compatibility with the
        # pre-STATE_PLAN file store; trash is now rows in place (STATE_PLAN
        # §4.2), so this has no functional effect and exists only for callers
        # that still pass it.
        self._trash_dir_hint = Path(trash_dir) if trash_dir is not None else None
        if not isinstance(retention_seconds, (int, float)) or retention_seconds <= 0:
            raise ValueError("retention_seconds must be a positive number")
        self._retention_seconds = float(retention_seconds)
        # Loop dependencies are opaque callables forwarded to each handle, so
        # this layer stays free of context/router/tools imports.
        self._assemble = assemble
        self._provider_for = provider_for
        self._limits = limits
        self._tools = tools
        self._attended = bool(attended)
        self._event_buffer = event_buffer
        self._snapshot_every = snapshot_every
        self._unattended_decision = unattended_decision
        self._auto_start_queued = auto_start_queued
        self._ensure_ready = ensure_ready
        self._turn_cleanup = turn_cleanup
        self._hooks = hooks
        #: One live :class:`Session` handle per id, so every caller that reaches
        #: the same session through this manager shares one bus, presence count,
        #: input queue, and active turn.
        self._handles: dict[str, Session] = {}
        self._handles_lock = threading.RLock()

    def exists(self, session_id: str) -> bool:
        return self.store.exists(session_id)

    def queued_depth(self, session_id: str) -> int:
        """Pending input depth for a live session handle, or 0 when not cached.

        A host surface uses this to refuse a delete that would strand a durable
        ``input.queued`` submission. Read-only: it never opens or creates a handle.
        """
        session_id = validate_session_id(session_id)
        handle = self._live_handle(session_id)
        return handle.queue_depth if handle is not None else 0

    @property
    def trash_dir(self) -> Path:
        """Legacy display path; trash is DB rows now (kept for compatibility)."""
        return self._trash_dir_hint if self._trash_dir_hint is not None else self.directory / "trash"

    @property
    def retention_seconds(self) -> float:
        """How long a trashed session is retained before ``purge_expired``."""
        return self._retention_seconds

    # -- open / handles ------------------------------------------------

    def open(
        self,
        session_id: str,
        *,
        create: bool = True,
        recover: bool = True,
        migrate: bool = True,
    ) -> Session:
        """Open a session, reusing the live handle for an id when one exists.

        ``create=False`` raises when the session has no row. With ``recover``
        the handle's dangling-tool crash recovery runs (idempotently) before it
        is returned, whether it was just built or already cached. ``migrate``
        is accepted for compatibility; legacy ``.json``/JSONL migration now
        happens once, up front, via :mod:`nexus.session.import_legacy`.

        Exactly one :class:`Session` handle exists per id per manager, so
        repeated ``Runtime.session(id)`` calls share the same bus, presence
        count, input queue, and active turn.
        """
        session_id = validate_session_id(session_id)
        # Opening is the resume operation: remove the archive marker before a
        # caller starts work or attaches to the session.
        self.unarchive(session_id)
        while True:
            with self._handles_lock:
                cached = self._handles.get(session_id)
                if cached is not None and (
                    getattr(cached, "_session_ended", False)
                    or getattr(cached, "retired", False)
                ):
                    # A closed or retired handle must never be handed out again.
                    self._handles.pop(session_id, None)
                    cached = None
                if cached is not None:
                    if recover:
                        cached.recover_dangling_tool_uses()
                    return cached
                if self.store.exists(session_id):
                    session = self._build_handle(session_id)
                    if recover:
                        session.recover_dangling_tool_uses()
                    self._handles[session_id] = session
                    return session
                if not create:
                    raise SessionError(f"Session {session_id!r} does not exist")
            # No cached handle and no row: create outside the lock. SQL's own
            # primary key is the concurrency authority here -- a losing racer's
            # ``INSERT OR IGNORE`` is simply a no-op, and the loop above then
            # finds the row the winner published.
            self.store.create(session_id)

    def _build_handle(self, session_id: str) -> Session:
        return Session(
            session_id,
            store=self.store,
            lock=SessionLock(self.store.lock_path(session_id)),
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
            hooks=self._hooks,
        )

    def evict(self, session_id: str) -> Session | None:
        """Drop the cached live handle for an idle id; the row is untouched.

        Refuses (``SessionBusy``) to evict a handle that is in use.
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
            evicted = self._handles.pop(session_id)
        self._schedule_session_end(evicted)
        return evicted

    def close(self, session_id: str) -> bool:
        """Alias for :meth:`evict` returning whether a handle was dropped."""
        return self.evict(session_id) is not None

    def close_all(self) -> list[Session]:
        """Evict every cached handle; returns them in insertion order."""
        with self._handles_lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            self._schedule_session_end(handle)
        return handles

    @staticmethod
    def _schedule_session_end(session: Session) -> None:
        """Best-effort schedule of ``session.aclose()`` on the running loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(session.aclose())

    async def aclose_session(self, session_id: str) -> bool:
        """Evict one idle session and await its ``SessionEnd`` hook/bus close."""
        session = self.evict(session_id)
        if session is None:
            return False
        with contextlib.suppress(Exception):
            await session.aclose()
        return True

    async def aclose_all(self) -> list[Session]:
        """Evict every handle and await each ``SessionEnd`` before returning."""
        handles = self.close_all()
        for handle in handles:
            with contextlib.suppress(Exception):
                await handle.aclose()
        return handles

    @property
    def live_sessions(self) -> tuple[str, ...]:
        """Ids with a cached live handle, in insertion order."""
        with self._handles_lock:
            return tuple(self._handles)

    def _live_handle(self, session_id: str) -> Session | None:
        with self._handles_lock:
            return self._handles.get(session_id)

    # -- archive ---------------------------------------------------------

    def archived(self) -> list[ArchiveRecord]:
        """Return archive metadata newest first."""
        rows = self.store.archived_rows()
        result = [
            ArchiveRecord(
                session_id=row["session_id"],
                archived_at=row["archived_at"],
                reason=row["reason"] or "user",
            )
            for row in rows
        ]
        return sorted(result, key=lambda row: (-row.archived_at, row.session_id))

    def archive(self, session_id: str, reason: Literal["auto", "user"] = "user") -> ArchiveRecord:
        """Mark an existing session archived without changing its log."""
        session_id = validate_session_id(session_id)
        if not isinstance(reason, str) or reason not in {"auto", "user"}:
            raise ValueError("archive reason must be 'auto' or 'user'")
        if not self.store.exists(session_id):
            raise SessionError(f"Session {session_id!r} does not exist")
        if self._has_pending_work(session_id):
            raise SessionBusy(f"Session {session_id!r} has running or pending work")
        lock = SessionLock(self.store.lock_path(session_id))
        with lock.exclusive(blocking=False):
            row = self.store.archive(session_id, reason)
            if row is None:
                raise SessionError(f"Session {session_id!r} does not exist")
            return ArchiveRecord(
                session_id=row["session_id"], archived_at=row["archived_at"], reason=row["reason"]
            )

    def unarchive(self, session_id: str) -> bool:
        """Remove one archive marker; return whether a marker was present."""
        session_id = validate_session_id(session_id)
        return self.store.unarchive(session_id)

    def archive_stale(
        self, *, now: float | None = None, older_than: float
    ) -> list[ArchiveRecord]:
        """Archive up to 500 old, idle, unopened sessions."""
        if isinstance(older_than, bool) or not isinstance(older_than, (int, float)) or older_than < 0:
            raise ValueError("older_than must be a non-negative number of seconds")
        moment = time.time() if now is None else float(now)
        threshold = moment - float(older_than)
        open_ids = set(self.live_sessions)
        candidates = []
        for session_id in self.store.stale_candidates(threshold=threshold, limit=_ARCHIVE_MAX_SWEEP):
            if session_id in open_ids:
                continue
            try:
                summary = self.summary(session_id)
            except (SessionError, OSError, ValueError):
                continue
            if (
                summary.state == "idle"
                and summary.viewers == 0
                and summary.last_activity <= threshold
                and not self._has_pending_work(session_id)
            ):
                candidates.append(session_id)
        archived = []
        for session_id in candidates:
            try:
                archived.append(self.archive(session_id, "auto"))
            except (SessionBusy, SessionError):
                continue
        return archived

    def _has_pending_work(self, session_id: str) -> bool:
        """Inspect durable queue/approval/turn markers without opening a handle."""
        try:
            events = tuple(self._consistent_read(session_id).events())
        except (SessionError, OSError, ValueError):
            return True
        queued: set[str] = set()
        approvals: set[str] = set()
        running = False
        for event in events:
            data = event.data if isinstance(event.data, dict) else {}
            if event.type == "turn.started":
                running = True
            elif event.type in {"turn.completed", "turn.failed", "turn.cancelled"}:
                running = False
            elif event.type == "input.queued":
                queued_id = data.get("queued_id")
                if isinstance(queued_id, str) and queued_id:
                    queued.add(queued_id)
            elif event.type in {"input.consumed", "input.dropped"}:
                queued_id = data.get("queued_id")
                if isinstance(queued_id, str):
                    queued.discard(queued_id)
            elif event.type == "permission.requested":
                request_id = data.get("id")
                if isinstance(request_id, str) and request_id:
                    approvals.add(request_id)
            elif event.type == "permission.resolved":
                request_id = data.get("id")
                if isinstance(request_id, str):
                    approvals.discard(request_id)
        return running or bool(queued) or bool(approvals)

    # -- list / summary ----------------------------------------------------

    def list(self, *, include_archived: bool = False) -> list[SessionSummary]:
        """Summarize every session, newest activity first.

        One indexed query: the denormalized ``sessions`` row columns (title,
        activity, message count, parent/fork) are kept current inside every
        append transaction, so no session log is read here.
        """
        rows = self.store.list_rows(include_archived=include_archived)
        summaries = [self._summary_from_row(row) for row in rows]
        summaries.sort(key=lambda item: (-item.last_activity, item.id))
        return summaries

    def summary(self, session_id: str) -> SessionSummary:
        """Project one session into a transport-neutral :class:`SessionSummary`."""
        session_id = validate_session_id(session_id)
        row = self.store.session_row(session_id)
        if row is None or row.get("trash_id") is not None:
            raise SessionError(f"Session {session_id!r} does not exist")
        return self._summary_from_row(row)

    def _summary_from_row(self, row: dict[str, Any]) -> SessionSummary:
        handle = self._live_handle(row["id"])
        return SessionSummary(
            id=row["id"],
            title=row["title"] or "",
            state=_state_for(handle),
            last_activity=row["last_activity"] or 0.0,
            last_seq=row["last_seq"] or 0,
            viewers=handle.viewers if handle is not None else 0,
            message_count=row["message_count"] or 0,
            created_at=row["created_at"] or 0.0,
            parent_id=row["parent_id"] or "",
            fork_seq=row["fork_seq"] or 0,
        )

    # -- export ------------------------------------------------------------

    def export(self, session_id: str, *, format: str = "json") -> str:
        """Render a consistent log prefix as ``json``, ``markdown``, or ``jsonl``."""
        session_id = validate_session_id(session_id)
        if not self.store.exists(session_id):
            raise SessionError(f"Session {session_id!r} does not exist")
        read = self._consistent_read(session_id)
        meta = self.summary(session_id).to_dict()
        return export_mod.render(session_id, read, format=format, meta=meta)

    # -- delete / trash / restore ------------------------------------------

    def delete(
        self, session_id: str, *, force: bool = False, reason: str = ""
    ) -> TrashRecord:
        """Move a session into trash (in place; STATE_PLAN §4.2), one transaction.

        Refuses a session with an active turn or pending queued input **even
        with** ``force``: deletion never cancels a turn or drops a durable
        submission. A session with live viewers is refused unless
        ``force=True`` (which detaches them; it does not cancel anything). The
        exclusive session lock is taken non-blocking, so a turn running in
        another process is refused too.
        """
        session_id = validate_session_id(session_id)
        handle = self._live_handle(session_id)
        self._check_deletable(session_id, handle, force=force)
        if handle is None and not self.store.exists(session_id):
            raise SessionError(f"Session {session_id!r} does not exist")
        lock = SessionLock(self.store.lock_path(session_id))
        try:
            lock.acquire(shared=False, blocking=False)
        except SessionBusy as exc:
            raise SessionBusy(
                f"Session {session_id!r} is locked by a running turn; refusing to delete"
            ) from exc
        try:
            handle = self._live_handle(session_id)
            self._check_deletable(session_id, handle, force=force, viewers_checked=True)
            if handle is not None:
                handle.retire()
            try:
                row = self.store.trash(
                    session_id, reason=reason, retention_seconds=self._retention_seconds
                )
            except BaseException:
                if handle is not None:
                    handle.unretire()
                raise
            if row is None:
                if handle is not None:
                    handle.unretire()
                raise SessionError(f"Session {session_id!r} does not exist")
        finally:
            lock.release()
        with self._handles_lock:
            evicted = self._handles.pop(session_id, None)
        if evicted is not None:
            self._schedule_session_end(evicted)
        return TrashRecord(
            trash_id=row["trash_id"],
            session_id=row["session_id"],
            trashed_at=row["trashed_at"],
            delete_after=row["delete_after"],
            files=(),
            title=row["title"] or "",
            last_seq=row["last_seq"] or 0,
            reason=reason,
        )

    def _check_deletable(
        self, session_id: str, handle: Session | None, *, force: bool, viewers_checked: bool = False
    ) -> None:
        if handle is not None and handle.active:
            raise SessionBusy(
                f"Session {session_id!r} has an active turn; cancel it explicitly "
                "before deleting (delete never cancels a turn)"
            )
        if handle is not None and handle.queue_depth > 0:
            raise SessionBusy(
                f"Session {session_id!r} has {handle.queue_depth} queued input(s); "
                "drop them explicitly before deleting (delete never drops a queue)"
            )
        if not viewers_checked and handle is not None and handle.viewers > 0 and not force:
            raise SessionBusy(
                f"Session {session_id!r} has {handle.viewers} live viewer(s); "
                "pass force=True to delete and detach them"
            )

    def restore(self, trash_id: str) -> str:
        """Move a trashed session back; returns its id.

        ``trash_id`` may be the trash entry's id or the session id itself. The
        primary key is the session row: it never left the table, so a restore
        can never collide with a live session of the same id -- there is no
        second row that could have been created meanwhile.
        """
        if not isinstance(trash_id, str) or not trash_id:
            raise ValueError("trash_id must be a non-empty string")
        found = self.store.find_trash(trash_id)
        if found is None:
            raise SessionError(f"No trashed session {trash_id!r}")
        session_id = found["id"]
        lock = SessionLock(self.store.lock_path(session_id))
        try:
            lock.acquire(shared=False, blocking=False)
        except SessionBusy as exc:
            raise SessionBusy(
                f"Session {session_id!r} is locked; refusing to restore over it"
            ) from exc
        try:
            restored = self.store.restore(trash_id)
            if restored is None:
                raise SessionError(f"No trashed session {trash_id!r}")
            return restored
        finally:
            lock.release()

    def list_trashed(self) -> list[TrashRecord]:
        """Retention metadata for every trashed session, newest first."""
        records = [
            TrashRecord(
                trash_id=row["trash_id"],
                session_id=row["id"],
                trashed_at=row["trashed_at"] or 0.0,
                delete_after=row["trash_expires_at"] or 0.0,
                files=(),
                title=row["title"] or "",
                last_seq=row["last_seq"] or 0,
                reason=row["trash_reason"] or "",
            )
            for row in self.store.trashed_rows()
        ]
        records.sort(key=lambda item: (-item.trashed_at, item.trash_id))
        return records

    def purge_expired(self, *, now: float | None = None) -> list[str]:
        """Remove trash rows past ``delete_after``; returns their trash ids."""
        return self.store.purge_expired(now=now)

    # -- migration (compatibility thin wrapper) -----------------------------

    def migrate(self, session_id: str) -> None:
        """No-op: legacy ``.json``/JSONL migration now runs once at startup.

        Kept only so callers written against the pre-STATE_PLAN manager (which
        migrated lazily on open) do not need an unconditional guard. See
        :mod:`nexus.session.import_legacy`.
        """
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

        One transaction: the destination row, its copied record prefix, and its
        own ``session.forked`` marker event are all inserted together
        (STATE_PLAN §2.5), so a reader can never observe a forked session with
        history but no marker or vice versa. ``new_id`` (or a generated one)
        that already names a row -- live or trashed -- is refused.
        """
        source_id = validate_session_id(source_id)
        self.unarchive(source_id)
        if not self.store.exists(source_id):
            raise SessionError(f"Session {source_id!r} does not exist")
        read = self._consistent_read(source_id)
        boundary = read.next_seq if at_seq is None else _check_boundary(at_seq, read)
        prefix = [record for record in read.records if record.seq <= boundary]
        child_id = self._child_id(source_id, new_id)
        now = time.time()
        fork_seq = boundary + 1
        fork_event = Event(
            "session.forked",
            {"parent": source_id, "at_seq": boundary},
            seq=fork_seq,
            ts=now,
            session=child_id,
        )
        fork_record = EventRecord(seq=fork_seq, event=fork_event, ts=now)
        self.store.create_from_records(
            child_id, [*prefix, fork_record], parent_id=source_id, fork_seq=boundary
        )
        self._inherit_snapshot(source_id, child_id, read, boundary)
        return self.open(child_id, create=False, recover=False, migrate=False)

    def _inherit_snapshot(
        self, source_id: str, child_id: str, read: ReadResult, boundary: int
    ) -> None:
        if boundary <= 0:
            return
        valid = self.store.load_snapshot(source_id, read)
        if valid is None or valid.seq > boundary:
            return
        try:
            built = snapshot_mod.build_from_records(child_id, read.records, boundary)
            self.store.write_snapshot(child_id, built)
        except Exception:  # noqa: BLE001 - derived cache only
            pass

    def _child_id(self, source_id: str, new_id: str | None) -> str:
        if new_id is not None:
            candidate = validate_session_id(new_id)
            if self.store.row_exists(candidate):
                raise SessionError(f"Session {candidate!r} already exists")
            return candidate
        for _ in range(32):
            token = secrets.token_hex(_FORK_TOKEN_BYTES)
            prefix = source_id[: 80 - len(token) - 1]
            candidate = f"{prefix}-{token}"
            if not self.store.row_exists(candidate):
                return candidate
        raise SessionError("Could not generate a unique fork session id")  # pragma: no cover

    # -- replay ------------------------------------------------------------

    async def replay(self, session_id: str) -> AsyncIterator[Event]:
        """Yield persisted events in sequence order, read-only."""
        session_id = validate_session_id(session_id)
        if not self.store.exists(session_id):
            raise SessionError(f"Session {session_id!r} does not exist")
        result = self._consistent_read(session_id)
        for record in result.records:
            if isinstance(record, EventRecord):
                yield record.event

    # -- shared-read helper ------------------------------------------------

    def _consistent_read(self, session_id: str) -> ReadResult:
        """A consistent read: WAL never blocks on (or is blocked by) a writer."""
        return self.store.read(session_id)


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


__all__ = [
    "DEFAULT_RETENTION_SECONDS",
    "TRASH_VERSION",
    "SessionManager",
    "SessionState",
    "SessionSummary",
    "TrashRecord",
]
