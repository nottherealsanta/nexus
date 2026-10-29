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

Each `turn_request` also records the `provider/model` ref and effort it ran
with, its token usage (the sum over its `turn_attempt` rows, each of which keeps
its own) and its reasoning time; `turn.ended` records the loop's `TurnEnd`.
`session_usage` is the per-session, per-model spend ledger: it references only
the session, so it outlives pruned turns. `turn_summaries` serves all of this as
`GET /api/session/{id}/turn` (TURN_SUMMARY_PLAN.md). Unknown usage is `null`,
never zero.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codec import message_from_json, message_to_json, part_from_json
from .llmhub.types import Image, Message, Text, ToolCall, ToolResult, Usage
from .paths import home

SCHEMA = """
create table if not exists project (
  id text primary key,
  path text not null unique,
  name text not null,
  created real not null,
  updated real not null,
  revision integer not null default 1
);
create table if not exists session (
  id text primary key,
  project_id text not null references project(id) on delete cascade,
  directory text not null,
  title text not null,
  model text not null,
  permission text not null default 'allow',
  created real not null,
  updated real not null,
  effort text,
  agent text not null default 'build',
  parent_id text references session(id) on delete cascade,
  parent_call_id text,
  launched_by text not null default 'user',
  disabled_tools text not null default '[]',
  archived integer not null default 0,
  revision integer not null default 1
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
  floor integer not null default 0,
  head_high integer not null default 0
);
create table if not exists preference (
  name text primary key,
  value text
);
create table if not exists mutation (
  key text primary key,
  fingerprint text not null,
  operation text not null,
  target text not null,
  accepted real not null,
  state text not null,
  status integer,
  body text,
  turn_id text,
  resource text,
  resolved real,
  expires real
);
create index if not exists mutation_turn on mutation(turn_id);
create index if not exists mutation_expiry on mutation(expires);
create table if not exists turn_attempt (
  id text primary key,
  turn_id text not null references turn(id) on delete cascade,
  request_id text references turn_request(id) on delete cascade,
  session_id text not null references session(id) on delete cascade,
  attempt integer not null,
  status text not null,
  delay real,
  started real,
  finished real,
  error text,
  input_tokens integer,
  cached_input_tokens integer,
  output_tokens integer,
  reasoning_tokens integer
);
create index if not exists turn_attempt_request on turn_attempt(request_id, attempt);
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
create table if not exists prompt_history (
  id integer primary key autoincrement,
  session_id text not null references session(id) on delete cascade,
  text text not null,
  image_count integer not null default 0,
  created real not null
);
create index if not exists prompt_history_session on prompt_history(session_id, id);
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
  data text not null default '{}',
  ended text
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
  data text not null default '{}',
  model text,
  effort text,
  input_tokens integer,
  cached_input_tokens integer,
  output_tokens integer,
  reasoning_tokens integer,
  reasoning_seconds real
);
create index if not exists turn_request_turn on turn_request(turn_id, step);
create table if not exists session_usage (
  session_id text not null references session(id) on delete cascade,
  model text not null,
  requests integer not null default 0,
  input_tokens integer not null default 0,
  cached_input_tokens integer not null default 0,
  output_tokens integer not null default 0,
  reasoning_tokens integer not null default 0,
  since real not null,
  primary key (session_id, model)
);
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


USAGE_COLUMNS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")
USAGE_KEYS = ("input", "cached", "output", "reasoning")  # their names in the API
UNKNOWN_MODEL = "unknown"  # ledger key for a request whose model was never recorded
TURN_PAGE = 50
TURN_PAGE_MAX = 200
MUTATION_BODY_MAX = 64 * 1024  # retained response bytes; larger results replay as a bounded accepted record


def _archive_clause(mode: str) -> str:
    """SQL for a session-list archive filter (`exclude`, `only` or `include`)."""
    try:
        return {"exclude": " and archived = 0", "only": " and archived = 1", "include": ""}[mode]
    except KeyError as exc:
        raise ValueError("archived must be 'exclude', 'only' or 'include'") from exc


def _usage_values(row: sqlite3.Row) -> tuple[int, ...] | None:
    """A row's four usage columns, or `None` when it never reported usage."""
    if row["input_tokens"] is None:
        return None
    return tuple(row[c] or 0 for c in USAGE_COLUMNS)


def _usage_json(row: sqlite3.Row) -> dict[str, int] | None:
    """API usage (`input`, `cached`, `output`, `reasoning`); `None` is unknown, never zeros."""
    values = _usage_values(row)
    return dict(zip(USAGE_KEYS, values, strict=True)) if values is not None else None


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    path: str
    name: str
    created: float
    updated: float
    revision: int = 1


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
    revision: int = 1  # monotonic configuration revision (expected-revision preconditions)

    def disabled(self) -> set[str]:
        return set(json.loads(self.disabled_tools or "[]"))


