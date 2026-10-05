"""The shared SQLite state database: one file for every project (STATE_PLAN §4).

:class:`StateDatabase` owns the connection, schema, and durability pragmas for
``~/.nexus/nexus.db`` (or a private per-test database). :class:`SqliteSessionStore`
is the SQLite session store, scoped to one ``(project_id, namespace)`` pair and
implementing the exact store surface that
:class:`~nexus.session.session.Session` and
:class:`~nexus.session.manager.SessionManager` depend on:
``exists``, ``create``, ``create_from_records``, ``read``, ``records``,
``next_seq``, ``append_event``, ``append_message``, ``append_summary``,
``fsync`` (an injectable no-op kept for API parity -- ``synchronous=FULL`` is
the real durability barrier), and ``lock_path``. Session listing, archive,
trash and snapshot state -- previously separate sidecar files -- are plain
tables on the same connection, so :class:`~nexus.session.manager.SessionManager`
can implement every operation as one bounded, indexed SQL transaction instead
of a directory scan.

Record encoding is unchanged: a ``records.body`` column stores exactly
``msgspec.json.encode(EventRecord | MessageRecord | SummaryRecord)`` -- the same
bytes a JSONL export line carries, so the reducer, export, snapshot validation,
fork and replay are untouched (STATE_PLAN §2.2).

Concurrency (STATE_PLAN §2.3-2.4): WAL journal mode, ``synchronous=FULL``,
``busy_timeout=5000``, and every write transaction opened with
``BEGIN IMMEDIATE`` so seq assignment is race-free across threads, processes,
and the several per-workspace daemons that may share this one file. Reads use a
plain deferred transaction, which under WAL always observes a consistent
committed prefix and never blocks (or is blocked by) a writer.

Secrecy boundary (STATE_PLAN §2.7): the database file is created ``0600`` and
its parent directory ``0700`` before the first connection.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import msgspec

from ..errors import SessionError
from ..events import Event
from ..model.message import Message
from . import export as export_mod
from . import snapshot as snapshot_mod
from .ids import validate_session_id
from .records import (
    SESSION_LOG_VERSION,
    EventRecord,
    MessageRecord,
    ReadResult,
    SessionRecord,
    SummaryRecord,
)


def _has_user_input(alias: str = "sessions") -> str:
    """SQL predicate shared by workspace and global session lists."""
    return (
        "EXISTS (SELECT 1 FROM records r WHERE "
        f"r.project_id={alias}.project_id AND r.namespace={alias}.namespace "
        f"AND r.session_id={alias}.id AND ("
        "(r.kind='message' AND json_extract(r.body, '$.message.role')='user') OR "
        "(r.kind='event' AND json_extract(r.body, '$.event.type')='input.queued')))"
    )


#: Current schema version. Bumped whenever ``_SCHEMA_STEPS`` grows a step; a
#: database with a *higher* ``user_version`` than this was written by a newer
#: Nexus and is refused rather than misread.
SCHEMA_VERSION = 3

#: Forward-only DDL, one tuple of statements per schema version. Applied inside
#: a single ``BEGIN IMMEDIATE`` transaction so two processes racing to create a
#: fresh database can never both "win": the loser's ``CREATE TABLE`` blocks on
#: the busy timeout and then observes ``user_version`` already advanced.
_SCHEMA_STEPS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE projects (
          id          TEXT PRIMARY KEY,
          root        TEXT NOT NULL,
          created_at  REAL NOT NULL,
          last_opened REAL NOT NULL
        )
        """,
        """
        CREATE TABLE sessions (
          project_id  TEXT NOT NULL REFERENCES projects(id),
          namespace   TEXT NOT NULL DEFAULT 'main',
          id          TEXT NOT NULL,
          created_at  REAL NOT NULL,
          last_seq    INTEGER NOT NULL DEFAULT 0,
          last_activity REAL NOT NULL DEFAULT 0,
          message_count INTEGER NOT NULL DEFAULT 0,
          title       TEXT NOT NULL DEFAULT '',
          parent_id   TEXT NOT NULL DEFAULT '',
          fork_seq    INTEGER NOT NULL DEFAULT 0,
          archived_at REAL, archive_reason TEXT,
          trash_id    TEXT UNIQUE, trashed_at REAL,
          trash_expires_at REAL, trash_reason TEXT,
          PRIMARY KEY (project_id, namespace, id)
        )
        """,
        """
        CREATE TABLE records (
          project_id TEXT NOT NULL, namespace TEXT NOT NULL, session_id TEXT NOT NULL,
          seq   INTEGER NOT NULL,
          kind  TEXT NOT NULL CHECK (kind IN ('event','message','summary')),
          ts    REAL NOT NULL,
          body  BLOB NOT NULL,
          PRIMARY KEY (project_id, namespace, session_id, seq),
          FOREIGN KEY (project_id, namespace, session_id)
             REFERENCES sessions(project_id, namespace, id) ON DELETE CASCADE
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE snapshots (
          project_id TEXT, namespace TEXT, session_id TEXT,
          seq INTEGER NOT NULL, body BLOB NOT NULL,
          PRIMARY KEY (project_id, namespace, session_id),
          FOREIGN KEY (project_id, namespace, session_id)
             REFERENCES sessions(project_id, namespace, id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE kv (
          project_id TEXT, namespace TEXT, key TEXT, value TEXT,
          PRIMARY KEY (project_id, namespace, key)
        )
        """,
        "CREATE INDEX sessions_by_activity ON sessions(project_id, namespace, last_activity DESC)",
    ),
    # v2: where a session's title came from. ``first_message`` is the derived
    # title a model-written one may replace; ``auto`` and ``user`` are never
    # overwritten. Rows that predate the column keep ``''`` and are left alone.
    ("ALTER TABLE sessions ADD COLUMN title_source TEXT NOT NULL DEFAULT ''",),
    # v3: durable watermark for the latest turn terminal event, distinct from
    # last_seq, which also advances for presence and other non-turn activity.
    (
        "ALTER TABLE sessions ADD COLUMN completion_seq INTEGER NOT NULL DEFAULT 0",
        "UPDATE sessions SET completion_seq=COALESCE(("
        "SELECT MAX(r.seq) FROM records r WHERE r.project_id=sessions.project_id "
        "AND r.namespace=sessions.namespace AND r.session_id=sessions.id "
        "AND r.kind='event' AND json_extract(r.body, '$.event.type') IN "
        "('turn.completed','turn.failed','turn.cancelled')"
        "), 0)",
    ),
)

