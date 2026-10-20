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
from typing import Any, Mapping

import msgspec

from ..errors import RequestIdConflict, SessionError
from ..events import Event
from ..model.message import Message
from . import export as export_mod
from . import snapshot as snapshot_mod
from .child_ids import stored_child_id
from .ids import validate_session_id
from .names import allocate as _allocate_name
from .records import (
    SESSION_LOG_VERSION,
    EventRecord,
    MessageRecord,
    ReadResult,
    SessionRecord,
    SummaryRecord,
)


def _backfill_ownership(conn: sqlite3.Connection) -> None:
    """Bounded, project-scoped evidence recovery; ambiguous rows stay unknown.

    Inspect at most 10,000 evidence records per project. Spawn/completion
    metadata and a child's recorded request are evidence; slug resemblance is
    not. Relayed nested events must name their actual parent session explicitly.
    """
    conn.execute("UPDATE sessions SET kind='root', root_id=id WHERE namespace='main'")
    conn.execute(
        "UPDATE sessions SET context_head_seq=COALESCE((SELECT max(seq) FROM records r "
        "WHERE r.project_id=sessions.project_id AND r.namespace=sessions.namespace "
        "AND r.session_id=sessions.id AND r.kind='summary'),0)"
    )
    projects = conn.execute("SELECT id FROM projects").fetchall()
    for project_row in projects:
        project = project_row[0]
        rows = conn.execute(
            "SELECT namespace,id,root_id FROM sessions WHERE project_id=?", (project,)
        ).fetchall()
        identities = {(r["namespace"], r["id"]) for r in rows}
        roots = {(r["namespace"], r["id"]): r["root_id"] for r in rows}
        candidates: dict[str, set[tuple[Any, ...]]] = {}
        evidence = conn.execute(
            "SELECT namespace,session_id,seq,body FROM records WHERE project_id=? "
            "AND kind='event' AND CASE WHEN json_valid(body) THEN (json_extract(body,'$.event.type') IN "
            "('agent.spawned','agent.completed','context.assembled','turn.started')) ELSE 0 END "
            "ORDER BY namespace,session_id,seq LIMIT 10000", (project,)
        ).fetchall()
        for record in evidence:
            try:
                event = msgspec.json.decode(record["body"], type=EventRecord).event
            except (msgspec.DecodeError, msgspec.ValidationError):
                continue
            data = event.data
            request = data.get("request") if event.type == "context.assembled" else None
            if request is not None:
                # Request snapshots are stored in the child's own log.
                meta = request.get("agent") if isinstance(request, dict) else None
            else:
                meta = data.get("agent")
            if not isinstance(meta, dict):
                continue
            logical = meta.get("session") or meta.get("logical_session_id") or meta.get("id")
            if not isinstance(logical, str) or not logical:
                continue
            child = stored_child_id(logical)
            if ("agents", logical) in identities:
                child = logical
            if ("agents", child) not in identities:
                continue
            parent = meta.get("parent_session")
            nested = bool(meta.get("parent_agent_id"))
            # Old direct root spawn events did not always carry parent_session.
            if not parent and event.type in {"agent.spawned", "agent.completed"} and record["namespace"] == "main" and not nested and logical.startswith(record["session_id"] + "/sub/"):
                suffix = logical[len(record["session_id"]) + 5:]
                if suffix.isdigit():
                    parent = record["session_id"]
            if not isinstance(parent, str) or not parent:
                continue
            namespace = "agents" if nested else "main"
            if (namespace, parent) not in identities and nested:
                parent = stored_child_id(parent)
            if (namespace, parent) not in identities:
                continue
            # A relayed copy is never assigned its containing root's sequence.
            spawn_seq = record["seq"] if event.type == "agent.spawned" and (record["namespace"], record["session_id"]) == (namespace, parent) else 0
            turn = str(event.turn or "") if spawn_seq else str(meta.get("owner_turn_id") or "")
            if not turn and namespace == "main":
                turn = str(meta.get("root_turn_id") or "")
            call = meta.get("parent_call_id")
            if call is not None and not isinstance(call, str):
                continue
            edge = (namespace, parent, turn, call, str(meta.get("id") or logical), logical)
            if (request is not None or event.type == "turn.started") and (record["namespace"], record["session_id"]) != ("agents", child):
                continue
            candidates.setdefault(child, set()).add((*edge, spawn_seq))
        accepted: dict[str, tuple[Any, ...]] = {}
        for child, edges in candidates.items():
            # Repeated spawn/completion evidence may differ only in sequence.
            identities_and_calls = {(edge[0], edge[1], edge[3], edge[4], edge[5]) for edge in edges}
            turns = {edge[2] for edge in edges if edge[2]}
            if len(identities_and_calls) != 1 or len(turns) > 1:
                continue
            namespace, parent, call, agent, logical = next(iter(identities_and_calls))
            base = (namespace, parent, next(iter(turns), ""), call, agent, logical)
            seqs = {edge[6] for edge in edges if edge[6]}
            if len(seqs) > 1:
                continue
            accepted[child] = (*base, next(iter(seqs), 0))
        # Resolve roots only along accepted edges, with cycle/depth protection.
        def root_for(child: str, seen: frozenset[str] = frozenset()) -> str:
            if child in seen or len(seen) >= 64:
                return ""
            edge = accepted.get(child)
            if edge is None:
                return roots.get(("agents", child), "")
            namespace, parent = edge[:2]
            if namespace == "main":
                return parent
            return root_for(parent, seen | {child})

        for child, edge in accepted.items():
            # Cycles are contradictory evidence, not an ownership tree.
            cursor, seen = child, set()
            cyclic = False
            for _ in range(65):
                if cursor in seen:
                    cyclic = True
                    break
                seen.add(cursor)
                candidate = accepted.get(cursor)
                if candidate is None or candidate[0] != "agents":
                    break
                cursor = candidate[1]
            if cyclic:
                continue
            namespace, parent, turn, call, agent, logical, seq = edge
            conn.execute(
                "UPDATE sessions SET owner_namespace=?,owner_id=?,owner_turn_id=?,"
                "owner_call_id=?,agent_id=?,logical_session_id=?,owner_spawn_seq=?,"
                "owner_agent_id=?,kind='child',root_id=? WHERE project_id=? "
                "AND namespace='agents' AND id=? AND owner_id=''",
                (namespace, parent, turn, call, agent, logical, seq, agent,
                 root_for(child), project, child),
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
SCHEMA_VERSION = 9

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
    # v4: bounded display snippets for the sessions dashboard. They stay off
    # SessionSummary. A Python backfill fills existing rows after these ALTERs.
    (
        "ALTER TABLE sessions ADD COLUMN last_user TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN last_agent TEXT NOT NULL DEFAULT ''",
    ),
    # v5: timestamp of the latest user prompt (tool results excluded). The
    # dashboard orders by it, so agent and tool activity, which advance
    # last_activity, never reshuffle sessions. A Python backfill fills old rows.
    ("ALTER TABLE sessions ADD COLUMN last_user_at REAL NOT NULL DEFAULT 0",),
    # v6: ownership is distinct from fork lineage (parent_id/fork_seq).
    (
        "ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN root_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN owner_namespace TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN owner_agent_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN owner_turn_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN owner_spawn_seq INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN context_head_seq INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN handoff_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN owner_call_id TEXT",
        "ALTER TABLE sessions ADD COLUMN agent_id TEXT",
        "ALTER TABLE sessions ADD COLUMN logical_session_id TEXT",
        "CREATE INDEX sessions_by_root ON sessions(project_id, root_id)",
        "CREATE INDEX sessions_by_owner ON sessions(project_id, owner_namespace, owner_id)",
        """CREATE TABLE submissions (
          project_id TEXT NOT NULL, namespace TEXT NOT NULL, session_id TEXT NOT NULL,
          request_id TEXT NOT NULL, content_digest TEXT NOT NULL,
          seq INTEGER NOT NULL, turn_id TEXT NOT NULL DEFAULT '', queued_id TEXT NOT NULL DEFAULT '',
          created_at REAL NOT NULL,
          PRIMARY KEY (project_id, namespace, session_id, request_id),
          FOREIGN KEY (project_id, namespace, session_id)
            REFERENCES sessions(project_id, namespace, id) ON DELETE CASCADE
        )""",
    ),
    # v7: durable allocation reservations; distinct from session/fork ownership.
    (
        """CREATE TABLE child_allocations (
          project_id TEXT NOT NULL, parent_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
          logical_id TEXT NOT NULL, stored_id TEXT NOT NULL,
          PRIMARY KEY(project_id,parent_id,ordinal),
          UNIQUE(project_id,logical_id), UNIQUE(project_id,stored_id)
        )""",
    ),
    (
        "ALTER TABLE sessions ADD COLUMN name TEXT",
        "CREATE UNIQUE INDEX sessions_by_name ON sessions(name) WHERE name IS NOT NULL",
        "CREATE TABLE session_names (name TEXT PRIMARY KEY, project_id TEXT NOT NULL, namespace TEXT NOT NULL, logical_id TEXT NOT NULL, created_at REAL NOT NULL, retired_at REAL)",
        "CREATE UNIQUE INDEX session_names_by_identity ON session_names(project_id,namespace,logical_id) WHERE retired_at IS NULL",
    ),
    ("ALTER TABLE child_allocations ADD COLUMN context_read INTEGER NOT NULL DEFAULT 0",),
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
                    if version < 4 <= SCHEMA_VERSION:
                        _backfill_message_snippets(conn)
                    if version < 5 <= SCHEMA_VERSION:
                        _backfill_last_user_at(conn)
                    if version < 7 <= SCHEMA_VERSION:
                        _backfill_ownership(conn)
                    if version < SCHEMA_VERSION:
                        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
            # Names are backfilled after the schema commits, in bounded batches (plan 03 U2).
            _backfill_names(self._raw_connect)

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
            try:
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

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
            try:
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

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
            if root == project_id:
                # A bare id is never a real root: it must not overwrite a recorded path
                # (the dashboard opens sessions by ``projects.root``).
                conn.execute(
                    "INSERT INTO projects(id, root, created_at, last_opened) VALUES (?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET last_opened=excluded.last_opened",
                    (project_id, root, now, now),
                )
                return
            conn.execute(
                "INSERT INTO projects(id, root, created_at, last_opened) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET root=excluded.root, "
                "last_opened=excluded.last_opened",
                (project_id, root, now, now),
            )

    def project_sessions(self, *, limit: int = 1000, query: str = "") -> list[dict[str, Any]]:
        """Bounded cross-project sidebar index; excludes children, archive and trash."""
        if not 1 <= limit <= _MAX_LIMIT:
            raise ValueError("invalid project session limit")
        if not isinstance(query, str) or len(query) > 256:
            raise ValueError("project session query must be at most 256 characters")
        search = ""
        params: list[Any] = []
        if query:
            pattern = "%" + query.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            search = "AND (" + " OR ".join(
                f"{column} LIKE ? ESCAPE '!' COLLATE NOCASE"
                for column in ("s.title", "s.id", "s.name", "s.last_user", "s.last_agent", "p.root")
            ) + ") "
            params.extend([pattern] * 6)
        params.append(limit)
        return [
            dict(row)
            for row in self._connection().execute(
                "SELECT s.*, p.root AS workspace FROM sessions s "
                "JOIN projects p ON p.id=s.project_id "
                "WHERE s.namespace='main' AND s.kind<>'manager' "
                "AND s.archived_at IS NULL AND s.trash_id IS NULL "
                f"AND {_has_user_input('s')} "
                + search
                + "ORDER BY CASE WHEN s.last_user_at > 0 THEN s.last_user_at "
                "WHEN s.created_at > 0 THEN s.created_at ELSE s.last_activity END DESC, "
                "s.project_id, s.id LIMIT ?",
                params,
            )
        ]

    def archived_project_sessions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """The newest archived sessions across projects (same row shape as ``project_sessions``)."""
        if not 1 <= limit <= _MAX_LIMIT:
            raise ValueError("invalid project session limit")
        return [
            dict(row)
            for row in self._connection().execute(
                "SELECT s.*, p.root AS workspace FROM sessions s "
                "JOIN projects p ON p.id=s.project_id "
                "WHERE s.namespace='main' AND s.kind<>'manager' "
                "AND s.archived_at IS NOT NULL AND s.trash_id IS NULL "
                f"AND {_has_user_input('s')} "
                "ORDER BY s.archived_at DESC, s.project_id, s.id LIMIT ?",
                [limit],
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


def _backfill_message_snippets(conn: sqlite3.Connection) -> None:
    """Fill ``last_user`` and ``last_agent`` from message records. Schema 4 only."""
    sessions = conn.execute("SELECT project_id, namespace, id FROM sessions").fetchall()
    for project_id, namespace, session_id in sessions:
        last_user = ""
        last_agent = ""
        rows = conn.execute(
            "SELECT body FROM records WHERE project_id=? AND namespace=? "
            "AND session_id=? AND kind='message' ORDER BY seq ASC",
            (project_id, namespace, session_id),
        ).fetchall()
        for (body,) in rows:
            try:
                record = _decode_record(body)
            except (SessionError, msgspec.DecodeError, msgspec.ValidationError):
                continue
            if not isinstance(record, MessageRecord):
                continue
            if record.message.meta.notice is not None:
                continue
            snippet = export_mod.message_snippet(record.message)
            if not snippet:
                continue
            if record.message.role == "user":
                last_user = snippet
            elif record.message.role == "assistant":
                last_agent = snippet
        if last_user or last_agent:
            conn.execute(
                "UPDATE sessions SET last_user=?, last_agent=? "
                "WHERE project_id=? AND namespace=? AND id=?",
                (last_user, last_agent, project_id, namespace, session_id),
            )


def _backfill_last_user_at(conn: sqlite3.Connection) -> None:
    """Fill ``last_user_at`` from the newest user prompt or queued input. Schema 5 only."""
    sessions = conn.execute("SELECT project_id, namespace, id FROM sessions").fetchall()
    for project_id, namespace, session_id in sessions:
        newest = 0.0
        rows = conn.execute(
            "SELECT ts, body FROM records WHERE project_id=? AND namespace=? "
            "AND session_id=? AND kind IN ('message', 'event') ORDER BY seq ASC",
            (project_id, namespace, session_id),
        ).fetchall()
        for ts, body in rows:
            try:
                record = _decode_record(body)
            except (SessionError, msgspec.DecodeError, msgspec.ValidationError):
                continue
            if isinstance(record, MessageRecord) and export_mod.is_user_prompt(record.message):
                newest = max(newest, ts)
            elif isinstance(record, EventRecord) and record.event.type == "input.queued":
                newest = max(newest, ts)
        if newest:
            conn.execute(
                "UPDATE sessions SET last_user_at=? WHERE project_id=? AND namespace=? AND id=?",
                (newest, project_id, namespace, session_id),
            )


def _append_in_txn(conn, project_id, namespace, session, requested_seq, kind, ts, build):
    row = conn.execute(
        "SELECT last_seq, title, message_count, created_at, last_activity, title_source, "
        "last_user, last_agent, last_user_at FROM sessions "
        "WHERE project_id=? AND namespace=? AND id=?",
        (project_id, namespace, session),
    ).fetchone()
    if row is None:
        raise SessionError(f"Session {session!r} does not exist")
    (
        last_seq,
        title,
        message_count,
        created_at,
        last_activity,
        title_source,
        last_user,
        last_agent,
        last_user_at,
    ) = row
    assigned = requested_seq if requested_seq is not None else last_seq + 1
    payload, encoded = build(assigned)
    try:
        conn.execute(
            "INSERT INTO records"
            "(project_id, namespace, session_id, seq, kind, ts, body) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                project_id,
                namespace,
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
    if kind == "summary":
        conn.execute("UPDATE sessions SET context_head_seq=? "
            "WHERE project_id=? AND namespace=? AND id=?",
            (assigned, project_id, namespace, session))
    new_message_count = message_count
    new_title = title
    new_title_source = title_source
    new_last_user = last_user
    new_last_agent = last_agent
    new_last_user_at = last_user_at
    if (
        (kind == "message" and isinstance(payload, Message) and export_mod.is_user_prompt(payload))
        or (kind == "event" and isinstance(payload, Event) and payload.type == "input.queued")
    ) and ts > 0:
        new_last_user_at = max(last_user_at, ts)
    if kind == "message" and isinstance(payload, Message) and payload.meta.notice is not None:
        pass  # a harness notice is not a user message: no count, title or last prompt
    elif kind == "message" and isinstance(payload, Message) and payload.role == "user":
        new_message_count += 1
        snippet = export_mod.message_snippet(payload)
        if snippet:
            new_last_user = snippet
        if not new_title:
            candidate = export_mod.derive_title([payload])
            if candidate:
                new_title = candidate
                new_title_source = "first_message"
    elif kind == "message" and isinstance(payload, Message) and payload.role == "assistant":
        snippet = export_mod.message_snippet(payload)
        if snippet:
            new_last_agent = snippet
    new_created_at = (
        created_at if created_at > 0 else (ts if ts > 0 else created_at)
    )
    new_activity = max(last_activity, ts) if ts > 0 else last_activity
    conn.execute(
        "UPDATE sessions SET last_seq=?, title=?, title_source=?, message_count=?, "
        "created_at=?, last_activity=?, last_user=?, last_agent=?, last_user_at=?, completion_seq=CASE "
        "WHEN ? THEN MAX(completion_seq, ?) ELSE completion_seq END "
        "WHERE project_id=? AND namespace=? AND id=?",
        (
            new_last_seq,
            new_title,
            new_title_source,
            new_message_count,
            new_created_at,
            new_activity,
            new_last_user,
            new_last_agent,
            new_last_user_at,
            kind == "event"
            and isinstance(payload, Event)
            and payload.type
            in {"turn.completed", "turn.failed", "turn.cancelled"},
            assigned,
            project_id,
            namespace,
            session,
        ),
    )
    if kind == "event":
        return EventRecord(seq=assigned, event=payload, ts=ts)
    if kind == "message":
        return MessageRecord(seq=assigned, message=payload, ts=ts)
    return msgspec.json.decode(encoded, type=SummaryRecord)


def allocate_name(conn, project_id, namespace, session, *, preferred=None):
    return _allocate_name(conn, project_id, namespace, session, preferred=preferred)


_NAME_BACKFILL_BATCH = 500


def _backfill_names(conn_factory) -> int:
    """Name unnamed sessions in deterministic batches of <= 500, one transaction each (plan 03 U2).

    Idempotent: only rows with ``name IS NULL`` are touched, so a crash or a
    second run resumes where it stopped. Returns the number of sessions named.
    """
    total = 0
    conn = conn_factory()
    try:
        while True:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT project_id,namespace,id FROM sessions WHERE name IS NULL "
                    "ORDER BY project_id,namespace,id LIMIT ?", (_NAME_BACKFILL_BATCH,)).fetchall()
                for row in rows:
                    allocate_name(conn, *row)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            total += len(rows)
            if len(rows) < _NAME_BACKFILL_BATCH:
                return total
    finally:
        conn.close()


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
        register_project: bool = True,
    ):
        self.db = db
        self.project_id = project_id
        self.namespace = namespace
        if register_project:
            self.db.touch_project(project_id, root if root is not None else project_id)
        self._lock_dir = Path(lock_dir) if lock_dir is not None else None
        self._fsync = lambda fd: None  # durability is PRAGMA-provided
        self._drafts: dict[str, list[SessionRecord]] = {}
        self._draft_metadata: dict[str, Mapping[str, Any]] = {}
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

            allocate_name(conn, self.project_id, self.namespace, session)

    def create_draft(self, session: str, *, metadata: Mapping[str, Any] | None = None) -> None:
        """Reserve a chat in memory until its first accepted user input."""
        session = validate_session_id(session)
        with self._draft_lock:
            if not self.row_exists(session):
                self._drafts[session] = []
                if metadata:
                    self._draft_metadata[session] = dict(metadata)

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
        meta: Mapping[str, Any] | None = None,
        _conn: sqlite3.Connection | None = None,
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
            and r.message.meta.notice is None
        )
        title = ""
        last_user = ""
        last_agent = ""
        last_user_at = 0.0
        for r in records:
            if isinstance(r, EventRecord) and r.event.type == "input.queued":
                last_user_at = max(last_user_at, r.ts)
            if not isinstance(r, MessageRecord) or r.message.meta.notice is not None:
                continue
            if export_mod.is_user_prompt(r.message):
                last_user_at = max(last_user_at, r.ts)
            if r.message.role == "user" and not title:
                title = export_mod.derive_title([r.message])
            snippet = export_mod.message_snippet(r.message)
            if not snippet:
                continue
            if r.message.role == "user":
                last_user = snippet
            elif r.message.role == "assistant":
                last_agent = snippet
        try:
            with (self.db.transaction() if _conn is None else contextlib.nullcontext(_conn)) as conn:
                conn.execute(
                    "INSERT INTO sessions"
                    "(project_id, namespace, id, created_at, last_seq, completion_seq, last_activity,"
                    " message_count, title, title_source, parent_id, fork_seq, archived_at, archive_reason,"
                    " trash_id, trashed_at, trash_expires_at, trash_reason, last_user, last_agent, last_user_at,"
                    " owner_namespace, owner_id, owner_turn_id, owner_call_id, agent_id, logical_session_id,"
                    " kind, root_id, owner_agent_id, owner_spawn_seq)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        last_user,
                        last_agent,
                        last_user_at,
                        *((meta or {}).get(key) or "" for key in (
                            "owner_namespace", "owner_id", "owner_turn_id", "owner_call_id",
                            "agent_id", "logical_session_id",
                        )),
                        (meta or {}).get("kind", "root" if self.namespace == "main" else ("child" if (meta or {}).get("owner_id") else "")),
                        (meta or {}).get("root_id", session if self.namespace == "main" else ""),
                        (meta or {}).get("owner_agent_id", (meta or {}).get("agent_id", "")),
                        (meta or {}).get("owner_spawn_seq", 0),
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
                carried = next((r.event.data.get("name") for r in records
                                if isinstance(r, EventRecord) and r.event.type == "session.named"), None)
                allocate_name(conn, self.project_id, self.namespace, session, preferred=carried)
                heads = [r for r in records if isinstance(r, SummaryRecord)]
                if heads:
                    conn.execute("UPDATE sessions SET context_head_seq=?, handoff_count=? "
                        "WHERE project_id=? AND namespace=? AND id=?",
                        (heads[-1].seq, sum(r.strategy == "handoff" for r in heads),
                         self.project_id, self.namespace, session))
        except sqlite3.IntegrityError as exc:
            raise SessionError(f"Session log already exists: {session}") from exc

    # -- reading ---------------------------------------------------------

    def context_head(self, session: str) -> SummaryRecord | None:
        """Indexed head read; no history scan or recovery side effects."""
        session = validate_session_id(session)
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT r.body FROM sessions s JOIN records r ON "
                "r.project_id=s.project_id AND r.namespace=s.namespace "
                "AND r.session_id=s.id AND r.seq=s.context_head_seq "
                "WHERE s.project_id=? AND s.namespace=? AND s.id=?",
                (self.project_id, self.namespace, session),
            ).fetchone()
        record = _decode_record(row[0]) if row else None
        return record if isinstance(record, SummaryRecord) else None

    def handoff_record(self, session: str, handoff_id: str):
        session = validate_session_id(session)
        if not isinstance(handoff_id, str) or len(handoff_id) != 16 or any(c not in "0123456789abcdef" for c in handoff_id):
            raise ValueError("Invalid handoff id")
        row = self.db._connection().execute(
            "SELECT body FROM records WHERE project_id=? AND namespace=? AND session_id=? "
            "AND kind='summary' AND json_extract(body,'$.handoff_id')=? LIMIT 1",
            (self.project_id, self.namespace, session, handoff_id),
        ).fetchone()
        return _decode_record(row[0]) if row else None

    def active_context_records(self, session: str):
        """Consistent model-only read: indexed head plus retained message range.

        A later ordinary compaction artifact must not resurrect the transcript
        retired by the most recent handoff. Historical reads use log derivation.
        """
        session = validate_session_id(session)
        with self._draft_lock:
            if session in self._drafts:
                return tuple(self._drafts[session]), None
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT r.body FROM sessions s JOIN records r ON "
                "r.project_id=s.project_id AND r.namespace=s.namespace "
                "AND r.session_id=s.id AND r.seq=s.context_head_seq "
                "WHERE s.project_id=? AND s.namespace=? AND s.id=?",
                (self.project_id, self.namespace, session),
            ).fetchone()
            head = _decode_record(row[0]) if row else None
            if head is not None and head.strategy != "handoff":
                row = conn.execute(
                    "SELECT body FROM records WHERE project_id=? AND namespace=? AND session_id=? "
                    "AND kind='summary' AND json_extract(body,'$.strategy')='handoff' ORDER BY seq DESC LIMIT 1",
                    (self.project_id, self.namespace, session),
                ).fetchone()
                head = _decode_record(row[0]) if row else None
            boundary = head.kept_from_seq if head is not None else 0
            rows = conn.execute(
                "SELECT body FROM records WHERE project_id=? AND namespace=? AND session_id=? "
                "AND seq>=? AND kind='message' ORDER BY seq",
                (self.project_id, self.namespace, session, boundary),
            ).fetchall()
        return tuple(_decode_record(row[0]) for row in rows), head

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

    def record_at(self, session: str, seq: int) -> SessionRecord | None:
        """Read one committed record, without opening a handle or recovering."""
        session = validate_session_id(session)
        row = self.db._connection().execute(
            "SELECT body FROM records WHERE project_id=? AND namespace=? AND session_id=? AND seq=?",
            (self.project_id, self.namespace, session, seq),
        ).fetchone()
        return _decode_record(row[0]) if row else None

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

    def pending_submission_sessions(self) -> list[str]:
        """Find live sessions whose admitted input has no consumed/dropped disposition.

        Read-only. Archived and trashed sessions are skipped so inspecting them
        never needs (or implies) reopening them.
        """
        rows = self.db._connection().execute(
            "SELECT DISTINCT s.session_id FROM submissions s "
            "JOIN sessions x ON x.project_id=s.project_id AND x.namespace=s.namespace "
            "AND x.id=s.session_id AND x.archived_at IS NULL AND x.trash_id IS NULL "
            "WHERE s.project_id=? AND s.namespace=? AND s.queued_id<>'' AND NOT EXISTS ("
            "SELECT 1 FROM records r WHERE r.project_id=s.project_id "
            "AND r.namespace=s.namespace AND r.session_id=s.session_id "
            "AND r.seq>s.seq AND r.kind='event' "
            "AND json_extract(r.body, '$.event.type') IN "
            "('input.consumed','input.dropped') "
            "AND json_extract(r.body, '$.event.data.queued_id')=s.queued_id) "
            "ORDER BY s.session_id",
            (self.project_id, self.namespace),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def submission(self, session: str, request_id: str, digest: str):
        """Read an admitted request without writing or acquiring a turn lease."""
        row = self.db._connection().execute(
            "SELECT content_digest, seq, turn_id, queued_id FROM submissions "
            "WHERE project_id=? AND namespace=? AND session_id=? AND request_id=?",
            (self.project_id, self.namespace, session, request_id),
        ).fetchone()
        if row is not None and row[0] != digest:
            raise RequestIdConflict("Request id was already used with different content")
        return row

    def append_batch(self, session, builders, *, row_updates=None, index_rows=()):
        """Append consecutive records and bounded metadata in one write transaction.

        Builders receive their assigned seq and return a SessionRecord. Index rows
        are submission mappings (request_id, content_digest, seq, turn_id,
        queued_id, created_at); callers publish/observe only after this returns.
        Draft setup is promoted with the batch, and retained intact on rollback.
        """
        session = validate_session_id(session)
        builders = tuple(builders)
        index_rows = tuple(index_rows)
        if not builders:
            raise ValueError("append_batch requires at least one record")
        updates = dict(row_updates or {})
        if updates.keys() - {"context_head_seq", "handoff_count"}:
            raise ValueError("Unsupported batch row update")
        with self._draft_lock:
            with self.db.transaction() as conn:
                for row in index_rows:
                    if self.submission(session, row["request_id"], row["content_digest"]) is not None:
                        return []
                draft = self._drafts.get(session)
                if draft is not None:
                    self.create_from_records(
                        session, draft, meta=self._draft_metadata.get(session), _conn=conn,
                    )
                records = []
                for builder in builders:
                    current = conn.execute(
                        "SELECT last_seq FROM sessions WHERE project_id=? AND namespace=? AND id=?",
                        (self.project_id, self.namespace, session),
                    ).fetchone()
                    if current is None:
                        raise SessionError(f"Session {session!r} does not exist")
                    seq = int(current[0]) + 1
                    record = builder(seq)
                    if record.seq != seq:
                        raise ValueError("Batch builder must use its assigned seq")
                    if isinstance(record, EventRecord):
                        record = msgspec.structs.replace(record, event=msgspec.structs.replace(
                            record.event, seq=seq, session=session,
                        ))
                    kind = _kind_of(record)
                    payload = record.event if isinstance(record, EventRecord) else (
                        record.message if isinstance(record, MessageRecord) else None
                    )
                    encoded = msgspec.json.encode(record)
                    records.append(self._append_in_transaction(
                        conn, session, seq, kind, record.ts,
                        lambda assigned, payload=payload, encoded=encoded: (payload, encoded),
                    ))
                heads = [r for r in records if isinstance(r, SummaryRecord)
                         and r.strategy == "handoff"]
                if heads:
                    updates["context_head_seq"] = heads[-1].seq
                    conn.execute(
                        "UPDATE sessions SET handoff_count=handoff_count+? "
                        "WHERE project_id=? AND namespace=? AND id=?",
                        (len(heads), self.project_id, self.namespace, session),
                    )
                for key, value in updates.items():
                    if type(value) is not int or value < 0:
                        raise ValueError("Batch row values must be nonnegative integers")
                    conn.execute(
                        f"UPDATE sessions SET {key}=? WHERE project_id=? AND namespace=? AND id=?",
                        (value, self.project_id, self.namespace, session),
                    )
                for row in index_rows:
                    if row.get("seq", records[-1].seq) not in {record.seq for record in records}:
                        raise ValueError("Submission index must reference a record in its batch")
                    conn.execute(
                        "INSERT INTO submissions (project_id, namespace, session_id, request_id, "
                        "content_digest, seq, turn_id, queued_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (self.project_id, self.namespace, session, row["request_id"],
                         row["content_digest"], row.get("seq", records[-1].seq),
                         row.get("turn_id", ""), row.get("queued_id", ""), row["created_at"]),
                    )
            # Do not drop a draft until COMMIT has succeeded, including deferred errors.
            if draft is not None:
                self._drafts.pop(session, None)
                self._draft_metadata.pop(session, None)
        return records

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
                    self.create_from_records(
                        session, [*records, record], meta=self._draft_metadata.get(session),
                    )
                    del self._drafts[session]
                    self._draft_metadata.pop(session, None)
                else:
                    records.append(record)
                return record
        with self.db.transaction() as conn:
            return self._append_in_transaction(conn, session, requested_seq, kind, ts, build)

    def _append_in_transaction(self, conn, session, requested_seq, kind, ts, build):
        return _append_in_txn(conn, self.project_id, self.namespace, session, requested_seq, kind, ts, build)

    # -- session summary / listing -----------------------------------------

    def resolve_name(self, target: str, *, namespace: str | None = None) -> str:
        """Exact id first, then exact name; workspace scoped, read-only, no drafts."""
        if not isinstance(target, str) or not target or len(target) > 256:
            raise SessionError("Invalid session target")
        conn = self.db._connection()
        scope = " AND namespace=?" if namespace is not None else ""
        args = (self.project_id, target) + ((namespace,) if namespace is not None else ())
        rows = conn.execute("SELECT id FROM sessions WHERE project_id=? AND id=? " + scope + " LIMIT 2", args).fetchall()
        if not rows:
            rows = conn.execute("SELECT id FROM sessions WHERE project_id=? AND name=? " + scope + " LIMIT 2", args).fetchall()
        if len(rows) != 1:
            raise SessionError("Session target is missing or ambiguous")
        return str(rows[0][0])

    def resolve_child_id(self, identity: str) -> str:
        """Resolve an exact stored/logical identity; never hash an existing id."""
        with self._draft_lock:
            if identity in self._drafts:
                return identity
        conn = self.db._connection()
        row = conn.execute(
            "SELECT id FROM sessions WHERE project_id=? AND namespace='agents' "
            "AND id=?", (self.project_id, identity),
        ).fetchone()
        if row is not None:
            return str(row[0])
        rows = conn.execute(
            "SELECT id FROM sessions WHERE project_id=? AND namespace='agents' "
            "AND logical_session_id=? LIMIT 2", (self.project_id, identity),
        ).fetchall()
        if len(rows) > 1:
            raise SessionError("Ambiguous child session identity")
        if rows:
            return str(rows[0][0])
        row = conn.execute(
            "SELECT stored_id FROM child_allocations WHERE project_id=? AND logical_id=?",
            (self.project_id, identity),
        ).fetchone()
        return str(row[0]) if row else stored_child_id(identity)

    def reserve_child_id(self, parent_id: str) -> tuple[int, str]:
        """Atomically reserve a durable ordinal across processes and restarts.

        Existing legacy rows (including trash) and abandoned reservations are
        never reused. A reservation is not a session and confers no ownership.
        """
        with self.db.transaction() as conn:
            last = conn.execute(
                "SELECT COALESCE(MAX(ordinal),0) FROM child_allocations "
                "WHERE project_id=? AND parent_id=?", (self.project_id, parent_id),
            ).fetchone()[0]
            for ordinal in range(last + 1, last + _MAX_LIMIT + 1):
                logical = f"{parent_id}/sub/{ordinal}"
                physical = stored_child_id(logical)
                if conn.execute(
                    "SELECT 1 FROM sessions WHERE project_id=? AND namespace='agents' "
                    "AND (id=? OR logical_session_id=?) LIMIT 1",
                    (self.project_id, physical, logical),
                ).fetchone():
                    continue
                try:
                    conn.execute(
                        "INSERT INTO child_allocations(project_id,parent_id,ordinal,logical_id,stored_id) VALUES(?,?,?,?,?)",
                        (self.project_id, parent_id, ordinal, logical, physical),
                    )
                except sqlite3.IntegrityError:
                    continue
                return ordinal, logical
            raise SessionError("Child allocation scan limit exceeded")

    def grant_child_context_read(self, logical_id: str, *, allowed: bool = False) -> None:
        """Host-only allocation policy; legacy/unknown allocations deny by default."""
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE child_allocations SET context_read=? WHERE project_id=? AND logical_id=?",
                (int(allowed is True), self.project_id, logical_id),
            ).rowcount
            if changed != 1:
                raise SessionError("Unknown child allocation")

    def ownership_tree_rows(
        self, session: str, *, namespace: str, max_depth: int, limit: int = 500,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        """One bounded, project-scoped snapshot of explicit ownership only.

        Root lookup follows at most 64 edges. Missing owners and cycles remain
        unattached; fork ancestry is deliberately not traversed.
        """
        limit = max(1, min(int(limit), 500))
        max_depth = max(0, min(int(max_depth), 64))
        with self.db.read() as conn:
            identity = (namespace, session)
            visited: set[tuple[str, str]] = set()
            for _ in range(64):
                if identity in visited:
                    break
                visited.add(identity)
                row = conn.execute(
                    "SELECT * FROM sessions WHERE project_id=? AND namespace=? AND id=? "
                    "AND trash_id IS NULL", (self.project_id, *identity),
                ).fetchone()
                if row is None:
                    raise SessionError("Session does not exist")
                owner = (row["owner_namespace"], row["owner_id"])
                if not all(owner):
                    break
                if conn.execute(
                    "SELECT 1 FROM sessions WHERE project_id=? AND namespace=? AND id=? "
                    "AND trash_id IS NULL", (self.project_id, *owner),
                ).fetchone() is None:
                    break
                identity = owner
            rows = conn.execute(
                """WITH RECURSIVE tree(namespace,id,depth,path) AS (
                    SELECT namespace,id,0,'|'||namespace||':'||id||'|' FROM sessions
                    WHERE project_id=? AND namespace=? AND id=? AND trash_id IS NULL
                    UNION ALL
                    SELECT s.namespace,s.id,t.depth+1,t.path||s.namespace||':'||s.id||'|'
                    FROM sessions s JOIN tree t
                      ON s.owner_namespace=t.namespace AND s.owner_id=t.id
                    WHERE s.project_id=? AND s.trash_id IS NULL AND t.depth<?
                      AND instr(t.path,'|'||s.namespace||':'||s.id||'|')=0
                    ORDER BY 3,1,2 LIMIT ?
                ) SELECT s.*,t.depth,
                    EXISTS(SELECT 1 FROM sessions c WHERE c.project_id=s.project_id
                      AND c.owner_namespace=s.namespace AND c.owner_id=s.id
                      AND c.trash_id IS NULL) AS has_children
                    FROM tree t JOIN sessions s ON s.project_id=?
                      AND s.namespace=t.namespace AND s.id=t.id
                    ORDER BY t.depth,s.namespace,s.id""",
                (self.project_id, *identity, self.project_id, max_depth,
                 limit + 1, self.project_id),
            ).fetchall()
            truncated = len(rows) > limit or any(
                row["depth"] == max_depth and row["has_children"] for row in rows
            )
            # Unknown legacy children are visible, never silently adopted.
            remaining = max(0, limit - min(len(rows), limit))
            unknown = conn.execute(
                "SELECT *,0 AS depth,0 AS has_children FROM sessions WHERE project_id=? "
                "AND namespace='agents' AND kind='' AND trash_id IS NULL "
                "AND NOT (namespace=? AND id=?) ORDER BY id LIMIT ?",
                (self.project_id, *identity, remaining + 1),
            ).fetchall()
            truncated = truncated or len(unknown) > remaining
            return ([dict(row) for row in rows[:limit]],
                    [dict(row) for row in unknown[:remaining]], bool(truncated))

    def unattached_count(self) -> int:
        """Project-local diagnostic count, without reading any session log."""
        return int(self.db._connection().execute(
            "SELECT count(*) FROM sessions WHERE project_id=? AND namespace='agents' "
            "AND kind='' AND trash_id IS NULL", (self.project_id,),
        ).fetchone()[0])

    def owned_rows(self, owner_namespace: str, owner_id: str) -> list[dict[str, Any]]:
        """Project-scoped direct ownership query, independent of fork lineage."""
        return [dict(row) for row in self.db._connection().execute(
            "SELECT * FROM sessions WHERE project_id=? AND owner_namespace=? "
            "AND owner_id=? AND trash_id IS NULL ORDER BY namespace, id",
            (self.project_id, owner_namespace, owner_id),
        )]

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
                    **self._draft_metadata.get(session, {}),
                    "id": session,
                    "namespace": self.namespace,
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
                    "last_user_at": 0.0,
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

    def child_counts(self, namespace: str, identities: list[str]) -> dict[str, int]:
        """Count direct children without crossing project or fork relationships."""
        if not identities:
            return {}
        result: dict[str, int] = {}
        for offset in range(0, len(identities), 500):
            batch = identities[offset:offset + 500]
            rows = self.db._connection().execute(
                "SELECT owner_id,count(*) FROM sessions WHERE project_id=? "
                "AND owner_namespace=? AND trash_id IS NULL AND owner_id IN ("
                + ",".join("?" for _ in batch) + ") GROUP BY owner_id",
                (self.project_id, namespace, *batch),
            ).fetchall()
            result.update({row[0]: row[1] for row in rows})
        return result

    def manager_row(self) -> dict[str, Any] | None:
        """The workspace's singleton manager row (main namespace), live or draft, else ``None``."""
        if self.namespace != "main":
            return None
        with self._draft_lock:
            for session, metadata in self._draft_metadata.items():
                if session in self._drafts and metadata.get("kind") == "manager":
                    return self.session_row(session)
        row = (
            self.db._connection()
            .execute(
                "SELECT * FROM sessions WHERE project_id=? AND namespace='main' "
                "AND kind='manager' AND trash_id IS NULL ORDER BY created_at, id LIMIT 1",
                (self.project_id,),
            )
            .fetchone()
        )
        return dict(row) if row is not None else None

    def list_rows(
        self,
        *,
        include_trashed: bool = False,
        include_archived: bool = True,
        include_manager: bool = False,
    ) -> list[dict[str, Any]]:
        """Session rows with user input. The manager session is excluded unless asked for."""
        clauses = ["project_id=?", "namespace=?", _has_user_input()]
        params: list[Any] = [self.project_id, self.namespace]
        if not include_manager:
            clauses.append("kind<>'manager'")
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
            conn.execute("UPDATE session_names SET retired_at=? WHERE project_id=? AND namespace=? AND logical_id=COALESCE(NULLIF((SELECT logical_session_id FROM sessions WHERE project_id=? AND namespace=? AND id=?),''),?) AND retired_at IS NULL", (time.time(), self.project_id, self.namespace, self.project_id, self.namespace, session, session))
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
                "kind<>'manager' AND trash_id IS NULL AND archived_at IS NULL "
                "AND last_activity <= ? ORDER BY last_activity ASC LIMIT ?",
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
                "UPDATE session_names SET retired_at=? WHERE project_id=? AND namespace=? AND logical_id IN (SELECT COALESCE(NULLIF(logical_session_id,''),id) FROM sessions WHERE project_id=? AND namespace=? AND trash_id IS NOT NULL AND trash_expires_at < ?)",
                (moment, self.project_id, self.namespace, self.project_id, self.namespace, moment),
            )
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
