"""Sessions dashboard projection and visibility-scoped preview polling.

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
POLL_SECONDS = 0.5


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
        self.preview = None
        self.generation = 0  # latest-request guard for previews
        self.epoch = 0  # bumped on open and close: a stale list refresh cannot publish
        self.task = None
        self.error = ""

    async def open(self):
        self.close()
        self.visible = True
        self.selected = ""
        self.preview = None
        self.error = ""
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
        epoch = self.epoch
        result = await self.shell.client.project_sessions()
        if not self.visible or epoch != self.epoch:
            return
        self.rows = _plain(session_rows(result, self.shell.controller.session, self.shell.seen_seq))
        self.shell.refresh_session_tabs(self.rows)
        self.shell.sessions_truncated = bool(result.truncated)
        ids = [row["id"] for row in self.rows if row.get("id")]
        if self.selected not in ids:
            await self.select(self.shell.controller.session if self.shell.controller.session in ids else next(iter(ids), ""))

    async def select(self, session_id):
        if not self.visible:
            return
        if session_id != self.selected:
            self.preview = None
        self.selected = session_id
        self.generation += 1
        await self.refresh_preview()

    async def refresh_preview(self):
        generation, selected = self.generation, self.selected
        if not self.visible or not selected:
            return
        try:
            preview = await self.shell.client.dashboard_preview(selected, max_rows=12)
        except Exception as exc:
            if self.visible and generation == self.generation:
                self.error = escape_controls(str(exc))
            return
        if self.visible and generation == self.generation and selected == self.selected:
            self.preview = _plain(preview)
            self.error = ""

    async def _poll(self):
        while self.visible:
            await asyncio.sleep(POLL_SECONDS)
            if not self.visible:
                return
            before = repr(self.snapshot())
            try:
                await self.refresh()
                await self.refresh_preview()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.visible:
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
        if kind in {"sessions_close", "sessions_refresh", "session_new", "worktrees", "archived"}:
            return True
        row = self.row(operation.get("id"))
        if row is None:
            return False
        if kind in {"session_preview", "session_archive"}:
            return True
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

    def snapshot(self):
        if not self.visible:
            return None
        from ...host_support.session_preview import message_tails, worked_label
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
        preview = self.preview
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
            if isinstance(preview, dict) and preview.get("session") == current:
                preview = dict(preview)
                for key in ("last_user", "last_agent"):
                    value = tails.get(key) or ""
                    if value:
                        preview[key] = escape_controls(value)
                preview["activity"] = escape_controls(tails.get("activity") or "")
                worked = worked_label(view)
                if worked:
                    preview["worked"] = worked
        return {
            "kind": "sessions", "rows": rows, "selected": self.selected,
            "preview": preview, "error": self.error,
            "truncated": bool(self.shell.sessions_truncated),
            "path": escape_controls(path), "branch": escape_controls(branch),
            "worktree": escape_controls(str(getattr(self.shell, "worktree", "") or ""))[:80],
            "workspace": escape_controls(workspace),
        }