#: Every ``kv``/session mutation this module performs is bounded by these; a
#: pathological request (an absurd ``limit``) is refused before it reaches SQL.
_MAX_LIMIT = 10_000


def _ensure_file_permissions(path: Path) -> None:
    """Create ``path``'s parent ``0700`` and the file itself ``0600`` (§2.7)."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(parent, 0o700)
    if not path.exists():
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


class StateDatabase:
    """One SQLite connection pool over ``path``, one connection per thread."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        _ensure_file_permissions(self._path)
        self._local = threading.local()
        self._migrate_lock = threading.Lock()
        self._migrate()

    @property
    def path(self) -> Path:
        return self._path

    def _raw_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.row_factory = sqlite3.Row
        return conn

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._raw_connect()
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        """Run forward-only DDL under ``BEGIN IMMEDIATE`` (idempotent, §4.1)."""
        with self._migrate_lock:
            conn = self._raw_connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if version > SCHEMA_VERSION:
                        raise SessionError(
                            f"state database schema {version} is newer than this "
                            f"Nexus (supports up to {SCHEMA_VERSION}): {self._path}"
                        )
                    for step in range(version, SCHEMA_VERSION):
                        for statement in _SCHEMA_STEPS[step]:
                            conn.execute(statement)
                    if version < SCHEMA_VERSION:
                        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT``/``ROLLBACK``: the one writer barrier."""
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A deferred transaction: a consistent multi-query read, never blocking."""
        conn = self._connection()
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def quick_check(self) -> str:
        row = self._connection().execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "unknown"

    def schema_version(self) -> int:
        return int(self._connection().execute("PRAGMA user_version").fetchone()[0])

    def size_bytes(self) -> int:
        try:
            return self._path.stat().st_size
        except OSError:
            return 0

    def touch_project(self, project_id: str, root: str) -> None:
        now = time.time()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO projects(id, root, created_at, last_opened) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET root=excluded.root, "
                "last_opened=excluded.last_opened",
                (project_id, root, now, now),
            )

    def project_sessions(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        """Bounded cross-project sidebar index; excludes children, archive and trash."""
        if not 1 <= limit <= _MAX_LIMIT:
            raise ValueError("invalid project session limit")
        return [
            dict(row)
            for row in self._connection().execute(
                "SELECT s.*, p.root AS workspace FROM sessions s "
                "JOIN projects p ON p.id=s.project_id "
                "WHERE s.namespace='main' AND s.archived_at IS NULL AND s.trash_id IS NULL "
                f"AND {_has_user_input('s')} "
                "ORDER BY s.last_activity DESC, s.project_id, s.id LIMIT ?",
                (limit,),
            )
        ]

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def _decode_record(raw: bytes) -> SessionRecord:
    record = msgspec.json.decode(raw, type=SessionRecord)
    if record.v != SESSION_LOG_VERSION:
        raise SessionError(
            f"Unsupported session log version {record.v}; expected {SESSION_LOG_VERSION}"
        )
    return record


class SqliteSessionStore:
    """SQL-backed store for one project's ``(project_id, namespace)`` sessions.

    Implements the store surface used by :class:`~nexus.session.session.Session`
    and :class:`~nexus.session.manager.SessionManager`
    (``exists``/``create``/``create_from_records``/``read``/``records``/
    ``next_seq``/``append_event``/``append_message``/``append_summary``/
    ``fsync``/``lock_path``), plus the SQL operations the manager needs for
    listing, archive, trash and snapshots so those no longer require a
    directory scan or sidecar file.
    """

    def __init__(
        self,
        db: StateDatabase,
        project_id: str,
        namespace: str = "main",
        *,
        root: str | None = None,
        lock_dir: str | Path | None = None,
    ):
        self.db = db
        self.project_id = project_id
        self.namespace = namespace
        self.db.touch_project(project_id, root if root is not None else project_id)
        self._lock_dir = Path(lock_dir) if lock_dir is not None else None
        self._fsync = lambda fd: None  # durability is PRAGMA-provided
        self._drafts: dict[str, list[SessionRecord]] = {}
        self._draft_lock = threading.RLock()

    @property
    def fsync(self):
        return self._fsync

    @property
    def lock_dir(self) -> Path | None:
        return self._lock_dir

    def lock_path(self, session: str) -> Path:
        session = validate_session_id(session)
        base = self._lock_dir if self._lock_dir is not None else Path(".")
        return base / f"{self.namespace}-{session}.lock"

    # -- existence / creation ------------------------------------------

    def exists(self, session: str) -> bool:
        session = validate_session_id(session)
        with self._draft_lock:
            if session in self._drafts:
                return True
        row = (
            self.db._connection()
            .execute(
                "SELECT 1 FROM sessions WHERE project_id=? AND namespace=? AND id=? "
                "AND trash_id IS NULL",
                (self.project_id, self.namespace, session),
            )
            .fetchone()
        )
        return row is not None

    def row_exists(self, session: str) -> bool:
        """Whether any row (live or trashed) exists for ``session``."""
        session = validate_session_id(session)
        with self._draft_lock:
            if session in self._drafts:
                return True
        row = (
            self.db._connection()
            .execute(
                "SELECT 1 FROM sessions WHERE project_id=? AND namespace=? AND id=?",
                (self.project_id, self.namespace, session),
            )
            .fetchone()
        )
        return row is not None

    def create(self, session: str) -> None:
        """Create an empty session row if absent; never overwrites one."""
        session = validate_session_id(session)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions"
                "(project_id, namespace, id, created_at, last_seq, last_activity) "
                "VALUES (?,?,?,?,0,0)",
                (self.project_id, self.namespace, session, time.time()),
            )

    def create_draft(self, session: str) -> None:
        """Reserve a chat in memory until its first accepted user input."""
        session = validate_session_id(session)
        with self._draft_lock:
            if not self.row_exists(session):
                self._drafts[session] = []

    def create_from_records(
        self,
        session: str,
        records: Sequence[SessionRecord],
        *,
        parent_id: str = "",
        fork_seq: int = 0,
        archived_at: float | None = None,
        archive_reason: str | None = None,
        trash_id: str | None = None,
        trashed_at: float | None = None,
        trash_expires_at: float | None = None,
        trash_reason: str | None = None,
    ) -> None:
        """Publish a brand-new session from an exact record prefix, one txn.

        Raises :class:`SessionError` ("Session log already exists") if a row
        (live or trashed) already occupies this id. The ``archived_*``/``trash_*``
        keywords are for the legacy importer, which restores exact historical
        metadata instead of re-deriving it.
        """
        session = validate_session_id(session)
        now = time.time()
        created_at = next((r.ts for r in records if r.ts > 0), now)
        last_seq = max((r.seq for r in records), default=0)
        completion_seq = max(
            (
                r.seq
                for r in records
                if isinstance(r, EventRecord)
                and r.event.type in {"turn.completed", "turn.failed", "turn.cancelled"}
            ),
            default=0,
        )
        last_activity = max((r.ts for r in records if r.ts > 0), default=0.0)
        message_count = sum(
            1
            for r in records
            if isinstance(r, MessageRecord) and r.message.role == "user"
        )
        title = ""
        for r in records:
            if isinstance(r, MessageRecord) and r.message.role == "user":
                title = export_mod.derive_title([r.message])
                if title:
                    break
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO sessions"
                    "(project_id, namespace, id, created_at, last_seq, completion_seq, last_activity,"
                    " message_count, title, title_source, parent_id, fork_seq, archived_at, archive_reason,"
                    " trash_id, trashed_at, trash_expires_at, trash_reason)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self.project_id,
                        self.namespace,
                        session,
                        created_at,
                        last_seq,
                        completion_seq,
                        last_activity,
                        message_count,
                        title,
                        "first_message" if title else "",
                        parent_id,
                        fork_seq,
                        archived_at,
                        archive_reason,
                        trash_id,
                        trashed_at,
                        trash_expires_at,
                        trash_reason,
                    ),
                )
                conn.executemany(
                    "INSERT INTO records"
                    "(project_id, namespace, session_id, seq, kind, ts, body) "
                    "VALUES (?,?,?,?,?,?,?)",
                    [
                        (
                            self.project_id,
                            self.namespace,
                            session,
                            r.seq,
                            _kind_of(r),
                            r.ts,
                            msgspec.json.encode(r),
                        )
                        for r in records
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise SessionError(f"Session log already exists: {session}") from exc

    # -- reading ---------------------------------------------------------

    def read(self, session: str) -> ReadResult:
        session = validate_session_id(session)
        with self._draft_lock:
            if session in self._drafts:
                records = list(self._drafts[session])
                return ReadResult(records=records)
        rows = (
            self.db._connection()
            .execute(
                "SELECT body FROM records WHERE project_id=? AND namespace=? AND session_id=? "
                "ORDER BY seq ASC",
                (self.project_id, self.namespace, session),
            )
            .fetchall()
        )
        records = tuple(_decode_record(row[0]) for row in rows)
        valid_bytes = sum(len(row[0]) + 1 for row in rows)
        return ReadResult(records, False, valid_bytes, True)

    def read_tail(self, session: str, *, max_records: int) -> ReadResult:
        """The newest ``max_records`` records, in ascending order.

        Used by bounded scans (doctor's mismatch aggregation, the observability
        log tail) that must never load an arbitrarily long history.
        """
        session = validate_session_id(session)
        if type(max_records) is not int or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        rows = (
            self.db._connection()
            .execute(
                "SELECT body FROM records WHERE project_id=? AND namespace=? AND session_id=? "
                "ORDER BY seq DESC LIMIT ?",
                (
                    self.project_id,
                    self.namespace,
                    session,
                    min(max_records, _MAX_LIMIT),
                ),
            )
            .fetchall()
        )
        rows.reverse()
        records = tuple(_decode_record(row[0]) for row in rows)
        return ReadResult(records, False, sum(len(row[0]) + 1 for row in rows), True)

    def tail_bytes(self, session: str, *, max_bytes: int) -> bytes:
        """Raw encoded bodies of the newest records, newline-joined, capped.

        Reads a bounded byte tail of serialized records for callers that want to
        substring-search the exported record text rather than
        decode it (``host_support/session_archive.py``). The descending scan
        stops once the requested byte window is covered and is hard-limited to
        ``_MAX_LIMIT`` records; SQLite truncates each fetched BLOB to the
        requested window so a single enormous record cannot defeat the cap.
        """
        session = validate_session_id(session)
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        cursor = self.db._connection().execute(
            "SELECT substr(body, -?) FROM records "
            "WHERE project_id=? AND namespace=? AND session_id=? "
            "ORDER BY seq DESC LIMIT ?",
            (
                max_bytes,
                self.project_id,
                self.namespace,
                session,
                min(max_bytes, _MAX_LIMIT),
            ),
        )
        collected: list[bytes] = []
        total = 0
        for (body,) in cursor:
            total += len(body) + 1
            collected.append(body)
            if total >= max_bytes:
                break
        collected.reverse()
        return (b"\n".join(collected) + (b"\n" if collected else b""))[-max_bytes:]

    def records(self, session: str) -> Iterator[SessionRecord]:
        yield from self.read(session).records

    def next_seq(self, session: str) -> int:
        session = validate_session_id(session)
        with self._draft_lock:
            if session in self._drafts:
                return max((r.seq for r in self._drafts[session]), default=0) + 1
        row = (
            self.db._connection()
            .execute(
                "SELECT last_seq FROM sessions WHERE project_id=? AND namespace=? AND id=?",
                (self.project_id, self.namespace, session),
            )
            .fetchone()
        )
        if row is None:
            return 1
        return int(row[0]) + 1

    # -- appending ---------------------------------------------------------

    def append_event(
        self, session: str, event: Event, *, seq: int | None = None
    ) -> EventRecord:
        if not isinstance(event, Event):
            raise TypeError("append_event requires an Event")
        session = validate_session_id(session)
        requested = seq if seq is not None else (event.seq if event.seq > 0 else None)

        def build(seq_value: int) -> tuple[Event, bytes]:
            ev = event
            if seq_value != ev.seq or ev.session is None:
                ev = msgspec.structs.replace(
                    ev,
                    seq=seq_value,
                    session=ev.session if ev.session is not None else session,
                )
            record = EventRecord(seq=seq_value, event=ev, ts=ev.ts)
            return ev, msgspec.json.encode(record)

        return self._commit_append(session, requested, "event", event.ts, build)

    def append_message(
        self, session: str, message: Message, *, seq: int | None = None
    ) -> MessageRecord:
        if not isinstance(message, Message):
            raise TypeError("append_message requires a Message")
        session = validate_session_id(session)
        ts = message.meta.ts if message.meta.ts is not None else time.time()

        def build(seq_value: int) -> tuple[Message, bytes]:
            record = MessageRecord(seq=seq_value, message=message, ts=ts)
            return message, msgspec.json.encode(record)

        return self._commit_append(session, seq, "message", ts, build)

    def append_summary(
        self,
        session: str,
        *,
        text: str = "",
        summary_id: str = "",
        strategy: str = "",
        input_digest: str = "",
        source_from_seq: int = 0,
        source_to_seq: int = 0,
        source_messages: int = 0,
        tokens_before: int | None = None,
        tokens_after: int | None = None,
        provider: str | None = None,
        model: str | None = None,
        ts: float | None = None,
        seq: int | None = None,
    ) -> SummaryRecord:
        session = validate_session_id(session)
        stamp = time.time() if ts is None else ts

        def build(seq_value: int) -> tuple[None, bytes]:
            record = SummaryRecord(
                seq=seq_value,
                text=text,
                summary_id=summary_id,
                strategy=strategy,
                input_digest=input_digest,
                source_from_seq=source_from_seq,
                source_to_seq=source_to_seq,
                source_messages=source_messages,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                provider=provider,
                model=model,
                ts=stamp,
            )
            return None, msgspec.json.encode(record)

        return self._commit_append(session, seq, "summary", stamp, build)

    def _commit_append(self, session, requested_seq, kind, ts, build):
        if requested_seq is not None and (
            type(requested_seq) is not int or requested_seq < 1
        ):
            raise ValueError("seq must be a positive integer")
        with self._draft_lock:
            if session in self._drafts:
                records = self._drafts[session]
                assigned = (
                    requested_seq
                    if requested_seq is not None
                    else self.next_seq(session)
                )
                if any(r.seq == assigned for r in records):
                    raise SessionError(
                        f"seq {assigned} already exists for session {session!r}"
                    )
                payload, encoded = build(assigned)
                record = msgspec.json.decode(encoded, type=SessionRecord)
                submitted = (
                    isinstance(payload, Message) and payload.role == "user"
                ) or (isinstance(payload, Event) and payload.type == "input.queued")
                if submitted:
                    # All setup records and the first input become durable together.
                    # A failed transaction leaves the draft available for retry.
                    self.create_from_records(session, [*records, record])
                    del self._drafts[session]
                else:
                    records.append(record)
                return record
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT last_seq, title, message_count, created_at, last_activity, title_source "
                "FROM sessions WHERE project_id=? AND namespace=? AND id=?",
                (self.project_id, self.namespace, session),
            ).fetchone()
            if row is None:
                raise SessionError(f"Session {session!r} does not exist")
            last_seq, title, message_count, created_at, last_activity, title_source = (
                row
            )
            assigned = requested_seq if requested_seq is not None else last_seq + 1
            payload, encoded = build(assigned)
            try:
                conn.execute(
                    "INSERT INTO records"
                    "(project_id, namespace, session_id, seq, kind, ts, body) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        self.project_id,
                        self.namespace,
                        session,
                        assigned,
                        kind,
                        ts,
                        encoded,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SessionError(
                    f"seq {assigned} already exists for session {session!r}"
                ) from exc
            new_last_seq = max(last_seq, assigned)
            new_message_count = message_count
            new_title = title
            new_title_source = title_source
            if (
                kind == "message"
                and isinstance(payload, Message)
                and payload.role == "user"
            ):
                new_message_count += 1
                if not new_title:
                    candidate = export_mod.derive_title([payload])
                    if candidate:
                        new_title = candidate
                        new_title_source = "first_message"
            new_created_at = (
                created_at if created_at > 0 else (ts if ts > 0 else created_at)
            )
            new_activity = max(last_activity, ts) if ts > 0 else last_activity
            conn.execute(
                "UPDATE sessions SET last_seq=?, title=?, title_source=?, message_count=?, "
                "created_at=?, last_activity=?, completion_seq=CASE "
                "WHEN ? THEN MAX(completion_seq, ?) ELSE completion_seq END "
                "WHERE project_id=? AND namespace=? AND id=?",
                (
                    new_last_seq,
                    new_title,
                    new_title_source,
                    new_message_count,
                    new_created_at,
                    new_activity,
                    kind == "event"
                    and isinstance(payload, Event)
                    and payload.type
                    in {"turn.completed", "turn.failed", "turn.cancelled"},
                    assigned,
                    self.project_id,
                    self.namespace,
                    session,
                ),
            )
        if kind == "event":
            return EventRecord(seq=assigned, event=payload, ts=ts)
        if kind == "message":
            return MessageRecord(seq=assigned, message=payload, ts=ts)
        return msgspec.json.decode(encoded, type=SummaryRecord)

    # -- session summary / listing -----------------------------------------

    def session_row(self, session: str) -> dict[str, Any] | None:
        session = validate_session_id(session)
        with self._draft_lock:
            if session in self._drafts:
                fork = next(
                    (
                        r.event.data
                        for r in self._drafts[session]
                        if isinstance(r, EventRecord)
                        and r.event.type == "session.forked"
                    ),
                    {},
                )
                return {
                    "id": session,
                    "title": "",
                    "title_source": "",
                    "created_at": min(
                        (r.ts for r in self._drafts[session]), default=0.0
                    ),
                    "last_activity": max(
                        (r.ts for r in self._drafts[session]), default=0.0
                    ),
                    "last_seq": self.next_seq(session) - 1,
                    "message_count": 0,
                    "completion_seq": 0,
                    "parent_id": fork.get("parent", ""),
                    "fork_seq": fork.get("at_seq", 0),
                    "archived_at": None,
                    "trash_id": None,
                }
        row = (
            self.db._connection()
            .execute(
                "SELECT * FROM sessions WHERE project_id=? AND namespace=? AND id=?",
                (self.project_id, self.namespace, session),
            )
            .fetchone()
        )
        return dict(row) if row is not None else None

    def list_rows(
        self, *, include_trashed: bool = False, include_archived: bool = True
    ) -> list[dict[str, Any]]:
        clauses = ["project_id=?", "namespace=?", _has_user_input()]
        params: list[Any] = [self.project_id, self.namespace]
        if not include_trashed:
            clauses.append("trash_id IS NULL")
        if not include_archived:
            clauses.append("archived_at IS NULL")
        query = "SELECT * FROM sessions WHERE " + " AND ".join(clauses)
        rows = self.db._connection().execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def set_auto_title(self, session: str, title: str) -> bool:
        """Replace a still-derived title with a model-written one.

        One conditional ``UPDATE``: only a row whose title came from the first
        message changes, so a title the user set (or one already written) always
        wins, even against a slow side call. Returns whether a row changed.
        """
        session = validate_session_id(session)
        if not isinstance(title, str) or not title.strip():
            return False
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE sessions SET title=?, title_source='auto' "
                "WHERE project_id=? AND namespace=? AND id=? AND title_source='first_message'",
                (title, self.project_id, self.namespace, session),
            )
            return cur.rowcount > 0

    def delete_row(self, session: str) -> None:
        """Hard-delete a session row and its records/snapshot (cascades)."""
        session = validate_session_id(session)
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE project_id=? AND namespace=? AND id=?",
                (self.project_id, self.namespace, session),
            )

    # -- archive -------------------------------------------------------

    def archive(self, session: str, reason: str) -> dict[str, Any] | None:
        session = validate_session_id(session)
        now = time.time()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT archived_at, archive_reason FROM sessions "
                "WHERE project_id=? AND namespace=? AND id=? AND trash_id IS NULL",
                (self.project_id, self.namespace, session),
            ).fetchone()
            if row is None:
                return None
            if row[0] is not None:
                return {"session_id": session, "archived_at": row[0], "reason": row[1]}
            conn.execute(
                "UPDATE sessions SET archived_at=?, archive_reason=? "
                "WHERE project_id=? AND namespace=? AND id=?",
                (now, reason, self.project_id, self.namespace, session),
            )
        return {"session_id": session, "archived_at": now, "reason": reason}

    def unarchive(self, session: str) -> bool:
        session = validate_session_id(session)
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE sessions SET archived_at=NULL, archive_reason=NULL "
                "WHERE project_id=? AND namespace=? AND id=? AND archived_at IS NOT NULL "
                "AND trash_id IS NULL",
                (self.project_id, self.namespace, session),
            )
            return cur.rowcount > 0

    def archived_rows(self) -> list[dict[str, Any]]:
        rows = (
            self.db._connection()
            .execute(
                "SELECT id AS session_id, archived_at, archive_reason AS reason FROM sessions "
                "WHERE project_id=? AND namespace=? AND archived_at IS NOT NULL AND trash_id IS NULL",
                (self.project_id, self.namespace),
            )
            .fetchall()
        )
        return [dict(row) for row in rows]

    def stale_candidates(self, *, threshold: float, limit: int) -> list[str]:
        """Up to ``limit`` idle, unarchived, untrashed session ids, oldest first.

        An indexed ``ORDER BY last_activity ASC LIMIT`` query is bounded and
        deterministic, so no cross-call cursor bookkeeping is needed.
        """
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        rows = (
            self.db._connection()
            .execute(
                "SELECT id FROM sessions WHERE project_id=? AND namespace=? AND "
                "trash_id IS NULL AND archived_at IS NULL AND last_activity <= ? "
                "ORDER BY last_activity ASC LIMIT ?",
                (self.project_id, self.namespace, threshold, min(limit, _MAX_LIMIT)),
            )
            .fetchall()
        )
        return [row[0] for row in rows]

    # -- trash -----------------------------------------------------------

    def trash(
        self, session: str, *, reason: str, retention_seconds: float
    ) -> dict[str, Any] | None:
        session = validate_session_id(session)
        now = time.time()
        token = secrets.token_hex(6)
        trash_id = f"{session}-{token}"
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT title, last_seq FROM sessions "
                "WHERE project_id=? AND namespace=? AND id=? AND trash_id IS NULL",
                (self.project_id, self.namespace, session),
            ).fetchone()
            if row is None:
                return None
            title, last_seq = row
            conn.execute(
                "UPDATE sessions SET trash_id=?, trashed_at=?, trash_expires_at=?, "
                "trash_reason=? "
                "WHERE project_id=? AND namespace=? AND id=?",
                (
                    trash_id,
                    now,
                    now + retention_seconds,
                    reason,
                    self.project_id,
                    self.namespace,
                    session,
                ),
            )
        return {
            "trash_id": trash_id,
            "session_id": session,
            "trashed_at": now,
            "delete_after": now + retention_seconds,
            "title": title,
            "last_seq": last_seq,
            "reason": reason,
        }

    def find_trash(self, trash_id: str) -> dict[str, Any] | None:
        row = (
            self.db._connection()
            .execute(
                "SELECT * FROM sessions WHERE project_id=? AND namespace=? AND "
                "(trash_id=? OR (id=? AND trash_id IS NOT NULL))",
                (self.project_id, self.namespace, trash_id, trash_id),
            )
            .fetchone()
        )
        return dict(row) if row is not None else None

    def restore(self, trash_id: str) -> str | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE project_id=? AND namespace=? AND "
                "(trash_id=? OR (id=? AND trash_id IS NOT NULL))",
                (self.project_id, self.namespace, trash_id, trash_id),
            ).fetchone()
            if row is None:
                return None
            session = row[0]
            conn.execute(
                "UPDATE sessions SET trash_id=NULL, trashed_at=NULL, "
                "trash_expires_at=NULL, trash_reason=NULL "
                "WHERE project_id=? AND namespace=? AND id=?",
                (self.project_id, self.namespace, session),
            )
        return session

    def trashed_rows(self) -> list[dict[str, Any]]:
        rows = (
            self.db._connection()
            .execute(
                "SELECT * FROM sessions WHERE project_id=? AND namespace=? AND trash_id IS NOT NULL",
                (self.project_id, self.namespace),
            )
            .fetchall()
        )
        return [dict(row) for row in rows]

    def purge_expired(self, *, now: float | None = None) -> list[str]:
        moment = time.time() if now is None else now
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT trash_id FROM sessions WHERE project_id=? AND namespace=? AND "
                "trash_id IS NOT NULL AND trash_expires_at < ?",
                (self.project_id, self.namespace, moment),
            ).fetchall()
            ids = [row[0] for row in rows]
            conn.execute(
                "DELETE FROM sessions WHERE project_id=? AND namespace=? AND "
                "trash_id IS NOT NULL AND trash_expires_at < ?",
                (self.project_id, self.namespace, moment),
            )
        return ids

    # -- snapshots -----------------------------------------------------

    def load_snapshot(
        self, session: str, read: ReadResult
    ) -> snapshot_mod.Snapshot | None:
        session = validate_session_id(session)
        row = (
            self.db._connection()
            .execute(
                "SELECT body FROM snapshots WHERE project_id=? AND namespace=? AND session_id=?",
                (self.project_id, self.namespace, session),
            )
            .fetchone()
        )
        if row is None:
            return None
        try:
            decoded = msgspec.json.decode(row[0], type=snapshot_mod.Snapshot)
        except (msgspec.DecodeError, msgspec.ValidationError):
            return None
        return snapshot_mod.validate(decoded, session, read)

    def write_snapshot(self, session: str, snapshot: snapshot_mod.Snapshot) -> None:
        session = validate_session_id(session)
        with self._draft_lock:
            if session in self._drafts:
                return
        encoded = msgspec.json.encode(snapshot)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO snapshots(project_id, namespace, session_id, seq, body) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(project_id, namespace, session_id) DO UPDATE SET "
                "seq=excluded.seq, body=excluded.body",
                (self.project_id, self.namespace, session, snapshot.seq, encoded),
            )

    # -- kv (archive cursor, etc.) ---------------------------------------

    def kv_get(self, key: str) -> str | None:
        row = (
            self.db._connection()
            .execute(
                "SELECT value FROM kv WHERE project_id=? AND namespace=? AND key=?",
                (self.project_id, self.namespace, key),
            )
            .fetchone()
        )
        return row[0] if row is not None else None

    def kv_set(self, key: str, value: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO kv(project_id, namespace, key, value) VALUES (?,?,?,?) "
                "ON CONFLICT(project_id, namespace, key) DO UPDATE SET value=excluded.value",
                (self.project_id, self.namespace, key, value),
            )


def _kind_of(record: SessionRecord) -> str:
    if isinstance(record, EventRecord):
        return "event"
    if isinstance(record, MessageRecord):
        return "message"
    return "summary"


__all__ = [
    "SCHEMA_VERSION",
    "SqliteSessionStore",
    "StateDatabase",
]