@dataclass(frozen=True, slots=True)
class StoredMessage:
    id: str
    session_id: str
    seq: int
    message: Message
    created: float
    delivery: str | None = None


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or home() / "nexus.db"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode = wal")
        self.db.execute("pragma foreign_keys = on")
        self.db.execute("pragma synchronous = normal")  # WAL + normal: safe across crashes, fast per-event commits
        self._pins: dict[int, tuple[int, int]] = {}
        self._pin_seq = 0
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
        if "revision" not in columns:  # databases from before expected-revision preconditions
            self.db.execute("alter table session add column revision integer not null default 1")
        # Older sessions defaulted to asking for tool approval. Apply the new
        # default once, including descendants; later explicit choices survive.
        with self.db:
            self.db.execute("create table if not exists migration (name text primary key)")
            applied = self.db.execute(
                "insert or ignore into migration (name) values (?)", ("default_tool_allow_v1",))
            if applied.rowcount:
                self.db.execute("update session set permission = 'allow', revision = revision + 1 "
                                "where permission = 'ask'")
        with self.db:  # one-time: sessions from before prompt history keep what the user typed
            if self.db.execute("insert or ignore into migration (name) values ('prompt_history_v1')").rowcount:
                self._backfill_prompt_history()
        project_columns = {r["name"] for r in self.db.execute("pragma table_info(project)")}
        if "revision" not in project_columns:
            self.db.execute("alter table project add column revision integer not null default 1")
        message_columns = {r["name"] for r in self.db.execute("pragma table_info(message)")}
        if "delivery" not in message_columns:
            self.db.execute("alter table message add column delivery text")
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
        self._migrate_usage()
        self._generation = self._ensure_log()

    def _migrate_usage(self) -> None:
        """Add model, effort, usage and reasoning time to requests, and `turn.ended`.

        Runs its backfill only in the transaction that adds the columns, so it
        runs once (TURN_SUMMARY_PLAN.md 4.9).
        """
        request_columns = {r["name"] for r in self.db.execute("pragma table_info(turn_request)")}
        attempt_columns = {r["name"] for r in self.db.execute("pragma table_info(turn_attempt)")}
        turn_columns = {r["name"] for r in self.db.execute("pragma table_info(turn)")}
        wanted_request = ("model text", "effort text", *(f"{c} integer" for c in USAGE_COLUMNS),
                          "reasoning_seconds real")
        missing_request = [c for c in wanted_request if c.split()[0] not in request_columns]
        missing_attempt = [f"{c} integer" for c in USAGE_COLUMNS if c not in attempt_columns]
        if not missing_request and not missing_attempt and "ended" in turn_columns:
            return
        with self.db:
            self.db.execute("begin immediate")  # the columns and their backfill land together
            for column in missing_request:
                self.db.execute(f"alter table turn_request add column {column}")
            for column in missing_attempt:
                self.db.execute(f"alter table turn_attempt add column {column}")
            if "ended" not in turn_columns:
                self.db.execute("alter table turn add column ended text")
            if "input_tokens" not in request_columns:
                self._backfill_usage()

    def _backfill_usage(self) -> None:
        """Fill old requests' usage, model and effort from the events that remain.

        A request's events are those after its `event_after` up to the next
        request's `event_after` in the same session. Exactly one `usage` event
        there fills the request and its last attempt; zero or several leave them
        unknown. `model` is the bare id from the first `wire_request` body (the
        provider is not in the body). The ledger is seeded from what was filled.
        """
        rows = self.db.execute(
            "select id, session_id, event_after from turn_request order by session_id, event_after, started, id"
        ).fetchall()
        by_session: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_session.setdefault(row["session_id"], []).append(row)
        for session_id, requests in by_session.items():
            events = self.db.execute(
                "select id, type, data from event where session_id = ? and id > ?"
                " and type in ('usage', 'wire_request') order by id",
                (session_id, requests[0]["event_after"])).fetchall()
            for index, request in enumerate(requests):
                low = request["event_after"]
                high = requests[index + 1]["event_after"] if index + 1 < len(requests) else None
                inside = [e for e in events if e["id"] > low and (high is None or e["id"] <= high)]
                usages = [json.loads(e["data"]) for e in inside if e["type"] == "usage"]
                wires = [json.loads(e["data"]) for e in inside if e["type"] == "wire_request"]
                body = (wires[0].get("body") if wires else None) or {}
                model = body.get("model") if isinstance(body.get("model"), str) else None
                reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), dict) else {}
                effort = body.get("reasoning_effort") or reasoning.get("effort")
                self.db.execute(
                    "update turn_request set model = coalesce(model, ?), effort = coalesce(effort, ?) where id = ?",
                    (model, effort if isinstance(effort, str) else None, request["id"]))
                if len(usages) != 1:
                    continue
                values = tuple(int(usages[0].get(c) or 0) for c in USAGE_COLUMNS)
                sets = ", ".join(f"{c} = ?" for c in USAGE_COLUMNS)
                self.db.execute(f"update turn_request set {sets} where id = ?", (*values, request["id"]))
                self.db.execute(
                    f"update turn_attempt set {sets} where id = (select id from turn_attempt where request_id = ?"
                    " order by attempt desc, started desc limit 1)", (*values, request["id"]))
        self.db.execute(
            "insert into session_usage (session_id, model, requests, input_tokens, cached_input_tokens,"
            " output_tokens, reasoning_tokens, since)"
            " select session_id, coalesce(model, ?), count(*), sum(input_tokens), sum(cached_input_tokens),"
            " sum(output_tokens), sum(reasoning_tokens), min(started) from turn_request"
            " where input_tokens is not null group by session_id, coalesce(model, ?)"
            " on conflict(session_id, model) do nothing",
            (UNKNOWN_MODEL, UNKNOWN_MODEL))

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
        columns = {r["name"] for r in self.db.execute("pragma table_info(log)")}
        if columns and "head_high" not in columns:
            self.db.execute("alter table log add column head_high integer not null default 0")
        if columns:
            with self.db:
                self.db.execute(
                    "update log set head_high = max(head_high, (select coalesce(max(id), 0) from event))"
                    " where id = 1")
        row = self.db.execute("select generation from log where id = 1").fetchone()
        if row is None:
            highest = self.db.execute("select coalesce(max(id), 0) from event").fetchone()[0]
            with self.db:
                self.db.execute(
                    "insert into log (id, generation, created, floor, head_high) values (1, ?, ?, 0, ?)",
                    (new_generation(), time.time(), highest))
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
            self.db.execute("insert into project (id, path, name, created, updated, revision) values (?, ?, ?, ?, ?, 1)",
                            (project.id, project.path, project.name, now, now))
        return project, True

    def projects(self) -> list[Project]:
        return [Project(**r) for r in self.db.execute("select * from project order by updated desc")]

    def project(self, project_id: str) -> Project | None:
        row = self.db.execute("select * from project where id = ?", (project_id,)).fetchone()
        return Project(**row) if row else None

    def update_project(self, project_id: str, *, name: str, expected_revision: int | None = None) -> Project | None:
        with self.db:
            if expected_revision is not None:
                row = self.db.execute(
                    "update project set name = ?, updated = ?, revision = revision + 1"
                    " where id = ? and revision = ?", (name, time.time(), project_id, expected_revision))
            else:
                row = self.db.execute(
                    "update project set name = ?, updated = ?, revision = revision + 1 where id = ?",
                    (name, time.time(), project_id))
            if row.rowcount == 0:
                return None
        return self.project(project_id)

    def delete_project(self, project_id: str) -> bool:
        with self.db:
            return self.db.execute("delete from project where id = ?", (project_id,)).rowcount > 0

    # -- sessions ------------------------------------------------------------

    def create_session(self, project: Project, *, title: str, agent: str, model: str = "", permission: str = "allow",
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
                    f"update session set archived = ?, revision = revision + 1 where id in ({marks})",
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
        expected_revision = fields.pop("expected_revision", None)
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
                if expected_revision is not None:
                    changed = self.db.execute(
                        f"update session set {sets}, updated = ?, revision = revision + 1"
                        " where id = ? and revision = ?",
                        (*allowed.values(), time.time(), session_id, expected_revision)).rowcount
                else:
                    changed = self.db.execute(
                        f"update session set {sets}, updated = ?, revision = revision + 1 where id = ?",
                        (*allowed.values(), time.time(), session_id)).rowcount
                if not changed:
                    return None
        return self.session(session_id)

    def delete_session(self, session_id: str) -> bool:
        with self.db:
            return self.db.execute("delete from session where id = ?", (session_id,)).rowcount > 0

    # -- preferences ---------------------------------------------------------

    def preference(self, name: str) -> str | None:
        """A machine-wide value, or `None` when it was never set."""
        row = self.db.execute("select value from preference where name = ?", (name,)).fetchone()
        return row["value"] if row else None

    def set_preference(self, name: str, value: str | None) -> None:
        """Set a value, or remove it when `value` is `None`."""
        with self.db:
            if value is None:
                self.db.execute("delete from preference where name = ?", (name,))
            else:
                self.db.execute(
                    "insert into preference (name, value) values (?, ?)"
                    " on conflict(name) do update set value = excluded.value",
                    (name, value),
                )

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
                event_id = row.lastrowid
                self.db.execute("update log set head_high = max(head_high, ?) where id = 1", (event_id,))
                return event_id
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
        """The highest durable event id ever committed, even after retention removed it."""
        row = self.db.execute("select max(head_high, (select coalesce(max(id), 0) from event)) from log where id = 1").fetchone()
        return row[0] if row and row[0] is not None else 0

    def log_floor(self) -> int:
        """The last event id removed by retention; `0` means nothing was removed."""
        row = self.db.execute("select floor from log where id = 1").fetchone()
        return row["floor"] if row else 0

    def protected_sessions(self) -> set[str]:
        """Sessions whose events still carry recovery or lifecycle evidence.

        An unfinished turn, an unfinished provider request, a live background job
        started event with no finish, or an open wait all pin that session's log
        out of retention. Idle alone is not proof that its events are disposable.
        """
        out: set[str] = {row["session_id"] for row in self.db.execute(
            "select distinct session_id from turn_tool where state != 'inserted'")}
        for row in self.db.execute(
                "select distinct session_id from turn where status = 'running' or recovery = 'pending'"):
            out.add(row["session_id"])
        for row in self.db.execute(
                "select distinct session_id from turn_request where status = 'started'"):
            out.add(row["session_id"])
        for row in self.db.execute(
                "select distinct session_id from event where session_id is not null and type in"
                " ('bash.started', 'permission.asked', 'question.asked')"):
            session_id = row["session_id"]
            if self._has_open_lifecycle(session_id):
                out.add(session_id)
        return out

    def _has_open_lifecycle(self, session_id: str) -> bool:
        started: set[str] = set()
        finished: set[str] = set()
        for row in self.db.execute(
                "select type, json_extract(data, '$.id') as ident from event where session_id = ?"
                " and type in ('bash.started', 'bash.finished') order by id", (session_id,)):
            if not row["ident"]:
                continue
            (started.add if row["type"] == "bash.started" else finished.add)(row["ident"])
        if started - finished:
            return True
        asked = {row["id"] for row in self.db.execute(
            "select json_extract(data, '$.id') as id from event where session_id = ? and type in"
            " ('permission.asked', 'question.asked')", (session_id,)) if row["id"]}
        replied = {row["id"] for row in self.db.execute(
            "select json_extract(data, '$.id') as id from event where session_id = ? and type in"
            " ('permission.replied', 'question.replied')", (session_id,)) if row["id"]}
        return bool(asked - replied)

    def prune_turns(self, *, now: float, age_seconds: float, keep_per_session: int = 200) -> dict[str, int]:
        """Remove old terminal execution rows, keeping recovery evidence and recent turns."""
        cutoff = now - age_seconds
        removed = 0
        with self.db:
            turns = self.db.execute(
                "select id, session_id from turn where status != 'running' and recovery != 'pending'"
                " and coalesce(finished, started) < ? order by started", (cutoff,)).fetchall()
            keep: dict[str, int] = {}
            for row in self.db.execute(
                    "select id, session_id from turn where status != 'running' and recovery != 'pending'"
                    " order by started desc"):
                keep[row["session_id"]] = keep.get(row["session_id"], 0) + 1
            protected = self.protected_sessions()
            for row in turns:
                if row["session_id"] in protected:
                    continue
                if self.db.execute(
                        "select 1 from mutation where turn_id = ? and (expires is null or expires >= ?)",
                        (row["id"], now)).fetchone():
                    continue
                if keep.get(row["session_id"], 0) <= keep_per_session:
                    continue
                removed += self.db.execute("delete from turn where id = ?", (row["id"],)).rowcount
                keep[row["session_id"]] -= 1
        return {"turns_removed": removed}

    def pin_events(self, start: int, end: int) -> int:
        """Keep events `start..end` alive for an in-flight replay or export.

        Returns a pin token for `unpin_events`. Pins are in-process; a crash
        discards them together with the replay that needed them.
        """
        token = self._pin_seq = getattr(self, "_pin_seq", 0) + 1
        self._pins[token] = (start, end)
        return token

    def unpin_events(self, token: int) -> None:
        self._pins.pop(token, None)

    def _pinned_cutoff(self) -> int | None:
        """The largest id retention may remove, or `None` when a pin blocks it."""
        if not self._pins:
            return None
        return min(start for start, _ in self._pins.values()) - 1

    def prune_events(self, *, now: float, age_seconds: float | None, byte_budget: int | None,
                     protect_sessions: set[str] | None = None, batch_size: int = 5000) -> dict[str, Any]:
        """Remove one contiguous prefix of old events and advance the floor.

        Protection is conservative: events of a session with an unfinished turn
        (or with a listed live job/wait) are never removed, and an in-flight
        replay pins its range. Returns counts and the new floor; never removes
        the durable head or reuses an id.
        """
        floor = self.log_floor()
        head = self.log_head()
        protecting = set(protect_sessions or ())
        protected_cutoff: int | None = None
        if protecting:
            marks = ", ".join("?" * len(protecting))
            row = self.db.execute(
                f"select min(id) from event where session_id in ({marks})", tuple(protecting)).fetchone()
            if row and row[0] is not None:
                protected_cutoff = row[0] - 1
        pinned = self._pinned_cutoff()
        cutoff: int | None = None
        if age_seconds is not None:
            row = self.db.execute(
                "select max(id) from event where id > ? and time < ?",
                (floor, now - age_seconds)).fetchone()
            if row and row[0] is not None:
                cutoff = row[0]
        if byte_budget is not None:
            # Keep the newest suffix that fits. Size and age are independent
            # expiry conditions: either can require removing an older prefix.
            used = 0
            for row in self.db.execute(
                    "select id, length(cast(data as blob)) + length(cast(type as blob))"
                    " from event where id > ? order by id desc", (floor,)):
                used += row[1]
                if used > byte_budget:
                    cutoff = max(cutoff or floor, row[0])
                    break
        if cutoff is None:
            return self._retention_pressure(floor, head, protecting, byte_budget)
        cutoff = min(cutoff, head)
        for limit in (protected_cutoff, pinned):
            if limit is not None:
                cutoff = min(cutoff, limit)
        if cutoff <= floor:
            return self._retention_pressure(floor, head, protecting, byte_budget)
        # One bounded batch per maintenance pass; do not hold the event loop
        # through an unbounded sequence of DELETE statements in one transaction.
        rows = self.db.execute(
            "select id from event where id > ? and id <= ? order by id limit ?",
            (floor, cutoff, max(1, batch_size))).fetchall()
        if rows:
            cutoff = rows[-1]["id"]
        with self.db:
            removed = self.db.execute("delete from event where id > ? and id <= ?", (floor, cutoff)).rowcount
            self.db.execute("update log set floor = ? where id = 1", (cutoff,))
        retained_bytes = self.db.execute(
            "select coalesce(sum(length(cast(data as blob)) + length(cast(type as blob))), 0) from event").fetchone()[0]
        return {"removed": removed, "floor": cutoff, "head": head, "protected": sorted(protecting),
                "pinned": bool(self._pins), "batches": 1,
                "retained_bytes": retained_bytes,
                "over_budget": (retained_bytes > byte_budget) if byte_budget is not None else False}

    def _retention_pressure(self, floor: int, head: int, protecting: set[str], budget: int | None) -> dict[str, Any]:
        used = self.db.execute(
            "select coalesce(sum(length(cast(data as blob)) + length(cast(type as blob))), 0) from event").fetchone()[0]
        return {"removed": 0, "floor": floor, "head": head, "protected": sorted(protecting),
                "pinned": bool(self._pins), "batches": 0, "retained_bytes": used,
                "over_budget": budget is not None and used > budget}

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

    NOTICE_PREFIXES = ("[Nexus notice", "[Notification", "→ Background Shell")

    def _backfill_prompt_history(self) -> None:
        """Old user messages that are not notices; child sessions are prompted by their parent."""
        rows = self.db.execute(
            "select m.session_id, m.parts, m.created from message m join session s on s.id = m.session_id"
            " where m.role = 'user' and s.parent_id is null order by m.created, m.seq").fetchall()
        for r in rows:
            text, images = self._prompt_text(message_from_json({"role": "user", "parts": json.loads(r["parts"])}))
            if (text or images) and not text.startswith(self.NOTICE_PREFIXES):
                self.db.execute("insert into prompt_history (session_id, text, image_count, created) values (?, ?, ?, ?)",
                                (r["session_id"], text, images, r["created"]))

    @staticmethod
    def _prompt_text(message: Message) -> tuple[str, int]:
        import re
        text = "".join(p.text for p in message.parts
                       if isinstance(p, Text) and not re.fullmatch(r"\[Image [A-Z]\]", p.text))
        return text, sum(isinstance(p, Image) for p in message.parts)

    def record_prompt(self, session_id: str, message: Message) -> None:
        """Remember what the user sent, for Up-arrow recall in every client."""
        text, images = self._prompt_text(message)
        if text.strip() or images:
            with self.db:
                self.db.execute("insert into prompt_history (session_id, text, image_count, created) values (?, ?, ?, ?)",
                                (session_id, text, images, time.time()))

    def prompt_history(self, project_id: str, *, q: str = "", limit: int = 50, before: int | None = None) -> list[dict[str, Any]]:
        """The project's prompts, newest first, each distinct text once (at its newest use)."""
        sql = ("select max(h.id) id, h.text, h.image_count, h.session_id, h.created from prompt_history h"
               " join session s on s.id = h.session_id where s.project_id = ?")
        params: list[Any] = [project_id]
        if q:
            sql += " and h.text like ? escape '\\'"
            params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        sql += " group by h.text, h.image_count"
        if before is not None:
            sql += " having max(h.id) < ?"
            params.append(before)
        sql += " order by id desc limit ?"
        params.append(limit)
        return [{"id": r["id"], "text": r["text"], "image_count": r["image_count"],
                 "session_id": r["session_id"], "created": r["created"]} for r in self.db.execute(sql, params)]

    def append_message(self, session_id: str, message: Message) -> StoredMessage:
        now = time.time()
        with self.db:
            return self._insert_message(session_id, message, now)

    def messages(self, session_id: str) -> list[StoredMessage]:
        rows = self.db.execute("select * from message where session_id = ? order by seq", (session_id,))
        return [
            StoredMessage(r["id"], r["session_id"], r["seq"], message_from_json({"role": r["role"], "parts": json.loads(r["parts"])}), r["created"], r["delivery"])
            for r in rows
        ]

    def messages_after_seq(self, session_id: str, seq: int) -> list[StoredMessage]:
        """Messages after a turn's starting sequence; recovery uses this, not a clock."""
        rows = self.db.execute(
            "select * from message where session_id = ? and seq > ? order by seq", (session_id, seq))
        return [
            StoredMessage(r["id"], r["session_id"], r["seq"], message_from_json({"role": r["role"], "parts": json.loads(r["parts"])}), r["created"], r["delivery"])
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
                          message_from_json({"role": r["role"], "parts": json.loads(r["parts"])}), r["created"], r["delivery"])
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

    # -- mutation journal ------------------------------------------------------

    def mutation(self, key: str) -> dict[str, Any] | None:
        """One accepted mutation by client key, including its replayed response."""
        row = self.db.execute("select * from mutation where key = ?", (key,)).fetchone()
        if row is None:
            return None
        return {
            "key": row["key"], "fingerprint": row["fingerprint"], "operation": row["operation"],
            "target": row["target"], "accepted": row["accepted"], "state": row["state"],
            "status": row["status"], "body": json.loads(row["body"]) if row["body"] else None,
            "turn_id": row["turn_id"], "resource": row["resource"], "resolved": row["resolved"],
            "expires": row["expires"],
        }

    def _encode_body(self, mutation: dict[str, Any], body: Any) -> str | None:
        encoded = None if body is None else json.dumps(body, separators=(",", ":"))
        if encoded is not None and len(encoded) > MUTATION_BODY_MAX:
            bounded = {
                "replayed": True, "bounded": True, "key": mutation["key"],
                "operation": mutation["operation"], "target": mutation["target"],
                "note": "the original response exceeded the retained size; inspect the resource for its current state",
                "resource": mutation.get("resource"), "turn_id": mutation.get("turn_id"),
            }
            if isinstance(body, dict) and "id" in body:
                bounded["id"] = body["id"]
            encoded = json.dumps(bounded)
        return encoded

    def _insert_mutation(self, mutation: dict[str, Any], *, now: float) -> None:
        """Insert a journal row inside the caller's transaction."""
        encoded = self._encode_body(mutation, mutation.get("body"))
        resolved = mutation.get("resolved")
        expires = mutation.get("expires")
        self.db.execute(
            "insert into mutation (key, fingerprint, operation, target, accepted, state, status, body,"
            " turn_id, resource, resolved, expires) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mutation["key"], mutation["fingerprint"], mutation["operation"], mutation["target"], now,
             mutation.get("state") or "resolved", mutation.get("status"), encoded, mutation.get("turn_id"),
             mutation.get("resource"), resolved, expires),
        )

    def begin_mutation(self, key: str, fingerprint: str, operation: str, target: str,
                       *, resource: str | None = None) -> dict[str, Any]:
        """Persist intent for a keyed external effect before it runs."""
        now = time.time()
        with self.db:
            self._insert_mutation({
                "key": key, "fingerprint": fingerprint, "operation": operation, "target": target,
                "state": "pending", "status": None, "body": None, "resource": resource,
            }, now=now)
        entry = self.mutation(key)
        assert entry is not None
        return entry

    def finish_mutation(self, key: str, *, status: int, body: Any = None, resource: str | None = None,
                        lifetime: float = 7 * 24 * 3600) -> dict[str, Any]:
        """Record durable evidence that a keyed external effect returned."""
        now = time.time()
        entry = self.mutation(key)
        if entry is None:
            raise KeyError(f"no pending mutation {key}")
        encoded = self._encode_body(entry, body)
        with self.db:
            self.db.execute(
                "update mutation set state = 'resolved', status = ?, body = ?, resource = coalesce(?, resource),"
                " resolved = ?, expires = ? where key = ?",
                (status, encoded, resource, now, now + lifetime, key))
        finished = self.mutation(key)
        assert finished is not None
        return finished

    def discard_mutation(self, key: str) -> None:
        """Forget a pending intent whose request was refused before it took effect."""
        with self.db:
            self.db.execute("delete from mutation where key = ? and state = 'pending'", (key,))

    def resolve_pending_mutations(self, *, lifetime: float = 7 * 24 * 3600,
                                  now: float | None = None) -> int:
        """Turn an interrupted external effect into a stable, expiring unknown outcome."""
        moment = time.time() if now is None else now
        body = json.dumps({
            "error": "the daemon stopped before this operation's result was recorded; its outcome is unknown. "
                     "Inspect the target before trying again.",
            "code": "mutation_outcome_unknown", "replayed": True, "bounded": True,
        })
        with self.db:
            return self.db.execute(
                "update mutation set state = 'resolved', status = 409, body = ?, resolved = ?, expires = ?"
                " where state = 'pending'",
                (body, moment, moment + lifetime)).rowcount

    def record_mutation(self, key: str, fingerprint: str, operation: str, target: str, *,
                        status: int | None, body: Any = None, turn_id: str | None = None,
                        resource: str | None = None, lifetime: float = 7 * 24 * 3600) -> dict[str, Any]:
        """Record a completed mutation (keyed requests without their own transaction)."""
        now = time.time()
        with self.db:
            self._insert_mutation({
                "key": key, "fingerprint": fingerprint, "operation": operation, "target": target,
                "state": "resolved", "status": status, "body": body, "turn_id": turn_id,
                "resource": resource, "resolved": now, "expires": now + lifetime,
            }, now=now)
        entry = self.mutation(key)
        assert entry is not None
        return entry

    def resolve_mutations_for_turn(self, turn_id: str, *, lifetime: float = 7 * 24 * 3600) -> int:
        """Start the deduplication lifetime once a keyed turn reaches a terminal state."""
        now = time.time()
        with self.db:
            return self.db.execute(
                "update mutation set state = 'resolved', resolved = ?, expires = ?"
                " where turn_id = ? and state != 'resolved'",
                (now, now + lifetime, turn_id)).rowcount

    def prune_mutations(self, now: float | None = None) -> int:
        """Remove expired resolved entries; unresolved entries never expire."""
        cutoff = time.time() if now is None else now
        with self.db:
            return self.db.execute(
                "delete from mutation where state = 'resolved' and expires is not null and expires < ?",
                (cutoff,)).rowcount

    def delete_mutation(self, key: str) -> None:
        with self.db:
            self.db.execute("delete from mutation where key = ?", (key,))

    # -- provider retry attempts ----------------------------------------------

    def start_attempt(self, turn_id: str, session_id: str, request_id: str | None, attempt_id: str,
                      attempt: int, *, delay: float | None = None, error: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "insert into turn_attempt (id, turn_id, request_id, session_id, attempt, status, delay,"
                " started, error) values (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)"
                " on conflict(id) do update set delay = excluded.delay, error = excluded.error",
                (attempt_id, turn_id, request_id, session_id, attempt, delay, time.time(), error),
            )

    def mark_attempt_started(self, attempt_id: str) -> None:
        with self.db:
            self.db.execute(
                "update turn_attempt set status = 'started', started = ? where id = ?",
                (time.time(), attempt_id))

    def finish_attempt(self, attempt_id: str, status: str, *, error: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "update turn_attempt set status = ?, finished = ?, error = coalesce(?, error) where id = ?",
                (status, time.time(), error, attempt_id))

    def attempts_for_turn(self, turn_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "select * from turn_attempt where turn_id = ? order by started, attempt, id", (turn_id,))
        return [dict(row) for row in rows]

    # -- turn execution lifecycle ---------------------------------------------

    def start_turn(self, session_id: str, *, start_seq: int | None = None, started: float | None = None,
                   data: dict[str, Any] | None = None, mutation: dict[str, Any] | None = None,
                   input_id: str | None = None) -> dict[str, Any]:
        """Record an accepted turn. The row is durable before the task starts.

        `start_seq` is the last message sequence before the turn; recovery uses it
        instead of a clock. The explicit form also creates a legacy turn for an
        older session whose saved context ends in an unfinished tool call.

        `mutation` records an idempotent request acceptance in the same
        transaction as the turn, so a crash cannot leave one without the other.
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
            if input_id is not None:
                claimed = self.db.execute("update pending_input set turn_id = ? where id = ? and session_id = ? and state = 'pending' and turn_id is null", (turn["id"], input_id, session_id))
                if claimed.rowcount != 1:
                    raise ValueError("input is no longer available")
            if mutation is not None:
                mutation = dict(mutation)
                body = mutation.get("body")
                if isinstance(body, dict):
                    mutation["body"] = {**body, "status": "busy", "turn_id": turn["id"],
                                        "origin": turn["data"].get("origin")}
                mutation.setdefault("resource", session_id)
                self._insert_mutation({**mutation, "turn_id": turn["id"]}, now=now)
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
            "ended": json.loads(row["ended"]) if row["ended"] else None,
        }

    def turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.db.execute("select * from turn where id = ?", (turn_id,)).fetchone()
        return self._turn_row(row) if row else None

    def unfinished_turns(self) -> list[dict[str, Any]]:
        """Turns a restart must reconcile: still running, or recovery did not finish."""
        rows = self.db.execute(
            "select * from turn where status = 'running' or recovery = 'pending' order by started, id")
        return [self._turn_row(row) for row in rows]

    def running_turns(self) -> list[dict[str, Any]]:
        rows = self.db.execute("select * from turn where status = 'running' order by started, id")
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

    def mark_turn_ended(self, turn_id: str, stop: str, steps: int) -> None:
        """Record that the loop ended the turn (its `TurnEnd`), before the turn is closed."""
        with self.db:
            self.db.execute("update turn set ended = ? where id = ?",
                            (json.dumps({"stop": stop, "steps": steps}), turn_id))

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

    def start_request(self, turn_id: str, session_id: str, step: int, *, model: str | None = None,
                      effort: str | None = None) -> dict[str, Any]:
        """Record one provider request before it is sent, with the log watermark.

        `model` is the `provider/model` ref this step runs with and `effort` its
        reasoning effort; both are read after the loop re-binds the agent, so a
        model change between steps is recorded per request.
        """
        now = time.time()
        request = {"id": new_id("req"), "turn_id": turn_id, "session_id": session_id, "step": step,
                   "status": "started", "started": now, "finished": None,
                   "event_after": self.log_head(), "message_id": None, "stop": None,
                   "response_id": None, "error": None, "parts": [], "data": {},
                   "model": model, "effort": effort, "usage": None, "reasoning_seconds": None}
        with self.db:
            self.db.execute(
                "insert into turn_request (id, turn_id, session_id, step, status, started, event_after, model, effort)"
                " values (?, ?, ?, ?, 'started', ?, ?, ?, ?)",
                (request["id"], turn_id, session_id, step, now, request["event_after"], model, effort),
            )
        return request

    def record_usage(self, request_id: str, attempt_id: str | None, usage: Usage) -> None:
        """Store one attempt's reported usage, re-sum its request and update the ledger.

        A provider reports one cumulative `Usage` per response, so the attempt's
        columns are replaced (the last report wins). The request holds the sum
        over its attempts that reported usage, and `session_usage` gains the
        difference for the request's model. Repeating the same `Usage` changes
        nothing.
        """
        new = tuple(int(getattr(usage, c) or 0) for c in USAGE_COLUMNS)
        sets = ", ".join(f"{c} = ?" for c in USAGE_COLUMNS)
        with self.db:
            request = self.db.execute(
                "select session_id, model, " + ", ".join(USAGE_COLUMNS) + " from turn_request where id = ?",
                (request_id,)).fetchone()
            if request is None:
                return
            counted = request["input_tokens"] is not None  # an earlier attempt already counted this request
            attempt = None if attempt_id is None else self.db.execute(
                "select " + ", ".join(USAGE_COLUMNS) + " from turn_attempt where id = ? and request_id = ?",
                (attempt_id, request_id)).fetchone()
            if attempt is not None:
                old = _usage_values(attempt)
                self.db.execute(f"update turn_attempt set {sets} where id = ?", (*new, attempt_id))
                self.db.execute(
                    f"update turn_request set {sets} where id = ?",
                    (*self.db.execute(
                        "select " + ", ".join(f"sum({c})" for c in USAGE_COLUMNS)
                        + " from turn_attempt where request_id = ? and input_tokens is not null",
                        (request_id,)).fetchone(), request_id))
            else:  # no attempt row to hold it: the request's own report is replaced
                old = _usage_values(request)
                self.db.execute(f"update turn_request set {sets} where id = ?", (*new, request_id))
            delta = [n - o for n, o in zip(new, old or (0, 0, 0, 0), strict=True)]
            if counted and not any(delta):
                return
            model = request["model"] or UNKNOWN_MODEL
            self.db.execute(
                "insert into session_usage (session_id, model, since) values (?, ?, ?)"
                " on conflict(session_id, model) do nothing", (request["session_id"], model, time.time()))
            self.db.execute(
                "update session_usage set requests = requests + ?, "
                + ", ".join(f"{c} = {c} + ?" for c in USAGE_COLUMNS) + " where session_id = ? and model = ?",
                (0 if counted else 1, *delta, request["session_id"], model))

    def session_usage(self, session_id: str) -> dict[str, Any]:
        """The session's spend ledger, which outlives pruned turns."""
        rows = self.db.execute(
            "select * from session_usage where session_id = ? order by model", (session_id,)).fetchall()
        return {
            "since": min((row["since"] for row in rows), default=None),
            "models": {row["model"]: {"requests": row["requests"],
                                      **dict(zip(USAGE_KEYS, (row[c] for c in USAGE_COLUMNS), strict=True))}
                       for row in rows},
        }

    @staticmethod
    def _request_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "turn_id": row["turn_id"], "session_id": row["session_id"],
            "step": row["step"], "status": row["status"], "started": row["started"],
            "finished": row["finished"], "event_after": row["event_after"],
            "message_id": row["message_id"], "stop": row["stop"], "response_id": row["response_id"],
            "error": json.loads(row["error"]) if row["error"] else None,
            "parts": json.loads(row["parts"] or "[]"), "data": json.loads(row["data"] or "{}"),
            "model": row["model"], "effort": row["effort"], "usage": _usage_json(row),
            "reasoning_seconds": row["reasoning_seconds"],
        }

    def request(self, request_id: str) -> dict[str, Any] | None:
        row = self.db.execute("select * from turn_request where id = ?", (request_id,)).fetchone()
        return self._request_row(row) if row else None

    def requests_for_turn(self, turn_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("select * from turn_request where turn_id = ? order by step, started", (turn_id,))
        return [self._request_row(row) for row in rows]

    def finish_request(self, request_id: str, *, stop: str | None, response_id: str | None,
                       parts: list[dict[str, Any]], reasoning_seconds: float | None = None) -> None:
        with self.db:
            self.db.execute(
                "update turn_request set status = 'completed', finished = ?, stop = ?, response_id = ?, parts = ?,"
                " reasoning_seconds = coalesce(?, reasoning_seconds) where id = ? and status = 'started'",
                (time.time(), stop, response_id, json.dumps(parts), reasoning_seconds, request_id),
            )

    def interrupt_request(self, request_id: str, error: dict[str, Any] | None = None,
                          reason: str | None = None, *, reasoning_seconds: float | None = None) -> None:
        """Close an unfinished provider request; its streamed output stays in the event log."""
        with self.db:
            self.db.execute(
                "update turn_request set status = 'interrupted', finished = ?, error = ?, data = ?,"
                " reasoning_seconds = coalesce(?, reasoning_seconds) where id = ? and status = 'started'",
                (time.time(), json.dumps(error) if error else None,
                 json.dumps({"reason": reason} if reason else {}), reasoning_seconds, request_id),
            )

    def fail_request(self, request_id: str, error: dict[str, Any], *,
                     reasoning_seconds: float | None = None) -> None:
        with self.db:
            self.db.execute(
                "update turn_request set status = 'failed', finished = ?, error = ?,"
                " reasoning_seconds = coalesce(?, reasoning_seconds) where id = ? and status = 'started'",
                (time.time(), json.dumps(error), reasoning_seconds, request_id),
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

    # -- consistent reads ------------------------------------------------------

    @contextmanager
    def read_transaction(self) -> Iterator[None]:
        """Hold one SQLite read snapshot across several selects."""
        if self.db.in_transaction:
            yield
            return
        self.db.execute("begin")
        try:
            yield
        finally:
            self.db.execute("commit")

    def state_snapshot(self, *, session_id: str | None = None, message_limit: int = 200) -> dict[str, Any]:
        """Projects, sessions, pending state and an optional bounded session view at one log head."""
        with self.read_transaction():
            head = self.log_head()
            floor = self.log_floor()
            generation = self.log_generation()
            projects = [dict(row) for row in self.db.execute("select * from project order by updated desc")]
            sessions = [dict(row) for row in self.db.execute("select * from session order by updated desc")]
            out: dict[str, Any] = {
                "watermark": {"generation": generation, "head": head, "floor": floor,
                              "head_cursor": f"{generation}:{head}"},
                "projects": projects,
                "sessions": sessions,
                "selected": None,
            }
            if session_id is not None:
                rows = self.db.execute(
                    "select * from message where session_id = ? order by seq desc limit ?",
                    (session_id, message_limit + 1)).fetchall()
                more = len(rows) > message_limit
                rows = list(reversed(rows[:message_limit]))
                out["selected"] = {
                    "session_id": session_id,
                    "messages": [
                        {"id": row["id"], "session_id": row["session_id"], "seq": row["seq"],
                         "created": row["created"], "role": row["role"],
                         "parts": json.loads(row["parts"]),
                         **({"delivery": row["delivery"]} if row["delivery"] else {})}
                        for row in rows
                    ],
                    "messages_limit": message_limit,
                    "messages_more": more,
                    "oldest_seq": rows[0]["seq"] if rows else None,
                }
            return out

    # -- turn summaries ----------------------------------------------------------

    def turn_summaries(self, session_id: str, *, after: str | None = None, before: str | None = None,
                       limit: int = TURN_PAGE) -> dict[str, Any]:
        """One page of turn summaries, oldest to newest, with the session's ledger.

        No cursor: the newest `limit` turns. `after`: the oldest `limit` turns
        started after that turn. `before`: the newest `limit` turns started
        before it. Requests, attempts and tools are read in one query each for
        the whole page. `floor.seq` is the first message seq a summary covers;
        messages below it (pruned turns, history from before turns were
        recorded) have none. It is `None` when every message is covered.
        """
        if after is not None and before is not None:
            raise ValueError("give after or before, not both")
        if not 1 <= limit <= TURN_PAGE_MAX:
            raise ValueError(f"limit must be 1..{TURN_PAGE_MAX}")
        with self.read_transaction():
            anchor = None
            if after is not None or before is not None:
                anchor = self.db.execute(
                    "select started, id from turn where id = ? and session_id = ?",
                    (after or before, session_id)).fetchone()
                if anchor is None:
                    raise ValueError(f"unknown turn {after or before} in session {session_id}")
            if after is not None:
                rows = self.db.execute(
                    "select * from turn where session_id = ? and (started, id) > (?, ?)"
                    " order by started, id limit ?",
                    (session_id, anchor["started"], anchor["id"], limit + 1)).fetchall()
                more_after, more_before = len(rows) > limit, True
                rows = rows[:limit]
            else:
                if before is not None:
                    rows = self.db.execute(
                        "select * from turn where session_id = ? and (started, id) < (?, ?)"
                        " order by started desc, id desc limit ?",
                        (session_id, anchor["started"], anchor["id"], limit + 1)).fetchall()
                else:
                    rows = self.db.execute(
                        "select * from turn where session_id = ? order by started desc, id desc limit ?",
                        (session_id, limit + 1)).fetchall()
                more_before, more_after = len(rows) > limit, before is not None
                rows = list(reversed(rows[:limit]))
            turns = self._summaries(rows)
            covered = self.db.execute(
                "select min(start_seq) from turn where session_id = ?", (session_id,)).fetchone()[0]
            if covered is None:  # no turn rows: no message has a summary
                covered = self.db.execute(
                    "select coalesce(max(seq), 0) from message where session_id = ?", (session_id,)).fetchone()[0]
            return {"turns": turns, "more_before": more_before, "more_after": more_after,
                    "floor": {"seq": covered + 1} if covered > 0 else None,
                    "usage": self.session_usage(session_id)}

    def turn_summary(self, turn_id: str) -> dict[str, Any] | None:
        """One turn's summary, the same entry `turn_summaries` returns."""
        with self.read_transaction():
            rows = self.db.execute("select * from turn where id = ?", (turn_id,)).fetchall()
            summaries = self._summaries(rows)
        return summaries[0] if summaries else None

    def _summaries(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """Summaries for turn rows, in their order, with three queries for all of them."""
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        marks = ", ".join("?" * len(ids))
        requests: dict[str, list[dict[str, Any]]] = {turn_id: [] for turn_id in ids}
        by_request: dict[str, list[dict[str, Any]]] = {}
        for row in self.db.execute(
                f"select * from turn_request where turn_id in ({marks}) order by step, started, id", ids):
            attempts: list[dict[str, Any]] = []
            by_request[row["id"]] = attempts
            requests[row["turn_id"]].append({
                "id": row["id"], "step": row["step"], "status": row["status"],
                "model": row["model"], "effort": row["effort"],
                "started": row["started"], "finished": row["finished"], "stop": row["stop"],
                "error": json.loads(row["error"]) if row["error"] else None,
                "message_id": row["message_id"], "usage": _usage_json(row),
                "reasoning_seconds": row["reasoning_seconds"], "attempts": attempts,
            })
        for row in self.db.execute(
                f"select * from turn_attempt where turn_id in ({marks}) order by attempt, started, id", ids):
            if row["request_id"] in by_request:
                by_request[row["request_id"]].append({
                    "n": row["attempt"], "status": row["status"], "delay": row["delay"],
                    "started": row["started"], "finished": row["finished"], "error": row["error"],
                    "usage": _usage_json(row),
                })
        tools: dict[str, list[dict[str, Any]]] = {turn_id: [] for turn_id in ids}
        for row in self.db.execute(
                "select turn_id, call_id, name, state, started, finished, is_error from turn_tool"
                f" where turn_id in ({marks}) order by rowid", ids):
            tools[row["turn_id"]].append({
                "call_id": row["call_id"], "name": row["name"], "state": row["state"],
                "started": row["started"], "finished": row["finished"], "is_error": bool(row["is_error"]),
            })
        out = []
        for row in rows:
            ended = json.loads(row["ended"]) if row["ended"] else None
            turn_requests = requests[row["id"]]
            stop = ended["stop"] if ended else (turn_requests[-1]["stop"] if turn_requests else None)
            out.append({
                "id": row["id"], "status": row["status"], "started": row["started"], "finished": row["finished"],
                "start_seq": row["start_seq"], "steps": row["steps"], "stop": stop, "ended": ended,
                "error": json.loads(row["error"]) if row["error"] else None, "recovery": row["recovery"],
                "requests": turn_requests, "tools": tools[row["id"]],
            })
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
