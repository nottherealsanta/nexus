"""SQLite storage for projects, sessions and messages (`<home>/nexus.db`).

A project is a directory on disk, unique by its resolved absolute path. A
session belongs to one project and runs in a `directory`: the project path
today, a worktree later (a session property, never a tool). A session runs one
agent (`nexus/agents.py`). Its `model` and `effort` are empty unless the user
chose them, in which case they override the agent's preference.

Sessions form a tree. `launched_by` records who started a session: `user` (a
client) or `agent` (another session's agent, through the `session` control
tool). `parent_id` is the session it was started from, and `parent_call_id` is
the tool call in the parent that started it. Deleting a session deletes its
descendants.

`session_snapshot` stores the JSON configuration a session froze at creation:
the agent, rendered prompt, tool descriptions/guidance, child catalog and the
session's `AGENTS.md` record (`nexus/snapshot.py`). `event` is the durable event
log: every published event, in publish order, so the inspector can show any past
session down to the raw wire traffic and a client can reconnect from a cursor.
Session events are deleted with their session; global events (`session_id` null:
projects and `session.deleted` tombstones) are not. `log` holds this database's
log generation and retention floor (`nexus/eventlog.py`). Messages are
append-only rows in session order.

`turn`, `turn_request` and `turn_tool` are the durable execution lifecycle
(`nexus/recovery.py`). A `turn` row exists from the moment a prompt is accepted
until it finishes; `turn_request` records each provider request and the parts
that completed; `turn_tool` records each tool call from saved (never started)
through started and finished (result durable, not yet in context) to inserted
(result in the conversation). Rows cascade with their session. The message row's
`event_id` links a saved message to the `message.added` event that announced it,
so recovery can publish a missing announcement exactly once.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codec import message_from_json, message_to_json, part_from_json
from .llmhub.types import Message, ToolCall, ToolResult
from .paths import home

SCHEMA = """
create table if not exists project (
  id text primary key,
  path text not null unique,
  name text not null,
  created real not null,
  updated real not null
);
create table if not exists session (
  id text primary key,
  project_id text not null references project(id) on delete cascade,
  directory text not null,
  title text not null,
  model text not null,
  permission text not null default 'ask',
  created real not null,
  updated real not null,
  effort text,
  agent text not null default 'build',
  parent_id text references session(id) on delete cascade,
  parent_call_id text,
  launched_by text not null default 'user',
  disabled_tools text not null default '[]',
  archived integer not null default 0
);
create table if not exists event (
  id integer primary key autoincrement,
  session_id text references session(id) on delete cascade,
  seq integer not null,
  time real not null,
  type text not null,
  data text not null
);
create index if not exists event_session on event(session_id, id);
create table if not exists log (
  id integer primary key check (id = 1),
  generation text not null,
  created real not null,
  floor integer not null default 0
);
create table if not exists session_snapshot (
  session_id text primary key references session(id) on delete cascade,
  data text not null,
  created real not null
);
create table if not exists message (
  id text primary key,
  session_id text not null references session(id) on delete cascade,
  seq integer not null,
  role text not null,
  parts text not null,
  created real not null,
  event_id integer,
  unique (session_id, seq)
);
create table if not exists file_observation (
  session_id text not null references session(id) on delete cascade,
  path text not null,
  data text not null,
  updated real not null,
  primary key (session_id, path)
);
create table if not exists turn (
  id text primary key,
  session_id text not null references session(id) on delete cascade,
  status text not null,
  started real not null,
  finished real,
  steps integer not null default 0,
  start_seq integer not null default 0,
  error text,
  recovery text not null default 'none',
  notice_id text,
  data text not null default '{}'
);
create index if not exists turn_session on turn(session_id, started);
create index if not exists turn_open on turn(status, recovery);
create table if not exists turn_request (
  id text primary key,
  turn_id text not null references turn(id) on delete cascade,
  session_id text not null references session(id) on delete cascade,
  step integer not null,
  status text not null,
  started real not null,
  finished real,
  event_after integer not null default 0,
  message_id text,
  stop text,
  response_id text,
  error text,
  parts text not null default '[]',
  data text not null default '{}'
);
create index if not exists turn_request_turn on turn_request(turn_id, step);
create table if not exists turn_tool (
  turn_id text not null references turn(id) on delete cascade,
  session_id text not null references session(id) on delete cascade,
  call_id text not null,
  name text not null,
  message_id text,
  arguments text not null default '',
  state text not null,
  started real,
  finished real,
  output text,
  is_error integer not null default 0,
  observations text not null default '[]',
  primary key (turn_id, call_id)
);
create index if not exists turn_tool_call on turn_tool(session_id, call_id);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000):x}{secrets.token_hex(4)}"


def new_generation() -> str:
    """A fresh identifier for one database's event log."""
    return f"log_{secrets.token_hex(8)}"


