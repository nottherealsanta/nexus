"""SessionManager: open/migrate/recover/fork/replay/list/delete/export.

``open`` validates the ID, migrates a legacy ``<id>.json`` v1 session on first
open, and (by default) runs dangling-tool recovery. ``fork`` branches an exact
log prefix into a new session. ``replay`` streams persisted events read-only for
a UI rebuild. ``list`` projects every session into a transport-neutral
:class:`SessionSummary`. ``delete`` moves a session's artifacts atomically into
``<trash>/`` with retention metadata (never cancelling a live turn). ``export``
renders a consistent prefix as JSON, Markdown, or JSONL.

Concurrency contract
-------------------
Turn execution holds the exclusive ``flock``. ``fork``/``replay``/``list``/
``export`` are read-only and never wait on (and never disturb) an active writer:
they try a non-blocking *shared* lock and, if a writer holds the exclusive lock,
fall back to a lock-free read. Because the log is append-only and the reader
tolerates a crash tail, that yields a consistent valid prefix either way.

``delete``/``restore`` take the exclusive lock non-blocking, so they fail fast
(``SessionBusy``) rather than racing a turn. Deletion **never** cancels a turn:
an active handle is refused, and the caller must cancel explicitly first.

Secrecy boundary
----------------
Only session artifacts are ever read or moved: the ``.jsonl`` log, its derived
``.snap.json`` snapshot, a legacy ``.json``, and a ``.v1.bak`` backup. Lock
files, workspace configuration, credentials, and environment variables are never
touched, so an export or a summary cannot leak anything beyond the session
content contract.

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
import os
import secrets
import shutil
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

import msgspec

from ..core.turn import TurnLimits
from ..errors import SessionBusy, SessionError
from ..events import Event
from . import export as export_mod
from . import snapshot as snapshot_mod
from .ids import is_valid_session_id, validate_session_id
from .lock import SessionLock, TrashLock
from .migrate import MigrationResult, migrate_session, should_migrate
from .session import DEFAULT_EVENT_BUFFER, Session
from .store import EventRecord, ReadResult, SessionStore

#: Length of the random suffix appended to a fork's generated child id.
_FORK_TOKEN_BYTES = 6

#: Trash directory name (a sibling of the sessions directory).
_TRASH_DIRNAME = "trash"

#: Trash metadata document name inside each trashed entry.
_TRASH_META = "meta.json"

#: Prefix for a trash staging directory that has not been atomically published.
_TRASH_STAGING_PREFIX = ".staging-"

#: Trash metadata format version.
TRASH_VERSION = 1

#: Default retention: one week, matching the extension trash policy (plan §2.3).
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60

#: How long ``open(create=True)`` waits for a competing creator's session lock
#: before refusing. A creator holds the lock only across the atomic empty-log
#: create, so a losing brand-new opener observes the winner's log instead of
#: failing with a spurious ``SessionBusy``.
_CREATE_LOCK_WAIT_SECONDS = 1.0

#: Poll interval while waiting for the create lock.
_CREATE_LOCK_POLL_SECONDS = 0.01

#: How long the create path waits for a transient, *unrelated* trash lock (a
#: short recovery sweep, another session's delete/purge) before refusing. A
#: genuinely stuck lock still fails closed after this bound: the wait is short
#: and bounded, never unbounded. Acquiring the lock always re-runs the crash
#: sweep and re-evaluates ``should_migrate`` under it, so waiting can never let a
#: staged authoritative log be shadowed.
_TRASH_LOCK_WAIT_SECONDS = 1.0

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


def _is_safe_name(value: object) -> bool:
    """Whether ``value`` is a single, non-traversing path component."""
    if not isinstance(value, str) or not value or value in (".", ".."):
        return False
    if "\x00" in value or "/" in value or "\\" in value:
        return False
    return os.path.basename(value) == value


def _is_safe_trash_id(value: object) -> bool:
    """Whether ``value`` is a single component drawn from the generated alphabet.

    Session trash ids are ``<session-id>-<hex>``; the session-id grammar is
    ``[A-Za-z0-9_-]`` and the suffix is hex, so this mirrors
    :meth:`SessionManager._trash` exactly. A value outside this shape was not
    written by this manager and is refused rather than trusted.
    """
    if not isinstance(value, str) or not 0 < len(value) <= 128:
        return False
    if value in (".", ".."):
        return False
    if "/" in value or "\\" in value or "\x00" in value:
        return False
    if os.path.basename(value) != value:
        return False
    return all(ch.isalnum() or ch in "-_." for ch in value)


def _session_artifact_names(session_id: str) -> frozenset[str]:
    """The exact artifact filenames a session id may restore."""
    base = Path(session_id)
    return frozenset(
        path.name
        for path in (
            base.with_suffix(".jsonl"),
            base.with_suffix(".snap.json"),
            base.with_suffix(".json"),
            base.with_suffix(".v1.bak"),
        )
    )


def _trustworthy_trash_record(meta: TrashRecord) -> bool:
    """Whether an on-disk session trash record passes every path-safety check."""
    if not _is_safe_trash_id(meta.trash_id):
        return False
    if not is_valid_session_id(meta.session_id):
        return False
    # A record with no artifacts is not one this manager could have written (it
    # refuses a session with no artifacts to move) and a restore from it would
    # silently move nothing while reporting success. Refuse it.
    if not meta.files:
        return False
    if not all(_is_safe_name(name) for name in meta.files):
        return False
    return not (set(meta.files) - _session_artifact_names(meta.session_id))


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
        hooks: Callable[[], Any] | None = None,
        trash_dir: str | Path | None = None,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    ):
        self.directory = Path(directory)
        self.store = store if store is not None else SessionStore(self.directory)
        # Trash is a sibling of the sessions directory, so the production layout
        # is ``.nexus/sessions`` -> ``.nexus/trash``. An explicit override keeps
        # tests hermetic (and lets a host relocate it).
        self._trash_dir = (
            Path(trash_dir)
            if trash_dir is not None
            else self.directory.parent / _TRASH_DIRNAME
        )
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
        # Optional provider of the current manifest's hook runner, forwarded to
        # each handle so session-level lifecycle hooks run against the pinned set.
        self._hooks = hooks
        #: One live :class:`Session` handle per id, so every caller that reaches
        #: the same session through this manager shares one bus, presence count,
        #: input queue, and active turn. Guarded for thread safety because
        #: ``open`` is synchronous and may be called from several threads (or
        #: several event loops); the asyncio side is naturally serialized by it.
        self._handles: dict[str, Session] = {}
        self._handles_lock = threading.RLock()
        #: Per-id event set when an in-flight create for that id finishes (or
        #: fails). A ``create=False`` opener that finds no log but an in-flight
        #: create waits on it (bounded) and re-checks instead of reporting a
        #: spurious "does not exist". Guarded by ``_handles_lock``.
        self._pending_creates: dict[str, threading.Event] = {}

    def path(self, session_id: str) -> Path:
        return self.store.log_path(session_id)

    def exists(self, session_id: str) -> bool:
        return self.store.exists(session_id)

    def queued_depth(self, session_id: str) -> int:
        """Pending input depth for a live session handle, or 0 when not cached.

        A host surface uses this to refuse a delete that would strand a durable
        ``input.queued`` submission: a deleted session must never resurrect by
        replaying a queue that outlived it. A read-only inspection: it never
        opens or creates a handle.
        """
        session_id = validate_session_id(session_id)
        handle = self._live_handle(session_id)
        return handle.queue_depth if handle is not None else 0

    @property
    def trash_dir(self) -> Path:
        """Directory holding trashed session artifacts and their metadata."""
        return self._trash_dir

    @property
    def retention_seconds(self) -> float:
        """How long a trashed session is retained before ``purge_expired``."""
        return self._retention_seconds

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

        A crash-interrupted delete is finished or rolled back first, so a log
        staged out of the sessions directory is restored before ``create`` could
        publish a fresh empty log that shadows the authoritative one.

        Exactly one :class:`Session` handle exists per id per manager, so
        repeated ``Runtime.session(id)`` calls share the same bus, presence
        count, input queue, and active turn. Migration and recovery are both
        idempotent, so re-opening never duplicates their effects.
        """
        session_id = validate_session_id(session_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        # Best-effort recovery first: a staging directory left by a
        # crash-mid-delete may still hold the only authoritative log. This sweep
        # is non-blocking and may be skipped when a concurrent recoverer holds
        # the trash lock, so it is not the last word: both ``migrate`` and
        # ``_create_session_log`` re-run the same sweep under the session lock
        # and the trash lock before any authoritative write, which is what stops
        # a fresh empty or stale-migrated log from shadowing the staged one. The
        # sweep only reads the trash directory, takes no session/handle lock, and
        # calls neither ``open`` nor ``list``, so it cannot recurse or deadlock
        # the ``_handles_lock`` taken below.
        self._recover_trash()
        if migrate:
            self.migrate(session_id)
        # Loop so exactly one handle is inserted even when several threads race
        # to open the same id. The slow create path runs *outside*
        # ``_handles_lock`` (see below); the loop re-checks the cache afterwards.
        # ``create=False`` must not turn a concurrent create into a spurious
        # "does not exist": a creator publishes a per-id event under
        # ``_handles_lock`` before it drops the lock, so a non-creating opener
        # that finds no log but an in-flight create waits (bounded) for it and
        # re-checks. A genuinely missing id, with no create in flight, still fails
        # promptly. The wait never holds ``_handles_lock``, so the session-lock
        # -> ``_handles_lock`` order ``delete`` uses is never inverted.
        deadline: float | None = None
        while True:
            with self._handles_lock:
                cached = self._handles.get(session_id)
                if cached is not None and (
                    getattr(cached, "_session_ended", False)
                    or getattr(cached, "retired", False)
                ):
                    # A closed or retired handle must never be handed out again:
                    # drop it and build a fresh one so a reopened session gets a
                    # live bus.
                    self._handles.pop(session_id, None)
                    cached = None
                if cached is not None:
                    # Re-opening an already-live handle still honors ``recover``:
                    # crash recovery is idempotent and never writes while this
                    # handle (or another process) holds the exclusive turn lock.
                    if recover:
                        cached.recover_dangling_tool_uses()
                    return cached
                if self.store.exists(session_id):
                    # Another opener may have published the log while we were
                    # outside the lock (or a crash-staged one was recovered).
                    session = self._build_handle(session_id)
                    if recover:
                        session.recover_dangling_tool_uses()
                    self._handles[session_id] = session
                    return session
                in_flight = self._pending_creates.get(session_id)
                if in_flight is None and not create:
                    raise SessionError(f"Session {session_id!r} does not exist")
                if in_flight is None:
                    # This opener owns the create. Publish the marker *before*
                    # releasing ``_handles_lock`` so a concurrent ``create=False``
                    # opener cannot slip in and report a spurious missing id.
                    in_flight = threading.Event()
                    self._pending_creates[session_id] = in_flight
                    owner = True
                else:
                    owner = False
            if owner:
                # No cached handle and no log: create it **outside**
                # ``_handles_lock``. The create path may wait (bounded) on the
                # session and trash flocks; holding the manager-wide lock across
                # that wait would stall every other ``open``/``evict``/``delete``
                # and invert the session-lock -> ``_handles_lock`` order
                # ``delete`` uses. Looping re-checks the cache so exactly one
                # handle is ever inserted. Create only after migration so a legacy
                # file is never shadowed. A crash-interrupted delete may have
                # staged the authoritative log out of the sessions directory; the
                # recovery above can have been skipped if another process held the
                # trash lock. The create path therefore takes the session lock
                # and, under it, the trash lock, so a concurrent recoverer can
                # never have a crash-staged authoritative log shadowed by a fresh
                # empty one.
                try:
                    self._create_session_log(session_id)
                finally:
                    with self._handles_lock:
                        event = self._pending_creates.pop(session_id, None)
                        if event is not None:
                            event.set()
                continue
            # Another opener is already creating this id. Wait for it, then
            # re-check (a completed create is observed as a log or handle; a
            # failed one leaves the id missing). ``create=True`` waits without a
            # bound because it will itself become the owner if the creator failed;
            # ``create=False`` is bounded so a genuinely missing id (or a wedged
            # creator) still fails promptly rather than hanging.
            if create:
                in_flight.wait(2 * _CREATE_LOCK_WAIT_SECONDS)
                continue
            if deadline is None:
                deadline = time.monotonic() + 2 * _CREATE_LOCK_WAIT_SECONDS
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionError(f"Session {session_id!r} does not exist")
            in_flight.wait(remaining)

    def _build_handle(self, session_id: str) -> Session:
        """Construct a live :class:`Session` handle for ``session_id``."""
        return Session(
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
            hooks=self._hooks,
        )

    def _create_session_log(self, session_id: str) -> None:
        """Create or migrate the log for ``session_id`` unless one must not exist.

        Called only when ``store.exists`` was false and ``create`` was requested.
        The session lock is taken first, then the trash lock, so the order is
        always session file lock -> trash lock (the same order
        ``delete``/``restore`` use) and no cycle is possible: recovery takes only
        the trash lock, and never a session lock.

        Two hazards are closed:

        * **A crash-staged authoritative log.** A delete interrupted after moving
          the log but before publishing its metadata leaves the only copy in a
          staging directory. A *concurrent* recoverer in another process holds
          only the trash lock; ``open``'s own non-blocking sweep would skip it.
          Re-running the sweep while holding both locks rolls the log back before
          ``store.create`` can publish a fresh empty one that shadows it.
        * **A losing brand-new opener.** ``store.create`` is already an atomic,
          non-truncating create, but two openers of a new id must both succeed
          rather than one failing with a spurious ``SessionBusy``. A session-lock
          contention waits briefly for the winner and then observes the created
          log; it only refuses when a delete genuinely holds the lock.

        The trash lock is retried only for a short, bounded window (a transient
        unrelated sweep or another session's delete), then the call refuses
        (``SessionBusy``) rather than waiting unbounded. The crash sweep and the
        ``should_migrate`` decision are re-run under the acquired lock, so waiting
        can never let a staged authoritative log be shadowed.
        """
        session_lock = SessionLock.for_session(self.directory, session_id)
        deadline = time.monotonic() + _CREATE_LOCK_WAIT_SECONDS
        while True:
            try:
                session_lock.acquire(shared=False, blocking=False)
            except SessionBusy:
                # The winner may have published the log already; a losing opener
                # must observe it. Otherwise a delete (or a slow create) holds the
                # lock: wait briefly, then refuse with a clear busy.
                if self.store.exists(session_id):
                    return
                if time.monotonic() >= deadline:
                    raise SessionBusy(
                        f"Session {session_id!r} is locked (a delete or create is "
                        "in progress); refusing to create over it"
                    ) from None
                time.sleep(_CREATE_LOCK_POLL_SECONDS)
                continue
            break
        try:
            if self.store.exists(session_id):
                return
            self._create_or_migrate_locked(session_id)
        finally:
            session_lock.release()

    def _acquire_trash_lock(self) -> TrashLock | None:
        """Take the trash lock with a short bounded retry, or return ``None``.

        A transient, *unrelated* holder (a short recovery sweep, another
        session's delete/purge) is waited out for at most
        ``_TRASH_LOCK_WAIT_SECONDS``; a genuinely stuck lock still fails closed
        rather than waiting unbounded. The caller already holds the session lock,
        so the order is always session file lock -> trash lock.
        """
        lock = TrashLock.for_dir(self.trash_dir)
        deadline = time.monotonic() + _TRASH_LOCK_WAIT_SECONDS
        while not lock.acquire(blocking=False):
            if time.monotonic() >= deadline:
                return None
            time.sleep(_CREATE_LOCK_POLL_SECONDS)
        return lock

    def _create_or_migrate_locked(self, session_id: str) -> None:
        """Publish the log for ``session_id``; caller holds its session lock.

        The trash lock is taken with a short bounded retry, preserving the
        session file lock -> trash lock order. Under both locks the crash sweep
        runs first. If it restored a legacy-only ``.json`` (so ``store.exists``
        is still false while ``should_migrate`` is true), that file is migrated
        to the authoritative ``.jsonl`` here -- under both locks -- instead of an
        empty log being published over the restored history. ``migrate_session``
        takes no lock of its own, so calling it with the session and trash locks
        already held is reentrant-safe and cannot deadlock.
        """
        trash_lock = self._acquire_trash_lock()
        if trash_lock is None:
            raise SessionBusy(
                f"Session {session_id!r} trash is busy (a recovery or delete is "
                "in progress); refusing to create over it"
            )
        try:
            self._recover_trash_locked()
            if self.store.exists(session_id):
                return
            # Recovery may have just restored a legacy-only session's ``.json``:
            # it has no ``.jsonl`` yet, so ``store.exists`` is false. Migrate it
            # rather than publish an empty log that would shadow the
            # authoritative restored history.
            if should_migrate(self.directory, session_id):
                migrate_session(self.directory, session_id)
                return
            self.store.create(session_id)
        finally:
            trash_lock.release()

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
            evicted = self._handles.pop(session_id)
        # Fire ``SessionEnd`` for the retired handle. A synchronous caller with a
        # running loop gets the close scheduled; otherwise an explicit
        # :meth:`aclose_session`/``await handle.aclose()`` performs it.
        self._schedule_session_end(evicted)
        return evicted

    def close(self, session_id: str) -> bool:
        """Alias for :meth:`evict` returning whether a handle was dropped."""
        return self.evict(session_id) is not None

    def close_all(self) -> list[Session]:
        """Evict every cached handle; returns them in insertion order.

        This is the shutdown seam and deliberately **forces** eviction even for
        handles that are active or viewed; the caller is expected to have torn
        those down first. Each retired handle's ``SessionEnd`` is scheduled (or
        awaited via :meth:`aclose_all`).
        """
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

    # -- list / summary ----------------------------------------------------

    def list(self) -> list[SessionSummary]:
        """Summarize every session, newest activity first.

        Both live handles and on-disk logs (including an unmigrated legacy
        ``<id>.json``) are listed. A session whose log is interior-corrupt is
        skipped rather than allowed to break the whole listing; a crash tail is
        tolerated by the store and still yields a valid prefix. A
        crash-interrupted delete is recovered first, so a log staged out of the
        sessions directory is listed rather than hidden behind a later empty
        session.
        """
        # Finish/roll back a crash-interrupted delete before enumerating: the
        # staged log is authoritative and must not be skipped. Read-only scan of
        # the trash directory; no lock is taken and neither ``open`` nor ``list``
        # is re-entered, so this cannot recurse.
        self._recover_trash()
        summaries: list[SessionSummary] = []
        for session_id in self._session_ids():
            try:
                summaries.append(self.summary(session_id))
            except (SessionError, OSError, ValueError):
                continue
        summaries.sort(key=lambda item: (-item.last_activity, item.id))
        return summaries

    def summary(self, session_id: str) -> SessionSummary:
        """Project one session into a transport-neutral :class:`SessionSummary`."""
        session_id = validate_session_id(session_id)
        handle = self._live_handle(session_id)
        if handle is None and not self._artifacts_exist(session_id):
            raise SessionError(f"Session {session_id!r} does not exist")
        read = self._consistent_read(session_id)
        return self._summary_for(session_id, read, handle)

    def _summary_for(
        self, session_id: str, read: ReadResult, handle: Session | None
    ) -> SessionSummary:
        loaded = snapshot_mod.load(self.directory, session_id, read)
        state = snapshot_mod.current_state(read, loaded)
        return SessionSummary(
            id=session_id,
            title=export_mod.derive_title(state.messages),
            state=_state_for(handle),
            last_activity=export_mod.last_activity(
                read.records, fallback=self.store.log_path(session_id)
            ),
            last_seq=read.next_seq,
            viewers=handle.viewers if handle is not None else 0,
        )

    def _live_handle(self, session_id: str) -> Session | None:
        with self._handles_lock:
            return self._handles.get(session_id)

    def _session_ids(self) -> list[str]:
        """Every session id with a log or a live handle, sorted for determinism."""
        ids: set[str] = set()
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            entries = []
        for path in entries:
            if not path.is_file():
                continue
            name = path.name
            if name.endswith(snapshot_mod.SNAPSHOT_SUFFIX):
                continue
            if name.endswith(".jsonl"):
                candidate = name[: -len(".jsonl")]
            elif name.endswith(".json"):
                # A legacy ``.json`` is a session only when it is a v1 session
                # document; this keeps an unrelated ``credentials.json`` (or any
                # other JSON) from being surfaced as a session.
                if not _looks_like_legacy_session(path):
                    continue
                candidate = name[: -len(".json")]
            else:
                continue
            if is_valid_session_id(candidate):
                ids.add(candidate)
        with self._handles_lock:
            ids.update(self._handles)
        return sorted(ids)

    # -- export ------------------------------------------------------------

    def export(self, session_id: str, *, format: str = "json") -> str:
        """Render a consistent log prefix as ``json``, ``markdown``, or ``jsonl``.

        Read-only and shared-lock aware, so it can run against a live session
        without disturbing its turn. Only session artifacts are read; no lock
        file, configuration, or credential is ever consulted.
        """
        session_id = validate_session_id(session_id)
        handle = self._live_handle(session_id)
        if handle is None and not self._artifacts_exist(session_id):
            raise SessionError(f"Session {session_id!r} does not exist")
        read = self._consistent_read(session_id)
        meta = self._summary_for(session_id, read, handle).to_dict()
        return export_mod.render(session_id, read, format=format, meta=meta)

    # -- delete / trash / restore ------------------------------------------

    def delete(
        self, session_id: str, *, force: bool = False, reason: str = ""
    ) -> TrashRecord:
        """Atomically move a session's artifacts into ``trash_dir``.

        Refuses a session with an active turn **even with** ``force``: deletion
        never cancels a turn, so the caller must cancel explicitly first. It also
        refuses one with pending queued input **even with** ``force``: a durable
        ``input.queued`` submission would otherwise outlive the delete and
        resurrect the session on the next open. A session with live viewers is
        refused unless ``force=True`` (which detaches them; it does not cancel
        anything). The exclusive session lock is taken non-blocking, so a turn
        running in another process is refused too.

        The move is atomic per artifact via ``os.replace`` under a staging
        directory that is renamed into place only after its retention metadata is
        durable, so a crash never loses the authoritative log.
        """
        session_id = validate_session_id(session_id)
        handle = self._live_handle(session_id)
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
        if handle is not None and handle.viewers > 0 and not force:
            raise SessionBusy(
                f"Session {session_id!r} has {handle.viewers} live viewer(s); "
                "pass force=True to delete and detach them"
            )
        if handle is None and not self._artifacts_exist(session_id):
            raise SessionError(f"Session {session_id!r} does not exist")
        lock = SessionLock.for_session(self.directory, session_id)
        try:
            lock.acquire(shared=False, blocking=False)
        except SessionBusy as exc:
            raise SessionBusy(
                f"Session {session_id!r} is locked by a running turn; refusing to delete"
            ) from exc
        try:
            # Re-check under the lock: a turn may have started in the window.
            handle = self._live_handle(session_id)
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
            # Retire the live handle **before** any artifact moves. A concurrent
            # or late writer -- a disconnecting viewer's presence cleanup, an
            # unattended fallback, a scheduled turn start -- is then blocked from
            # recreating the log once it is gone. If the move fails, the
            # retirement is rolled back so the session stays usable and the log
            # authoritative.
            if handle is not None:
                handle.retire()
            try:
                record = self._trash(session_id, reason=reason)
            except BaseException:
                if handle is not None:
                    handle.unretire()
                raise
        finally:
            lock.release()
        with self._handles_lock:
            evicted = self._handles.pop(session_id, None)
        if evicted is not None:
            # The handle is already retired; closing its bus detaches views
            # without cancelling any turn (an active turn was refused above).
            self._schedule_session_end(evicted)
        return record

    def restore(self, trash_id: str) -> str:
        """Move a trashed session back; returns its id.

        ``trash_id`` may be the trash entry's id or the session id. Restoration
        never overwrites an existing session and is serialized by the exclusive
        session lock.
        """
        if not isinstance(trash_id, str) or not trash_id:
            raise ValueError("trash_id must be a non-empty string")
        self._recover_trash()
        meta = self._find_trash(trash_id)
        if meta is None:
            raise SessionError(f"No trashed session {trash_id!r}")
        if not is_valid_session_id(meta.session_id):
            raise SessionError(
                f"Trashed session {meta.trash_id!r} names an invalid session id"
            )
        session_id = meta.session_id
        # The artifact names in on-disk metadata are untrusted; only the exact
        # filenames this manager itself would have written for this session may
        # be restored, so a crafted record can never move a path outside the
        # sessions directory.
        allowed = _session_artifact_names(session_id)
        entry = self.trash_dir / meta.trash_id
        if entry.is_symlink() or not entry.is_dir():
            raise SessionError(
                f"Trash entry {meta.trash_id!r} is not a directory"
            )
        for name in meta.files:
            if name not in allowed:
                raise SessionError(
                    f"Trash entry {meta.trash_id!r} names an unexpected artifact "
                    f"{name!r}; refusing to restore it"
                )
            source = entry / name
            if source.is_symlink() or not source.is_file():
                # A listed artifact this manager could not have published (gone,
                # a symlink, or a non-file) must refuse the whole restore rather
                # than be skipped: skipping would report success while moving
                # nothing and delete the authoritative trash entry.
                raise SessionError(
                    f"Trash entry {meta.trash_id!r} artifact {name!r} is missing "
                    "or not a regular file; refusing to restore it"
                )
        if self._artifacts_exist(session_id):
            raise SessionError(
                f"Session {session_id!r} already exists; refusing to overwrite"
            )
        lock = SessionLock.for_session(self.directory, session_id)
        try:
            lock.acquire(shared=False, blocking=False)
        except SessionBusy as exc:
            raise SessionBusy(
                f"Session {session_id!r} is locked; refusing to restore over it"
            ) from exc
        try:
            if self._artifacts_exist(session_id):
                raise SessionError(
                    f"Session {session_id!r} already exists; refusing to overwrite"
                )
            # Serialize the move with producers, recovery, and purges in other
            # processes. The session lock is already held, so the order is
            # session file lock -> trash lock.
            with TrashLock.for_dir(self.trash_dir).guard():
                if self._artifacts_exist(session_id):
                    raise SessionError(
                        f"Session {session_id!r} already exists; refusing to overwrite"
                    )
                moved: list[str] = []
                try:
                    for name in meta.files:
                        source = entry / name
                        if source.is_symlink() or not source.is_file():
                            raise SessionError(
                                f"Trash entry {meta.trash_id!r} artifact {name!r} is "
                                "missing or not a regular file; refusing to restore it"
                            )
                        destination = self.directory / name
                        if destination.exists():
                            raise SessionError(
                                f"Session artifact {name!r} already exists; "
                                "refusing to overwrite"
                            )
                        os.replace(source, destination)
                        moved.append(name)
                    self._remove_trash_entry(entry)
                    _fsync_dir(self.trash_dir)
                    return session_id
                except BaseException:
                    # A partial restore must not strand artifacts in the sessions
                    # directory: move back everything already restored so the
                    # trash entry stays authoritative and the operation can be
                    # retried.
                    for name in reversed(moved):
                        source = self.directory / name
                        destination = entry / name
                        if source.exists() and not destination.exists():
                            with contextlib.suppress(OSError):
                                os.replace(source, destination)
                    raise
        finally:
            lock.release()

    def list_trashed(self) -> list[TrashRecord]:
        """Retention metadata for every trashed session, newest first."""
        self._recover_trash()
        records = self._trash_records()
        records.sort(key=lambda item: (-item.trashed_at, item.trash_id))
        return records

    def purge_expired(self, *, now: float | None = None) -> list[str]:
        """Remove trash entries past ``delete_after``; returns their ids.

        Cleanup is explicit so a listing never mutates state as a side effect.
        """
        self._recover_trash()
        moment = time.time() if now is None else float(now)
        removed: list[str] = []
        if not self.trash_dir.is_dir():
            return removed
        with TrashLock.for_dir(self.trash_dir).guard():
            for meta in self._trash_records():
                if meta.delete_after > moment:
                    continue
                entry = self.trash_dir / meta.trash_id
                if entry.is_symlink():
                    # ``rmtree`` refuses a symlink; unlink the link itself so a
                    # crafted entry can never recurse outside the trash dir.
                    with contextlib.suppress(OSError):
                        entry.unlink()
                else:
                    with contextlib.suppress(OSError):
                        shutil.rmtree(entry)
                removed.append(meta.trash_id)
        return removed

    def _artifact_paths(self, session_id: str) -> list[Path]:
        base = self.directory / session_id
        candidates = (
            base.with_suffix(".jsonl"),
            base.with_suffix(".snap.json"),
            base.with_suffix(".json"),
            base.with_suffix(".v1.bak"),
        )
        return [path for path in candidates if path.exists()]

    def _trash(self, session_id: str, *, reason: str) -> TrashRecord:
        paths = self._artifact_paths(session_id)
        if not paths:
            raise SessionError(f"Session {session_id!r} has no artifacts to delete")
        # The exclusive lock is held, so a plain read is already consistent.
        read = self.store.read(session_id)
        loaded = snapshot_mod.load(self.directory, session_id, read)
        title = export_mod.derive_title(snapshot_mod.current_state(read, loaded).messages)
        trash_dir = self.trash_dir
        token = secrets.token_hex(6)
        now = time.time()
        # Hold the trash lock across the entire stage/publish so a recovery
        # sweep in another process can never roll the staging directory back
        # mid-move. The caller already holds the session's exclusive lock, so the
        # ordering is session file lock -> trash lock.
        with TrashLock.for_dir(trash_dir).guard():
            trash_dir.mkdir(parents=True, exist_ok=True)
            staging = trash_dir / f"{_TRASH_STAGING_PREFIX}{token}"
            staging.mkdir()
            moved: list[str] = []
            try:
                for path in paths:
                    os.replace(path, staging / path.name)
                    moved.append(path.name)
                meta = TrashRecord(
                    trash_id=f"{session_id}-{token}",
                    session_id=session_id,
                    trashed_at=now,
                    delete_after=now + self._retention_seconds,
                    files=tuple(moved),
                    title=title,
                    last_seq=read.next_seq,
                    reason=reason,
                )
                _write_trash_meta(staging / _TRASH_META, meta)
                _fsync_dir(staging)
                # Publish atomically: the staging directory becomes the trash
                # entry only once its metadata is durable.
                os.replace(staging, trash_dir / meta.trash_id)
                _fsync_dir(trash_dir)
                return meta
            except BaseException:
                self._rollback_staging(staging, moved)
                raise

    def _rollback_staging(self, staging: Path, moved: list[str]) -> None:
        for name in moved:
            source = staging / name
            destination = self.directory / name
            if source.exists() and not destination.exists():
                with contextlib.suppress(OSError):
                    os.replace(source, destination)
        for name in (_TRASH_META, _TRASH_META + ".tmp"):
            with contextlib.suppress(OSError):
                (staging / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            staging.rmdir()

    def _trash_records(self) -> list[TrashRecord]:
        try:
            entries = list(self.trash_dir.iterdir())
        except OSError:
            return []
        records: list[TrashRecord] = []
        for entry in entries:
            # Only a real published entry directory is trusted; a symlinked
            # entry could make a purge or restore act outside the trash dir, and
            # a staging dir carries no durable ``trash_id`` identity yet.
            if entry.is_symlink() or not entry.is_dir():
                continue
            if entry.name.startswith(_TRASH_STAGING_PREFIX):
                continue
            meta_path = entry / _TRASH_META
            if meta_path.is_symlink():
                continue
            try:
                meta = msgspec.json.decode(meta_path.read_bytes(), type=TrashRecord)
            except (OSError, msgspec.DecodeError, msgspec.ValidationError):
                continue
            # On-disk metadata is untrusted: every field that later becomes a
            # path (the trash id, the session id, and each artifact name) must
            # be one this manager could itself have written.
            if not _trustworthy_trash_record(meta):
                continue
            # The entry directory's name is authoritative for its identity: a
            # record whose ``trash_id`` names a different entry is a mismatch
            # this manager never wrote, so it is ignored rather than trusted.
            if entry.name != meta.trash_id:
                continue
            records.append(meta)
        return records

    def _find_trash(self, trash_id: str) -> TrashRecord | None:
        for meta in self._trash_records():
            if meta.trash_id == trash_id or meta.session_id == trash_id:
                return meta
        return None

    def _remove_trash_entry(self, entry: Path) -> None:
        for name in (_TRASH_META, _TRASH_META + ".tmp"):
            with contextlib.suppress(OSError):
                (entry / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            entry.rmdir()

    def _recover_trash(self) -> None:
        """Finish or roll back a trash move interrupted before publication.

        A staging directory with durable metadata is published; one without is
        rolled back so the authoritative log returns to the sessions directory.
        Best-effort: any failure leaves the artifacts intact for the next sweep.

        The sweep takes the trash lock **non-blocking**, and takes no session
        lock: if a producer is mid-stage (or a restore/purge is running) the whole
        sweep is skipped rather than waiting, so ``open``/``list`` never block and
        a partially moved stage is never touched by two processes at once.
        """
        if not self.trash_dir.is_dir():
            return
        lock = TrashLock.for_dir(self.trash_dir)
        with lock.guard(blocking=False) as acquired:
            if not acquired:
                return
            self._recover_trash_locked()

    def _recover_trash_locked(self) -> None:
        """Sweep the trash; the caller must already hold the trash lock.

        Split from :meth:`_recover_trash` so the create path can re-run the very
        same sweep while it holds the session lock *and* the trash lock, instead
        of nesting a second non-blocking acquisition of the same lock (which the
        OS would report as contention). Otherwise the two paths are identical.
        """
        try:
            entries = list(self.trash_dir.iterdir())
        except OSError:
            return
        for entry in entries:
            if (
                entry.is_symlink()
                or not entry.is_dir()
                or not entry.name.startswith(_TRASH_STAGING_PREFIX)
            ):
                continue
            meta_path = entry / _TRASH_META
            meta: TrashRecord | None = None
            if meta_path.exists() and not meta_path.is_symlink():
                try:
                    candidate = msgspec.json.decode(
                        meta_path.read_bytes(), type=TrashRecord
                    )
                except (OSError, msgspec.DecodeError, msgspec.ValidationError):
                    candidate = None
                if candidate is not None and _trustworthy_trash_record(candidate):
                    meta = candidate
            if meta is not None:
                # Publish only a record this manager could itself have written.
                final = self.trash_dir / meta.trash_id
                if not final.exists():
                    with contextlib.suppress(OSError):
                        os.replace(entry, final)
                continue
            # No trustworthy metadata: roll the moved artifacts back. A partially
            # written ``meta.json`` (or its temp) is removed, never moved: the
            # authoritative log must return to the sessions directory, and an
            # untrusted metadata document must not land beside it. A symlinked
            # child is unlinked, never followed.
            for child in list(entry.iterdir()):
                if child.name in (_TRASH_META, _TRASH_META + ".tmp"):
                    with contextlib.suppress(OSError):
                        child.unlink(missing_ok=True)
                    continue
                if child.is_symlink():
                    with contextlib.suppress(OSError):
                        child.unlink(missing_ok=True)
                    continue
                destination = self.directory / child.name
                if not destination.exists():
                    with contextlib.suppress(OSError):
                        os.replace(child, destination)
            with contextlib.suppress(OSError):
                entry.rmdir()

    def migrate(self, session_id: str) -> MigrationResult | None:
        """Migrate one session if needed, serialized by the session lock.

        The session lock is taken first and, under it, the trash lock; a
        crash-interrupted delete is recovered **before** the migrate decision.
        That re-run closes the shadowing hole ``open``'s best-effort sweep cannot
        close: if a concurrent recoverer held the trash lock, the outer sweep
        skipped, and a stale legacy ``.json`` could be migrated over the
        authoritative ``.jsonl`` that a crash had staged out of the sessions
        directory. Recovery restores that log first, so ``should_migrate``
        re-checked under the lock is then false and no stale log is written.

        Lock order is session file lock -> trash lock, matching
        ``delete``/``restore``/``_create_session_log``; recovery takes only the
        trash lock, so no cycle is possible. A trash lock a live producer or
        recoverer still holds refuses (``SessionBusy``) rather than risk a
        shadow. This holds for a direct :meth:`migrate` call as much as for the
        ``open`` path, so a caller that migrates without opening is safe too.
        """
        session_id = validate_session_id(session_id)
        if not should_migrate(self.directory, session_id):
            return None
        lock = SessionLock.for_session(self.directory, session_id)
        try:
            with lock.exclusive(blocking=False):
                return self._migrate_locked(session_id)
        except SessionBusy:
            # Another opener is migrating or running, or a producer/recoverer
            # holds the trash lock. If another opener finished, we are done;
            # otherwise surface the contention instead of risking a shadow.
            if should_migrate(self.directory, session_id):
                raise
            return None

    def _migrate_locked(self, session_id: str) -> MigrationResult | None:
        """Recover, then migrate only if still needed; caller holds the session lock.

        The trash lock is taken with the same short bounded retry as the create
        path (never the reverse order, and recovery re-entered lock-free) so a
        transient, unrelated holder is waited out without ever deadlocking or
        recursing. When it stays busy past the bound the migration is refused:
        an authoritative log may be staged out of the sessions directory and
        migrating the stale legacy file would shadow it.
        """
        trash_lock = self._acquire_trash_lock()
        if trash_lock is None:
            raise SessionBusy(
                f"Session {session_id!r} trash is busy (a recovery or delete "
                "is in progress); refusing to migrate over a possibly staged log"
            )
        try:
            self._recover_trash_locked()
            if not should_migrate(self.directory, session_id):
                # Recovery restored the authoritative JSONL (or another opener
                # migrated): never shadow it with the stale legacy bytes.
                return None
            return migrate_session(self.directory, session_id)
        finally:
            trash_lock.release()

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
        """Whether any artifact this manager may move/restore is present.

        The set must match :meth:`_artifact_paths` (what ``delete`` moves) and
        :func:`_session_artifact_names` (what ``restore`` trusts): a ``.v1.bak``
        alone still names a session that can be deleted and restored, so it must
        count here too or those paths refuse a session they could otherwise move.
        """
        base = self.directory / session_id
        return any(
            candidate.exists()
            for candidate in (
                base.with_suffix(".jsonl"),
                base.with_suffix(".json"),
                base.with_suffix(".snap.json"),
                base.with_suffix(".v1.bak"),
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


def _looks_like_legacy_session(path: Path) -> bool:
    """Whether ``path`` is a v1 ``{version: 1, exchanges: [...]}`` document.

    Used only while enumerating a sessions directory: a legacy file is listed as
    a session, but arbitrary JSON (for example a stray ``credentials.json``) is
    not. A malformed file is simply not a session.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if not raw:
        return False
    try:
        data = msgspec.json.decode(raw)
    except (msgspec.DecodeError, msgspec.ValidationError):
        return False
    return (
        isinstance(data, dict)
        and data.get("version") == 1
        and isinstance(data.get("exchanges"), list)
    )


def _write_trash_meta(path: Path, meta: TrashRecord) -> None:
    """Atomically and durably write one trash entry's metadata.

    The bytes go to a sibling ``<meta>.tmp`` that is ``fsync``'d and then renamed
    over ``path``, so a concurrent reader (a recovery sweep in another process)
    never observes a half-written document: it sees either no metadata or the
    complete record. The containing directory is ``fsync``'d so the rename is
    durable before the staging entry is published.
    """
    payload = msgspec.json.encode(meta) + b"\n"
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform without dir fsync
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


__all__ = [
    "DEFAULT_RETENTION_SECONDS",
    "TRASH_VERSION",
    "SessionManager",
    "SessionState",
    "SessionSummary",
    "TrashRecord",
]
