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
session's `AGENTS.md` record (`nexus/snapshot.py`). `event` keeps every bus event
that belongs to a session (deltas, wire requests and SSE events included), in
order, so the inspector can show any past session down to the raw wire traffic.
Rows are deleted with their session. Messages are append-only rows in session
order.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codec import message_from_json, message_to_json
from .llmhub.types import Message
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
  session_id text not null references session(id) on delete cascade,
  seq integer not null,
  time real not null,
  type text not null,
  data text not null
);
create index if not exists event_session on event(session_id, id);
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
  unique (session_id, seq)
);
create table if not exists file_observation (
  session_id text not null references session(id) on delete cascade,
  path text not null,
  data text not null,
  updated real not null,
  primary key (session_id, path)
);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000):x}{secrets.token_hex(4)}"


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
        self.db.execute("create index if not exists session_parent on session(parent_id)")

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

    def append_event(self, event: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "insert into event (session_id, seq, time, type, data) values (?, ?, ?, ?, ?)",
                (event["session_id"], event["seq"], event["time"], event["type"], json.dumps(event["data"])),
            )

    def events(self, session_id: str, *, after: int = 0, limit: int = 1000, types: list[str] | None = None) -> list[dict[str, Any]]:
        sql = "select * from event where session_id = ? and id > ?"
        params: list[Any] = [session_id, after]
        if types:
            sql += f" and type in ({', '.join('?' * len(types))})"
            params += types
        rows = self.db.execute(sql + " order by id limit ?", (*params, limit))
        return [{"id": r["id"], "seq": r["seq"], "time": r["time"], "type": r["type"], "session_id": r["session_id"],
                 "data": json.loads(r["data"])} for r in rows]

    def event_counts(self, session_id: str) -> dict[str, int]:
        rows = self.db.execute("select type, count(*) n from event where session_id = ? group by type", (session_id,))
        return {r["type"]: r["n"] for r in rows}

    # -- messages ------------------------------------------------------------

    def append_message(self, session_id: str, message: Message) -> StoredMessage:
        now = time.time()
        with self.db:
            seq = self.db.execute("select coalesce(max(seq), 0) + 1 from message where session_id = ?", (session_id,)).fetchone()[0]
            stored = StoredMessage(new_id("msg"), session_id, seq, message, now)
            self.db.execute(
                "insert into message values (?, ?, ?, ?, ?, ?)",
                (stored.id, session_id, seq, message.role, json.dumps(message_to_json(message)["parts"]), now),
            )
            self.db.execute("update session set updated = ? where id = ?", (now, session_id))
        return stored

    def messages(self, session_id: str) -> list[StoredMessage]:
        rows = self.db.execute("select * from message where session_id = ? order by seq", (session_id,))
        return [
            StoredMessage(r["id"], r["session_id"], r["seq"], message_from_json({"role": r["role"], "parts": json.loads(r["parts"])}), r["created"])
            for r in rows
        ]