def _archive_clause(mode: str) -> str:
    """SQL for a session-list archive filter (`exclude`, `only` or `include`)."""
    try:
        return {"exclude": " and archived = 0", "only": " and archived = 1", "include": ""}[mode]
    except KeyError as exc:
        raise ValueError("archived must be 'exclude', 'only' or 'include'") from exc


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    path: str
    name: str
    created: float
    updated: float


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    project_id: str
    directory: str
    title: str
    model: str  # provider/model chosen by the user; "" = the agent's or the default
    permission: str  # "ask" | "allow"
    created: float
    updated: float
    effort: str | None = None  # chosen by the user; None = the agent's or the provider's default
    agent: str = "build"
    parent_id: str | None = None  # the session this one was launched from
    parent_call_id: str | None = None  # the parent's tool call that launched it
    launched_by: str = "user"  # "user" | "agent"
    disabled_tools: str = "[]"  # JSON list of tool names turned off for this session
    archived: int = 0  # 0/1; archive/unarchive applies to the whole tree

    def disabled(self) -> set[str]:
        return set(json.loads(self.disabled_tools or "[]"))


@dataclass(frozen=True, slots=True)
class StoredMessage:
    id: str
    session_id: str
    seq: int
    message: Message
    created: float


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or home() / "nexus.db"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode = wal")
        self.db.execute("pragma foreign_keys = on")
        self.db.execute("pragma synchronous = normal")  # WAL + normal: safe across crashes, fast per-event commits
        had_turn = self.db.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'turn'").fetchone() is not None
        self.db.executescript(SCHEMA)
        columns = {r["name"] for r in self.db.execute("pragma table_info(session)")}
        if "effort" not in columns:  # databases from before effort existed
            self.db.execute("alter table session add column effort text")
        if "agent" not in columns:  # databases from before agents existed
            self.db.execute("alter table session add column agent text not null default 'build'")
        if "parent_id" not in columns:  # databases from before session trees existed
            self.db.execute("alter table session add column parent_id text references session(id) on delete cascade")
            self.db.execute("alter table session add column parent_call_id text")
            self.db.execute("alter table session add column launched_by text not null default 'user'")
        if "disabled_tools" not in columns:  # databases from before per-session tool switches
            self.db.execute("alter table session add column disabled_tools text not null default '[]'")
        if "archived" not in columns:  # databases from before session archiving: existing sessions stay live
            self.db.execute("alter table session add column archived integer not null default 0")
        message_columns = {r["name"] for r in self.db.execute("pragma table_info(message)")}
        if "event_id" not in message_columns:  # databases from before recovery: old messages have no link
            with self.db:
                self.db.execute("begin immediate")  # the column and legacy marker must land together
                self.db.execute("alter table message add column event_id integer")
                # Mark existing history as pre-link so recovery never republishes it.
                self.db.execute("update message set event_id = -1")
        elif not had_turn:  # an intermediate database with the column: its history also predates linking
            with self.db:
                self.db.execute("update message set event_id = -1 where event_id is null")
        with self.db:
            self.db.execute("create index if not exists message_event on message(event_id)")
        turn_columns = {r["name"] for r in self.db.execute("pragma table_info(turn)")}
        if turn_columns and "start_seq" not in turn_columns:
            with self.db:
                self.db.execute("alter table turn add column start_seq integer not null default 0")
        tool_columns = {r["name"] for r in self.db.execute("pragma table_info(turn_tool)")}
        if "message_id" not in tool_columns:
            self.db.execute("alter table turn_tool add column message_id text")
        self.db.execute("create index if not exists session_parent on session(parent_id)")
        self._migrate_event_log()
        self._generation = self._ensure_log()

    def _migrate_event_log(self) -> None:
        """Make session_id nullable so global lifecycle events and tombstones can be logged.

        The event table is rebuilt in place, preserving every row id (a cursor must
        keep working after an upgrade) and the autoincrement sequence.
        """
        columns = {r["name"]: r for r in self.db.execute("pragma table_info(event)")}
        if not columns["session_id"]["notnull"]:
            return
        sequence = self.db.execute("select seq from sqlite_sequence where name = 'event'").fetchone()
        previous_highest = sequence[0] if sequence else 0
        self.db.execute("pragma foreign_keys = off")
        try:
            with self.db:
                self.db.execute("begin immediate")  # include DDL in the rollback boundary
                self.db.execute("drop index if exists event_session")
                self.db.execute("alter table event rename to event_old")
                self.db.execute(
                    "create table event ("
                    " id integer primary key autoincrement,"
                    " session_id text references session(id) on delete cascade,"
                    " seq integer not null, time real not null, type text not null, data text not null)"
                )
                self.db.execute(
                    "insert into event (id, session_id, seq, time, type, data)"
                    " select id, session_id, seq, time, type, data from event_old"
                )
                self.db.execute("drop table event_old")
                self.db.execute("create index if not exists event_session on event(session_id, id)")
                highest = max(previous_highest, self.db.execute("select coalesce(max(id), 0) from event").fetchone()[0])
                if self.db.execute("select seq from sqlite_sequence where name = 'event'").fetchone() is None:
                    self.db.execute("insert into sqlite_sequence (name, seq) values ('event', ?)", (highest,))
                else:
                    self.db.execute("update sqlite_sequence set seq = ? where name = 'event'", (highest,))
        finally:
            self.db.execute("pragma foreign_keys = on")

    def _ensure_log(self) -> str:
        """This database's log generation; created once and kept across restarts."""
        row = self.db.execute("select generation from log where id = 1").fetchone()
        if row is None:
            with self.db:
                self.db.execute("insert into log (id, generation, created, floor) values (1, ?, ?, 0)",
                                (new_generation(), time.time()))
            row = self.db.execute("select generation from log where id = 1").fetchone()
        return row["generation"]

    def close(self) -> None:
        self.db.close()

    # -- projects ------------------------------------------------------------

    def project_for_path(self, path: str, name: str | None = None) -> tuple[Project, bool]:
        """The project at `path`, created if missing. Returns (project, created)."""
        resolved = str(Path(path).expanduser().resolve())
        if not Path(resolved).is_dir():
            raise ValueError(f"{resolved} is not a directory")
        row = self.db.execute("select * from project where path = ?", (resolved,)).fetchone()
        if row:
            return Project(**row), False
        now = time.time()
        project = Project(new_id("prj"), resolved, name or Path(resolved).name or resolved, now, now)
        with self.db:
            self.db.execute("insert into project values (?, ?, ?, ?, ?)", (project.id, project.path, project.name, now, now))
        return project, True

    def projects(self) -> list[Project]:
        return [Project(**r) for r in self.db.execute("select * from project order by updated desc")]

    def project(self, project_id: str) -> Project | None:
        row = self.db.execute("select * from project where id = ?", (project_id,)).fetchone()
        return Project(**row) if row else None

    def update_project(self, project_id: str, *, name: str) -> Project | None:
        with self.db:
            self.db.execute("update project set name = ?, updated = ? where id = ?", (name, time.time(), project_id))
        return self.project(project_id)

    def delete_project(self, project_id: str) -> bool:
        with self.db:
            return self.db.execute("delete from project where id = ?", (project_id,)).rowcount > 0

    # -- sessions ------------------------------------------------------------

    def create_session(self, project: Project, *, title: str, agent: str, model: str = "", permission: str = "ask",
                       effort: str | None = None, parent: Session | None = None, parent_call_id: str | None = None,
                       launched_by: str = "user", disabled_tools: list[str] | None = None,
                       snapshot: dict[str, Any] | None = None) -> Session:
        now = time.time()
        directory = parent.directory if parent else project.path
        disabled = json.dumps(sorted(set(disabled_tools or [])))
        session = Session(new_id("ses"), project.id, directory, title, model, permission, now, now, effort, agent,
                          parent.id if parent else None, parent_call_id, launched_by, disabled)
        with self.db:
            self.db.execute(
                "insert into session (id, project_id, directory, title, model, permission, created, updated, effort, agent,"
                " parent_id, parent_call_id, launched_by, disabled_tools) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session.id, session.project_id, directory, session.title, model, permission, now, now, effort, agent,
                 session.parent_id, parent_call_id, launched_by, disabled),
            )
            if snapshot is not None:
                self.db.execute("insert into session_snapshot (session_id, data, created) values (?, ?, ?)",
                                (session.id, json.dumps(snapshot), now))
            self.db.execute("update project set updated = ? where id = ?", (now, project.id))
        return session

    def snapshot(self, session_id: str) -> dict[str, Any] | None:
        row = self.db.execute("select data from session_snapshot where session_id = ?", (session_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def set_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "insert into session_snapshot (session_id, data, created) values (?, ?, ?)"
                " on conflict(session_id) do update set data = excluded.data",
                (session_id, json.dumps(snapshot), time.time()),
            )

    def sessions(self, project_id: str | None = None, *, archived: str = "include") -> list[Session]:
        """Sessions of one project or all, filtered by archive state.

        `archived` is `include` (the storage default, all sessions), `exclude` or
        `only`. The HTTP API applies `exclude` by default. Archive state is
        stored on every session of a tree, so filtering rows filters trees.
        """
        clause = _archive_clause(archived)
        if project_id:
            rows = self.db.execute(
                f"select * from session where project_id = ?{clause} order by updated desc", (project_id,))
        else:
            rows = self.db.execute(f"select * from session where 1{clause} order by updated desc")
        return [Session(**r) for r in rows]

    def session(self, session_id: str) -> Session | None:
        row = self.db.execute("select * from session where id = ?", (session_id,)).fetchone()
        return Session(**row) if row else None

    def children(self, session_id: str, *, archived: str = "include") -> list[Session]:
        clause = _archive_clause(archived)
        rows = self.db.execute(
            f"select * from session where parent_id = ?{clause} order by created", (session_id,))
        return [Session(**r) for r in rows]

    def tree_ids(self, session_id: str) -> list[str]:
        """The session and all of its descendants, root first."""
        rows = self.db.execute(
            "with recursive tree(id, depth) as (select id, 0 from session where id = ?"
            " union all select s.id, tree.depth + 1 from session s join tree on s.parent_id = tree.id)"
            " select id from tree order by depth, id",
            (session_id,),
        )
        return [r["id"] for r in rows]

    def tree_members(self, session_id: str) -> list[tuple[str, str | None]]:
        """`(id, parent_id)` for the session and its descendants (for stream filtering)."""
        rows = self.db.execute(
            "with recursive tree(id, parent_id) as (select id, parent_id from session where id = ?"
            " union all select s.id, s.parent_id from session s join tree on s.parent_id = tree.id)"
            " select id, parent_id from tree",
            (session_id,),
        )
        return [(r["id"], r["parent_id"]) for r in rows]

    def set_tree_archived(self, session_id: str, archived: bool) -> tuple[list[str], bool]:
        """Atomically set archive state for `session_id`'s whole tree.

        Returns (tree ids, changed). Repeating the same state changes nothing and
        reports `changed: False`, so callers can avoid duplicate transition events.
        """
        ids = self.tree_ids(session_id)
        if not ids:
            return [], False
        marks = ", ".join("?" * len(ids))
        with self.db:
            rows = self.db.execute(f"select id, archived from session where id in ({marks})", ids).fetchall()
            changed = any(bool(row["archived"]) != archived for row in rows)
            if changed:
                self.db.execute(
                    f"update session set archived = ? where id in ({marks})",
                    (1 if archived else 0, *ids),
                )
        return ids, changed

    def ancestors(self, session_id: str) -> list[str]:
        """Ids from the parent up to the root (empty for a root session)."""
        rows = self.db.execute(
            "with recursive up(id, parent_id, depth) as ("
            " select id, parent_id, 0 from session where id = ?"
            " union all select s.id, s.parent_id, up.depth + 1 from session s join up on s.id = up.parent_id)"
            " select id from up where depth > 0 order by depth",
            (session_id,),
        )
        return [r["id"] for r in rows]

    def descendants(self, session_id: str) -> list[Session]:
        rows = self.db.execute(
            "with recursive down(id) as (select id from session where parent_id = ?"
            " union all select s.id from session s join down on s.parent_id = down.id)"
            " select session.* from session join down using (id) order by created",
            (session_id,),
        )
        return [Session(**r) for r in rows]

    def update_session(self, session_id: str, **fields: Any) -> Session | None:
        allowed = {k: v for k, v in fields.items() if k in ("title", "model", "permission", "effort", "agent")
                   and v is not None}
        if fields.get("directory") is not None:
            allowed["directory"] = str(Path(fields["directory"]).expanduser().resolve())
        if fields.get("effort") == "":
            allowed["effort"] = None  # "" clears it back to the agent's or provider's default
        if fields.get("disabled_tools") is not None:
            allowed["disabled_tools"] = json.dumps(sorted(set(fields["disabled_tools"])))
        if allowed:
            sets = ", ".join(f"{k} = ?" for k in allowed)
            with self.db:
                self.db.execute(f"update session set {sets}, updated = ? where id = ?", (*allowed.values(), time.time(), session_id))
        return self.session(session_id)

    def delete_session(self, session_id: str) -> bool:
        with self.db:
            return self.db.execute("delete from session where id = ?", (session_id,)).rowcount > 0

    # -- file observations ---------------------------------------------------

    def file_observation(self, session_id: str, path: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "select data from file_observation where session_id = ? and path = ?", (session_id, path)
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def set_file_observation(self, session_id: str, path: str, data: dict[str, Any]) -> None:
        now = time.time()
        with self.db:
            self.db.execute(
                "insert into file_observation (session_id, path, data, updated) values (?, ?, ?, ?)"
                " on conflict(session_id, path) do update set data = excluded.data, updated = excluded.updated",
                (session_id, path, json.dumps(data), now),
            )

    def delete_file_observations(self, session_id: str, paths: list[str]) -> None:
        if not paths:
            return
        with self.db:
            self.db.executemany(
                "delete from file_observation where session_id = ? and path = ?",
                [(session_id, path) for path in paths],
            )

    # -- events --------------------------------------------------------------

    def append_event(self, event: dict[str, Any]) -> int | None:
        """Commit one event and return its durable id.

        Returns `None` when the event names a session that no longer exists: its
        history was deleted with the session, so there is nothing to resume from.
        Any other storage failure is raised; the caller must not publish an event
        that was not stored.
        """
        try:
            with self.db:
                row = self.db.execute(
                    "insert into event (session_id, seq, time, type, data) values (?, ?, ?, ?, ?)",
                    (event["session_id"], event["seq"], event["time"], event["type"], json.dumps(event["data"])),
                )
                return row.lastrowid
        except sqlite3.IntegrityError:
            session_id = event["session_id"]
            if session_id is not None and self.db.execute(
                "select 1 from session where id = ?", (session_id,)
            ).fetchone() is None:
                return None
            raise

    def replay(self, after: int, upto: int, limit: int) -> list[dict[str, Any]]:
        """Stored events with `after < id <= upto`, in order, at most `limit`."""
        rows = self.db.execute(
            "select * from event where id > ? and id <= ? order by id limit ?", (after, upto, limit))
        return [self._event_row(r) for r in rows]

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "seq": row["seq"], "time": row["time"], "type": row["type"],
                "session_id": row["session_id"], "data": json.loads(row["data"])}

    def log_generation(self) -> str:
        return self._generation

    def log_head(self) -> int:
        """The highest durable event id, or 0 for an empty log."""
        return self.db.execute("select coalesce(max(id), 0) from event").fetchone()[0]

    def log_floor(self) -> int:
        """The lowest event id still retained. No retention runs yet, so this is 0."""
        row = self.db.execute("select floor from log where id = 1").fetchone()
        return row["floor"] if row else 0

    def events(self, session_id: str, *, after: int = 0, limit: int = 1000, types: list[str] | None = None) -> list[dict[str, Any]]:
        sql = "select * from event where session_id = ? and id > ?"
        params: list[Any] = [session_id, after]
        if types:
            sql += f" and type in ({', '.join('?' * len(types))})"
            params += types
        rows = self.db.execute(sql + " order by id limit ?", (*params, limit))
        return [self._event_row(r) for r in rows]

    def event_counts(self, session_id: str) -> dict[str, int]:
        rows = self.db.execute("select type, count(*) n from event where session_id = ? group by type", (session_id,))
        return {r["type"]: r["n"] for r in rows}

    # -- messages ------------------------------------------------------------

    def _insert_message(self, session_id: str, message: Message, now: float) -> StoredMessage:
        seq = self.db.execute("select coalesce(max(seq), 0) + 1 from message where session_id = ?", (session_id,)).fetchone()[0]
        stored = StoredMessage(new_id("msg"), session_id, seq, message, now)
        self.db.execute(
            "insert into message (id, session_id, seq, role, parts, created, event_id)"
            " values (?, ?, ?, ?, ?, ?, null)",
            (stored.id, session_id, seq, message.role, json.dumps(message_to_json(message)["parts"]), now),
        )
        self.db.execute("update session set updated = ? where id = ?", (now, session_id))
        return stored

    def append_message(self, session_id: str, message: Message) -> StoredMessage:
        now = time.time()
        with self.db:
            return self._insert_message(session_id, message, now)

    def messages(self, session_id: str) -> list[StoredMessage]:
        rows = self.db.execute("select * from message where session_id = ? order by seq", (session_id,))
        return [
            StoredMessage(r["id"], r["session_id"], r["seq"], message_from_json({"role": r["role"], "parts": json.loads(r["parts"])}), r["created"])
            for r in rows
        ]

    def messages_after_seq(self, session_id: str, seq: int) -> list[StoredMessage]:
        """Messages after a turn's starting sequence; recovery uses this, not a clock."""
        rows = self.db.execute(
            "select * from message where session_id = ? and seq > ? order by seq", (session_id, seq))
        return [
            StoredMessage(r["id"], r["session_id"], r["seq"], message_from_json({"role": r["role"], "parts": json.loads(r["parts"])}), r["created"])
            for r in rows
        ]

    def message_event_id(self, message_id: str) -> int | None:
        row = self.db.execute("select event_id from message where id = ?", (message_id,)).fetchone()
        return row["event_id"] if row else None

    def messages_missing_events(self) -> list[StoredMessage]:
        """New messages whose `message.added` event was not linked before a crash.

        Pre-recovery history carries `event_id = -1` and is never selected.
        """
        rows = self.db.execute(
            "select * from message where event_id is null order by created, session_id, seq")
        return [
            StoredMessage(r["id"], r["session_id"], r["seq"],
                          message_from_json({"role": r["role"], "parts": json.loads(r["parts"])}), r["created"])
            for r in rows
        ]

    def link_message_event(self, message_id: str, event_id: int) -> None:
        with self.db:
            self.db.execute("update message set event_id = ? where id = ?", (event_id, message_id))

    def event_for_message(self, session_id: str, message_id: str) -> int | None:
        """The stored `message.added` event for one message, if it was published."""
        row = self.db.execute(
            "select id from event where type = 'message.added' and session_id = ? and json_extract(data, '$.id') = ?"
            " order by id limit 1",
            (session_id, message_id),
        ).fetchone()
        return row["id"] if row else None

    # -- turn execution lifecycle ---------------------------------------------

    def start_turn(self, session_id: str, *, start_seq: int | None = None, started: float | None = None,
                   data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record an accepted turn. The row is durable before the task starts.

        `start_seq` is the last message sequence before the turn; recovery uses it
        instead of a clock. The explicit form also creates a legacy turn for an
        older session whose saved context ends in an unfinished tool call.
        """
        now = time.time()
        if start_seq is None:
            start_seq = self.db.execute("select coalesce(max(seq), 0) from message where session_id = ?",
                                        (session_id,)).fetchone()[0]
        turn = {"id": new_id("trn"), "session_id": session_id, "status": "running",
                "started": started if started is not None else now,
                "finished": None, "steps": 0, "start_seq": start_seq, "error": None, "recovery": "none",
                "notice_id": None, "data": dict(data or {})}
        with self.db:
            self.db.execute(
                "insert into turn (id, session_id, status, started, steps, start_seq, recovery, data)"
                " values (?, ?, 'running', ?, 0, ?, 'none', ?)",
                (turn["id"], session_id, turn["started"], start_seq, json.dumps(turn["data"])),
            )
        return turn

    def legacy_dangling_sessions(self) -> list[tuple[str, StoredMessage]]:
        """Sessions from before execution records whose last saved message ends in tool calls.

        There is no turn row, so this cannot be a turn running now: it is the state
        an older daemon left when it crashed. Recovery labels it unknown rather than
        guessing whether the calls ran.
        """
        rows = self.db.execute(
            "select s.id from session s where not exists (select 1 from turn t where t.session_id = s.id)")
        out: list[tuple[str, StoredMessage]] = []
        for session_id in [row["id"] for row in rows]:
            last = self.db.execute(
                "select * from message where session_id = ? order by seq desc limit 1", (session_id,)).fetchone()
            if last is None or last["role"] != "assistant":
                continue
            try:
                parts = [part_from_json(part) for part in json.loads(last["parts"])]
            except (KeyError, TypeError):
                continue
            if not any(isinstance(part, ToolCall) for part in parts):
                continue
            out.append((session_id, StoredMessage(
                last["id"], session_id, last["seq"],
                Message("assistant", tuple(parts)), last["created"])))
        return out

    @staticmethod
    def _turn_row(row: sqlite3.Row) -> dict[str, Any]:
        data = json.loads(row["data"] or "{}")
        return {
            "id": row["id"], "session_id": row["session_id"], "status": row["status"],
            "started": row["started"], "finished": row["finished"], "steps": row["steps"],
            "start_seq": row["start_seq"],
            "error": json.loads(row["error"]) if row["error"] else None,
            "recovery": row["recovery"], "notice_id": row["notice_id"], "data": data,
        }

    def turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.db.execute("select * from turn where id = ?", (turn_id,)).fetchone()
        return self._turn_row(row) if row else None

    def unfinished_turns(self) -> list[dict[str, Any]]:
        """Turns a restart must reconcile: still running, or recovery did not finish."""
        rows = self.db.execute(
            "select * from turn where status = 'running' or recovery = 'pending' order by started, id")
        return [self._turn_row(row) for row in rows]

    def turns_for_session(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("select * from turn where session_id = ? order by started, id", (session_id,))
        return [self._turn_row(row) for row in rows]

    def finish_turn(self, turn_id: str, status: str, *, steps: int = 0,
                    error: dict[str, Any] | None = None) -> bool:
        """Close a running turn once. Returns whether a row changed."""
        if status not in ("completed", "interrupted", "failed"):
            raise ValueError("status must be 'completed', 'interrupted' or 'failed'")
        with self.db:
            changed = self.db.execute(
                "update turn set status = ?, finished = ?, steps = ?, error = ?, recovery = 'pending'"
                " where id = ? and status = 'running'",
                (status, time.time(), steps, json.dumps(error) if error else None, turn_id),
            ).rowcount > 0
        return changed

    def set_turn_recovery(self, turn_id: str, state: str) -> None:
        if state not in ("none", "pending", "done"):
            raise ValueError("recovery must be 'none', 'pending' or 'done'")
        with self.db:
            self.db.execute("update turn set recovery = ? where id = ?", (state, turn_id))

    def set_recovery_notice(self, turn_id: str, message: Message) -> StoredMessage | None:
        """Insert a turn's recovery notice once, atomically with its marker."""
        now = time.time()
        with self.db:
            row = self.db.execute("select session_id, notice_id from turn where id = ?", (turn_id,)).fetchone()
            if row is None or row["notice_id"] is not None:
                return None
            stored = self._insert_message(row["session_id"], message, now)
            self.db.execute("update turn set notice_id = ? where id = ?", (stored.id, turn_id))
        return stored

    def start_request(self, turn_id: str, session_id: str, step: int) -> dict[str, Any]:
        """Record one provider request before it is sent, with the log watermark."""
        now = time.time()
        request = {"id": new_id("req"), "turn_id": turn_id, "session_id": session_id, "step": step,
                   "status": "started", "started": now, "finished": None,
                   "event_after": self.log_head(), "message_id": None, "stop": None,
                   "response_id": None, "error": None, "parts": [], "data": {}}
        with self.db:
            self.db.execute(
                "insert into turn_request (id, turn_id, session_id, step, status, started, event_after)"
                " values (?, ?, ?, ?, 'started', ?, ?)",
                (request["id"], turn_id, session_id, step, now, request["event_after"]),
            )
        return request

    @staticmethod
    def _request_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "turn_id": row["turn_id"], "session_id": row["session_id"],
            "step": row["step"], "status": row["status"], "started": row["started"],
            "finished": row["finished"], "event_after": row["event_after"],
            "message_id": row["message_id"], "stop": row["stop"], "response_id": row["response_id"],
            "error": json.loads(row["error"]) if row["error"] else None,
            "parts": json.loads(row["parts"] or "[]"), "data": json.loads(row["data"] or "{}"),
        }

    def request(self, request_id: str) -> dict[str, Any] | None:
        row = self.db.execute("select * from turn_request where id = ?", (request_id,)).fetchone()
        return self._request_row(row) if row else None

    def requests_for_turn(self, turn_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("select * from turn_request where turn_id = ? order by step, started", (turn_id,))
        return [self._request_row(row) for row in rows]

    def finish_request(self, request_id: str, *, stop: str | None, response_id: str | None,
                       parts: list[dict[str, Any]]) -> None:
        with self.db:
            self.db.execute(
                "update turn_request set status = 'completed', finished = ?, stop = ?, response_id = ?, parts = ?"
                " where id = ? and status = 'started'",
                (time.time(), stop, response_id, json.dumps(parts), request_id),
            )

    def interrupt_request(self, request_id: str, error: dict[str, Any] | None = None,
                          reason: str | None = None) -> None:
        """Close an unfinished provider request; its streamed output stays in the event log."""
        with self.db:
            self.db.execute(
                "update turn_request set status = 'interrupted', finished = ?, error = ?, data = ?"
                " where id = ? and status = 'started'",
                (time.time(), json.dumps(error) if error else None,
                 json.dumps({"reason": reason} if reason else {}), request_id),
            )

    def fail_request(self, request_id: str, error: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "update turn_request set status = 'failed', finished = ?, error = ?"
                " where id = ? and status = 'started'",
                (time.time(), json.dumps(error), request_id),
            )

    @staticmethod
    def _tool_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "turn_id": row["turn_id"], "session_id": row["session_id"], "call_id": row["call_id"],
            "name": row["name"], "arguments": row["arguments"], "state": row["state"],
            "started": row["started"], "finished": row["finished"], "output": row["output"],
            "is_error": bool(row["is_error"]), "observations": json.loads(row["observations"] or "[]"),
        }

    def tools_for_turn(self, turn_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("select * from turn_tool where turn_id = ? order by started, call_id", (turn_id,))
        return [self._tool_row(row) for row in rows]

    def latest_tool_rows(self, session_id: str, call_ids: list[str], *, message_id: str) -> dict[str, dict[str, Any]]:
        """Execution evidence for calls in a particular saved assistant message.

        A saved assistant message and its call rows are one transaction, so this
        is the evidence a continuation uses to close a dangling call: `started`
        means unknown, `finished` preserves a completed result, and `saved` means
        it never ran. Rows are ordered by their turn's start, newest first.
        """
        if not call_ids:
            return {}
        marks = ", ".join("?" * len(call_ids))
        rows = self.db.execute(
            "select tt.* from turn_tool tt join turn t on t.id = tt.turn_id"
            f" where tt.session_id = ? and tt.call_id in ({marks})"
            " and (tt.message_id = ? or (tt.message_id is null and exists"
            " (select 1 from turn_request r where r.turn_id = tt.turn_id and r.message_id = ?)))"
            " order by t.started desc, t.id desc",
            (session_id, *call_ids, message_id, message_id),
        )
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            out.setdefault(row["call_id"], self._tool_row(row))
        return out

    def mark_tool_started(self, turn_id: str, session_id: str, call: ToolCall) -> None:
        """Record that a call is about to run, before the tool is invoked."""
        with self.db:
            self.db.execute(
                "insert into turn_tool (turn_id, session_id, call_id, name, arguments, state, started)"
                " values (?, ?, ?, ?, ?, 'started', ?)"
                " on conflict(turn_id, call_id) do update set"
                " name = excluded.name, arguments = excluded.arguments,"
                " state = case when turn_tool.state = 'saved' then 'started' else turn_tool.state end,"
                " started = coalesce(turn_tool.started, excluded.started)",
                (turn_id, session_id, call.id, call.name, call.arguments, time.time()),
            )

    def mark_tool_finished(self, turn_id: str, session_id: str, call: ToolCall, result: ToolResult,
                           observations: list[dict[str, Any]] | None = None) -> None:
        """Keep a completed result durable before it enters the conversation."""
        now = time.time()
        with self.db:
            changed = self.db.execute(
                "update turn_tool set state = 'finished', finished = ?, output = ?, is_error = ?, observations = ?"
                " where turn_id = ? and call_id = ? and state in ('started', 'saved', 'finished')",
                (now, result.output, 1 if result.is_error else 0, json.dumps(observations or []),
                 turn_id, result.call_id),
            ).rowcount
            if not changed:
                self.db.execute(
                    "insert into turn_tool (turn_id, session_id, call_id, name, arguments, state, started,"
                    " finished, output, is_error, observations)"
                    " values (?, ?, ?, ?, ?, 'finished', ?, ?, ?, ?, ?)",
                    (turn_id, session_id, result.call_id, call.name, call.arguments, now, now,
                     result.output, 1 if result.is_error else 0, json.dumps(observations or [])),
                )

    def save_assistant_message(self, session_id: str, message: Message, *, turn_id: str,
                               request_id: str | None = None) -> StoredMessage:
        """Save one assistant message, its request link and its calls as one transaction."""
        now = time.time()
        with self.db:
            stored = self._insert_message(session_id, message, now)
            if request_id is not None:
                self.db.execute(
                    "update turn_request set message_id = ?, parts = '[]' where id = ? and message_id is null",
                    (stored.id, request_id),
                )
            for part in message.parts:
                if isinstance(part, ToolCall):
                    self.db.execute(
                        "insert into turn_tool (turn_id, session_id, call_id, name, arguments, message_id, state)"
                        " values (?, ?, ?, ?, ?, ?, 'saved')"
                        " on conflict(turn_id, call_id) do update set name = excluded.name,"
                        " arguments = excluded.arguments, message_id = excluded.message_id, state = 'saved',"
                        " started = null, finished = null, output = null, is_error = 0, observations = '[]'",
                        (turn_id, session_id, part.id, part.name, part.arguments, stored.id),
                    )
        return stored

    def _apply_tool_results(self, session_id: str, message: Message, *, turn_id: str | None,
                            now: float) -> None:
        """Mark one message's results inserted, or record a result with no known turn."""
        if turn_id is None:
            return
        for part in message.parts:
            if not isinstance(part, ToolResult):
                continue
            changed = self.db.execute(
                "update turn_tool set state = 'inserted', output = ?, is_error = ?, finished = coalesce(finished, ?)"
                " where turn_id = ? and call_id = ?",
                (part.output, 1 if part.is_error else 0, now, turn_id, part.call_id),
            ).rowcount
            if not changed:  # a result whose call predates the lifecycle record still closes visibly
                self.db.execute(
                    "insert into turn_tool (turn_id, session_id, call_id, name, arguments, state,"
                    " finished, output, is_error) values (?, ?, ?, '', '', 'inserted', ?, ?, ?)",
                    (turn_id, session_id, part.call_id, now, part.output, 1 if part.is_error else 0),
                )

    def save_tool_message(self, session_id: str, message: Message, *, turn_id: str | None) -> StoredMessage:
        """Save a tool message and mark its results inserted, as one transaction."""
        now = time.time()
        with self.db:
            stored = self._insert_message(session_id, message, now)
            self._apply_tool_results(session_id, message, turn_id=turn_id, now=now)
        return stored

    def save_tool_message_with_notice(self, session_id: str, message: Message, notice: Message, *,
                                      turn_id: str | None) -> tuple[StoredMessage, StoredMessage]:
        """Insert one tool message and one follow-up context message atomically.

        Continuation reconciliation uses this so a crash cannot leave closed tool
        calls without their labelled explanation, or a notice that a later
        continuation would append again.
        """
        now = time.time()
        with self.db:
            stored = self._insert_message(session_id, message, now)
            self._apply_tool_results(session_id, message, turn_id=turn_id, now=now)
            explained = self._insert_message(session_id, notice, now)
        return stored, explained

    def has_event(self, type_: str, fields: dict[str, Any], *, session_id: str | None = None) -> bool:
        """Whether a stored event of `type_` carries every field value (exact JSON match)."""
        conditions = ["type = ?"]
        params: list[Any] = [type_]
        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)
        for key, value in fields.items():
            conditions.append("json_extract(data, ?) = ?")
            params += [f"$.{key}", value]
        row = self.db.execute(
            "select 1 from event where " + " and ".join(conditions) + " limit 1", params).fetchone()
        return row is not None

    def has_event_type(self, session_id: str, types: list[str]) -> bool:
        row = self.db.execute(
            "select 1 from event where session_id = ? and type in (" + ", ".join("?" * len(types)) + ") limit 1",
            (session_id, *types),
        ).fetchone()
        return row is not None

    def unfinished_bash_jobs(self, session_id: str) -> list[str]:
        """Job ids whose durable `bash.started` has no matching `bash.finished`."""
        rows = self.db.execute(
            "select type, json_extract(data, '$.id') as job from event"
            " where session_id = ? and type in ('bash.started', 'bash.finished') order by id",
            (session_id,),
        )
        started: list[str] = []
        finished: set[str] = set()
        for row in rows:
            job = row["job"]
            if not job:
                continue
            if row["type"] == "bash.started":
                if job not in started:
                    started.append(job)
            else:
                finished.add(job)
        return [job for job in started if job not in finished]

    def wait_lifecycle(self, session_id: str, *, since: float | None = None) -> dict[str, Any]:
        """Durable asked/replied records for approvals and questions in one session."""
        rows = self.db.execute(
            "select type, data from event where session_id = ? and type in"
            " ('question.asked', 'question.replied', 'permission.asked', 'permission.replied')"
            " and (? is null or time >= ?) order by id",
            (session_id, since, since),
        )
        out: dict[str, Any] = {
            "questions_asked": [], "questions_replied": [], "approvals_asked": [], "approvals_replied": [],
        }
        for row in rows:
            data = json.loads(row["data"])
            key = {"question.asked": "questions_asked", "question.replied": "questions_replied",
                   "permission.asked": "approvals_asked", "permission.replied": "approvals_replied"}[row["type"]]
            out[key].append(data)
        return out

    def stream_summary(self, session_id: str, after: int) -> dict[str, Any]:
        """What one interrupted provider request left in the durable event log.

        Counts only: recovery must not reconstruct a response from deltas. They stay
        diagnostic history, not replayable model context.
        """
        rows = self.db.execute(
            "select type, count(*) n,"
            " sum(case when type in ('text_delta', 'reasoning_delta')"
            "     then coalesce(length(json_extract(data, '$.text')), 0) else 0 end) chars,"
            " sum(case when type = 'tool_args_delta'"
            "     then coalesce(length(json_extract(data, '$.delta')), 0) else 0 end) arg_chars"
            " from event where session_id = ? and id > ? group by type",
            (session_id, after),
        )
        counts = {row["type"]: {"count": row["n"], "chars": row["chars"], "arg_chars": row["arg_chars"]} for row in rows}
        parts = {
            row["part_type"]: row["n"] for row in self.db.execute(
                "select json_extract(data, '$.part.type') part_type, count(*) n from event"
                " where session_id = ? and id > ? and type = 'part_done' group by part_type",
                (session_id, after),
            ) if row["part_type"]
        }
        calls = []
        for row in self.db.execute(
            "select data from event where session_id = ? and id > ? and type = 'tool_call_started'"
            " order by id limit 50",
            (session_id, after),
        ):
            data = json.loads(row["data"])
            calls.append({"id": data.get("id"), "name": data.get("name")})
        return {"counts": counts, "parts": parts, "tool_calls": calls}
