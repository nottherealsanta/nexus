"""Sessions dashboard projection and visibility-scoped list polling.

``snapshot()`` is the ``dashboard`` field of the process-bridge payload: ``None`` unless the
dashboard is visible. ``close()`` runs when the surface is dismissed, replaced by another
session, or the client shuts down. Selection never opens a session.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, is_dataclass
from pathlib import Path

from ...ui_support.text import escape_controls

BUSY_STATUS = {"working", "input"}
BUSY_STATE = {"running", "awaiting_input", "awaiting_permission"}
TITLE_LIMIT = 200
QUERY_LIMIT = 256
POLL_SECONDS = 0.5
ARCHIVED_LIMIT = 20  # newest archived sessions on the dashboard
PERMISSION_DECISIONS = {"allow_once", "deny_once"}  # what a "Needs you" row may answer


def _plain(value):
    fields = getattr(type(value), "__struct_fields__", None)  # host msgspec structs, duck-typed
    if fields is not None:
        value = {key: getattr(value, key) for key in fields}
    elif is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, str):
        return escape_controls(value)
    return value


def _home_path(path: str) -> str:
    home = str(Path.home()).rstrip("/")
    if home and Path(home).is_absolute():
        if path == home:
            return "~"
        if path.startswith(home + "/"):
            return "~" + path[len(home):]
    return path


def _location(shell) -> tuple[str, str]:
    """``breadcrumb`` is ``path › branch``. The header draws them separately."""
    breadcrumb = getattr(shell, "breadcrumb", "") or ""
    path, separator, branch = breadcrumb.partition(" › ")
    return _home_path(path), branch if separator else ""


def is_busy(row) -> bool:
    """A working or waiting session: it cannot be archived from the dashboard."""
    return row.get("status") in BUSY_STATUS or row.get("state") in BUSY_STATE


class SessionDashboard:
    def __init__(self, shell):
        self.shell = shell
        self.visible = False
        self.rows = []
        self.selected = ""
        self.generation = 0  # latest-request guard for list refreshes
        self.epoch = 0  # bumped on open and close: a stale list refresh cannot publish
        self.task = None
        self.error = ""
        self.query = ""
        self.rows_query = ""
        self.list_generation = 0
        #: Seconds between list refreshes while visible; ``None`` uses ``POLL_SECONDS``
        #: (the sidebar, which stays open for long stretches, polls less often).
        self.poll_seconds = None

    async def open(self):
        self.close()
        self.visible = True
        self.selected = ""
        self.error = ""
        self.query = ""
        self.rows_query = ""
        self.rows = []
        manager = getattr(self.shell, "manager", None)
        if manager is not None:
            await manager.refresh()  # the pinned entry shows the current status the moment the page opens
        await self.refresh()
        if self.visible:
            self.task = asyncio.create_task(self._poll())

    def close(self):
        self.visible = False
        self.epoch += 1
        self.generation += 1
        task, self.task = self.task, None
        if task and task is not asyncio.current_task():
            task.cancel()

    async def refresh(self):
        from .workflows import session_rows
        epoch, query = self.epoch, self.query
        self.list_generation += 1
        generation = self.list_generation
        # Archived sessions ride along (collapsed "Archived" section); a search matches them too.
        result = await self.shell.client.project_sessions(
            limit=1000 if query else 20, query=query, archived=ARCHIVED_LIMIT)
        if not self.visible or epoch != self.epoch or query != self.query or generation != self.list_generation:
            return
        self.rows = _plain(session_rows(result, self.shell.controller.session, self.shell.seen_seq))
        self.rows_query = query
        self.shell.refresh_session_tabs([row for row in self.rows if not row.get("archived")])
        self.shell.sessions_truncated = bool(result.truncated)
        ids = [row["id"] for row in self.rows if row.get("id")]
        if self.selected not in ids:
            self.selected = self.shell.controller.session if self.shell.controller.session in ids else next(iter(ids), "")
            self.generation += 1

    async def search(self, query):
        if not self.visible or not isinstance(query, str) or len(query) > QUERY_LIMIT:
            return
        self.query = query
        self.rows = []
        self.selected = ""
        self.error = ""
        self.generation += 1
        await self.refresh()

    async def _poll(self):
        while self.visible:
            await asyncio.sleep(POLL_SECONDS if self.poll_seconds is None else self.poll_seconds)
            if not self.visible:
                return
            before = repr(self.snapshot())
            epoch, query = self.epoch, self.query
            manager = getattr(self.shell, "manager", None)
            try:
                if manager is not None and manager.due():
                    await manager.refresh()
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.visible and epoch == self.epoch and query == self.query:
                    self.error = escape_controls(str(exc))
            update = getattr(self.shell, "on_update", None)
            if self.visible and update is not None and repr(self.snapshot()) != before:
                with contextlib.suppress(Exception):
                    await update()

    def row(self, session_id):
        return next((row for row in self.rows if row.get("id") == session_id), None)

    def accepts(self, operation) -> bool:
        """Is ``operation`` one the visible dashboard offers? Ids must name a listed row."""
        if not self.visible or not isinstance(operation, dict):
            return False
        kind = operation.get("kind")
        if kind == "sessions_search":
            query = operation.get("query")
            return isinstance(query, str) and len(query) <= QUERY_LIMIT
        if kind in {"sessions_close", "sessions_refresh", "session_new", "worktrees", "archived",
                    "manager_open", "manager_live"}:
            return True
        row = self.row(operation.get("id"))
        if row is None:
            return False
        if kind in {"session_archive", "ownership_tree"}:
            return True
        if kind == "archived_preview":
            return bool(row.get("archived"))
        if kind == "session_permission":
            pending = row.get("pending") or {}
            return bool(pending.get("request_id")) and pending["request_id"] == operation.get("request_id") \
                and operation.get("decision") in PERMISSION_DECISIONS
        if kind == "session_rename":
            title = operation.get("title")
            return isinstance(title, str) and bool(title.strip())
        if kind == "session_open":
            return operation.get("workspace") in {row.get("workspace"), row.get("repo")}
        return False

    async def rename(self, session_id, title):
        title = " ".join(str(title).split())[:TITLE_LIMIT]
        if not title:
            return
        await self.shell.client.rename_session(session_id, title)
        await self.refresh()

    async def archive(self, session_id):
        row = self.row(session_id)
        if row is not None and is_busy(row):
            self.shell.flash("A working session can't be archived — wait for it to finish", "warning")
            return
        await self.shell.client.archive_session(session_id)
        if self.visible:
            await self.refresh()
            self.shell.toast("Session archived", "success")

    async def resolve_permission(self, session_id, request_id, decision):
        """Answer a "Needs you" row's permission without opening the session."""
        resolved = await self.shell.client.resolve_permission(session_id, request_id, decision)
        if not resolved:
            self.shell.flash("Permission already resolved by another client", "warning")
        if self.visible:
            await self.refresh()

    def snapshot(self):
        if not self.visible:
            return None
        from ...host_support.session_preview import message_tails
        path, branch = _location(self.shell)
        workspace = str(getattr(self.shell, "workspace", "") or "")
        rows = []
        for row in self.rows:
            item = dict(row)
            worktree = item.get("worktree") or ""
            if worktree:
                item["branch"] = worktree
            elif workspace and (item.get("workspace") == workspace or item.get("repo") == workspace):
                item["branch"] = branch
            else:
                item.setdefault("branch", "")
            rows.append(item)
        controller = getattr(self.shell, "controller", None)
        current = getattr(controller, "session", "") or ""
        view = getattr(controller, "view", None)
        if current and view is not None:
            tails = message_tails(view)
            for item in rows:
                if item.get("id") != current:
                    continue
                for key in ("last_user", "last_agent"):
                    value = tails.get(key) or ""
                    if value:
                        item[key] = escape_controls(value)
                item["activity"] = escape_controls(tails.get("activity") or "")
                break
        snapshot = {
            "kind": "sessions", "rows": rows, "selected": self.selected, "query": self.rows_query,
            "error": self.error,
            "truncated": bool(self.shell.sessions_truncated),
            "path": escape_controls(path), "branch": escape_controls(branch),
            "worktree": escape_controls(str(getattr(self.shell, "worktree", "") or ""))[:80],
            "workspace": escape_controls(workspace),
        }
        manager = getattr(self.shell, "manager", None)
        if manager is not None:  # pinned above the roots, never one of the rows
            snapshot["manager"] = manager.entry()
        return snapshot
