"""Native left sidebar controller: Projects, Sessions, Files and Git tabs (docs/ratatui-parity.md).

Python owns the state; the Rust client draws the ``sidebar`` snapshot block and sends
``{"type": "sidebar", "op": ...}`` actions. Every datum comes from a host command
(``ProjectSessionsList``, ``DirectoryList``, ``GitStatus``) and is control-escaped by
``ui_support/sidebar.py``; this module never reads the filesystem or runs Git.

The Sessions tab reuses ``SessionDashboard`` (filtering, archive, rename, permissions) and
hides the manager session. Results carry a sequence and the workspace they were requested
for, so a slow answer never replaces a newer one or another project's.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import re
import tempfile
import time
from pathlib import Path, PurePath
from types import SimpleNamespace

from ...host_support.errors import error_text
from ...ui_support import sidebar as view

#: Seconds between list refreshes of the Sessions tab while the sidebar is open.
SESSIONS_POLL_SECONDS = 2.0
PROJECT_LIMIT = 300
#: Minimum seconds between background refreshes of the visible tab.
DUE_SECONDS = {"Projects": 10.0, "Files": 5.0, "Git": 3.0}


def _temporary_project(path: str) -> bool:
    """Generated tempfile roots, not projects merely named ``tmp*`` elsewhere.

    Python's default mkdtemp names have eight random characters. Match only direct
    children of system temp roots (including macOS's per-user T directory). Nexus
    worktree/test checkouts can register these in the shared project registry.
    """
    root = PurePath(path)
    if not re.fullmatch(r"tmp[a-z0-9_]{8}", root.name):
        return False
    parent = str(root.parent)
    return (parent in {tempfile.gettempdir().rstrip("/"), "/tmp", "/private/tmp", "/var/tmp"}
            or bool(re.fullmatch(r"/(?:private/)?var/folders/[^/]+/[^/]+/T", parent)))


class SidebarController:
    def __init__(self, shell):
        self.shell = shell
        #: Set by the terminal client; hosts without the sidebar keep the legacy Sessions page.
        self.enabled = False
        #: Bumped only by an explicit ``show(focus=True)``; opening alone leaves focus alone.
        self.focus = 0
        #: Bumped by every ``show``; the client re-shows a sidebar it hid after a session open.
        self.reveal = 0
        self.workspace = ""
        self.session = ""
        self.tasks: set[asyncio.Task] = set()
        self._reset()

    def _reset(self):
        self._project_path = ""
        self._browser_sequence = getattr(self, "_browser_sequence", 0) + 1
        self.projects = {"rows": [], "truncated": False, "error": "", "loaded": False, "browser_open": False}
        self.files = {"path": "", "label": "", "rows": [], "truncated": False, "error": "", "loaded": False}
        self.git = {"available": False, "message": "", "branch": "", "summary": "", "rows": [],
                    "truncated": False, "error": "", "loaded": False}
        self._openable: set[tuple[str, str]] = set()
        self._dirs: set[str] = set()
        self._previewable: set[str] = set()
        self._preview_sequence = 0
        self._files_path = ""
        self.sequence = {tab: 0 for tab in DUE_SECONDS}
        self.due = {tab: 0.0 for tab in DUE_SECONDS}

    @property
    def open(self) -> bool:
        return self.enabled and bool(self.shell.preferences.values["sidebar_open"])

    @property
    def tab(self) -> str:
        return view.normalize_tab(self.shell.preferences.values["sidebar_tab"])

    @property
    def width(self) -> int:
        return view.clamp_width(self.shell.preferences.values["sidebar_width"])

    def _manager_session(self) -> str:
        manager = getattr(self.shell, "manager", None)
        try:
            return str(manager.entry().get("session") or "") if manager is not None else ""
        except Exception:  # noqa: BLE001 - the identity filter falls back to the fixed manager id
            return ""

    # -- lifecycle ---------------------------------------------------------------------

    async def show(self, tab=None, *, focus=False):
        """Open the sidebar (on ``tab``), start its data feeds; opening never steals keyboard focus."""
        prefs = self.shell.preferences
        prefs.set("sidebar_open", True)
        self.reveal += 1  # un-hides a sidebar the client dismissed after opening a session
        if tab:
            prefs.set("sidebar_tab", view.normalize_tab(tab))
        if focus:
            self.focus += 1
        await self.activate()

    async def activate(self):
        """Start the Sessions feed and load the visible tab (idempotent; a closed sidebar does nothing)."""
        if not self.open:
            return
        flows = self.shell.workflows
        dashboard = getattr(flows, "dashboard", None)
        if dashboard is None or not dashboard.visible:
            await flows.open_dashboard()
            dashboard = flows.dashboard
        dashboard.poll_seconds = SESSIONS_POLL_SECONDS
        self._sync_workspace(kick=False)
        await self.refresh(force=True)

    async def close(self):
        self.projects["browser_open"] = False
        self._browser_sequence += 1
        self.shell.preferences.set("sidebar_open", False)
        self.shell.workflows.close_dashboard(force=True)

    async def sync(self):
        """Apply the stored open/closed preference."""
        if self.open:
            await self.activate()
        elif self.enabled:
            self.shell.workflows.close_dashboard(force=True)

    async def browse_project(self, path: str) -> None:
        """Read-only navigation through the host, separate from opening a project."""
        self._browser_sequence += 1
        sequence = self._browser_sequence
        try:
            result = await self.shell.client.list_project_directory(path)
            if sequence != self._browser_sequence:
                return
            self._project_path = result.path
            parent = str(PurePath(result.path).parent)
            self.projects.update(browser_path=result.path, browser_loaded=True,
                                 browser_error="", browser_truncated=result.truncated,
                                 browser_rows=([{"name": "..", "path": parent, "kind": "dir"}]
                                               if parent != result.path else []) + list(result.entries))
        except Exception as exc:
            if sequence == self._browser_sequence:
                self.projects["browser_error"] = view.clean(error_text(exc))

    # -- data --------------------------------------------------------------------------

    def _sync_workspace(self, *, kick=True):
        """Files and Git describe the current session root; drop them when it changes."""
        workspace = str(getattr(self.shell, "workspace", "") or "")
        session = str(getattr(self.shell.controller, "session", "") or "")
        if (workspace, session) == (self.workspace, self.session):
            return
        self.workspace, self.session = workspace, session
        self._reset()
        if kick and self.open:
            self._spawn(self.refresh(force=True))

    def _spawn(self, coroutine):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coroutine.close()
            return
        task = loop.create_task(self._guarded(coroutine))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _guarded(self, coroutine):
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - each loader records its own error text
            return
        update = getattr(self.shell, "on_update", None)
        if update is not None:
            with contextlib.suppress(Exception):
                await update()

    async def refresh(self, tab=None, *, force=False, path=None):
        """Load ``tab`` (default: the visible one) unless it was loaded recently; Sessions has its own poll."""
        tab = tab or ({"Browser": "Files", "Git": "Git"}.get(self.shell.preferences.values.get("details_tab")) if not self.open else None) or self.tab
        if tab not in DUE_SECONDS or not self.enabled or (not self.open and tab not in {"Files", "Git"}):
            return
        self._sync_workspace(kick=False)
        now = time.monotonic()
        if not force and now < self.due[tab]:
            return
        self.due[tab] = now + DUE_SECONDS[tab]
        self.sequence[tab] += 1
        sequence, workspace = self.sequence[tab], str(self.shell.workspace or "")
        session = str(self.shell.controller.session or "")

        def current() -> bool:
            return (self.enabled and sequence == self.sequence[tab]
                    and workspace == str(self.shell.workspace or "") and workspace == self.workspace
                    and session == str(self.shell.controller.session or "")
                    and session == self.session)

        client = self.shell.client
        try:
            if tab == "Projects":
                result = await client.project_sessions(limit=PROJECT_LIMIT, projects=PROJECT_LIMIT)
                if not current():
                    return
                from .workflows import session_rows
                rows = session_rows(result, self.shell.controller.session, {})
                registered = list(getattr(result, "projects", None) or [])
                # The registry is shared with other clients/tests; hide generated temp
                # checkouts here, never delete registry records or directories. A current
                # checkout remains visible even when it is temporary.
                def keep(path):
                    return path == workspace or not _temporary_project(path)

                rows = [row for row in rows if row.get("workspace") == workspace or
                        (keep(str(row.get("repo") or row.get("workspace") or ""))
                         and keep(str(row.get("workspace") or "")))]
                registered = [entry for entry in registered
                              if getattr(entry, "workspace", "") == workspace or
                              (keep(str(getattr(entry, "workspace", "")))
                               and keep(str(getattr(entry, "repo", "") or getattr(entry, "workspace", ""))))]
                recent = self.shell.preferences.values.get("project_recent", [])
                cleaned = [path for path in recent if isinstance(path, str) and
                           (path == workspace or (keep(path) and Path(path).is_dir()))]
                if cleaned != recent:
                    self.shell.preferences.set("project_recent", cleaned)
                known = {getattr(entry, "workspace", "") for entry in registered}
                registered.extend(SimpleNamespace(workspace=path) for path in
                                  dict.fromkeys([workspace, *cleaned])
                                  if path and path not in known)
                listed = view.project_rows(rows, workspace, self._manager_session(), registered)
                self._openable = view.openable(listed)
                self.projects.update({"rows": listed, "truncated": bool(getattr(result, "truncated", False)),
                                 "error": "", "loaded": True})
            elif tab == "Files":
                target = self._files_path if path is None else path
                result = await client.list_directory(target, session=session)
                if not current():
                    return
                payload = view.files_payload(result, workspace)
                self._files_path = payload["path"]
                self._dirs = view.navigable(payload)
                self._previewable = view.previewable(payload)
                self.files = {**payload, "loaded": True}
            else:
                result = await client.git_status(session=session)
                if not current():
                    return
                self.git = {**view.git_payload(result), "loaded": True}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - shown in the tab; never breaks the shell
            if not current():
                return
            message = view.clean(error_text(exc))
            if tab == "Projects":
                self.projects = {**self.projects, "error": message, "loaded": True}
            elif tab == "Files":
                if self._files_path and path is None:  # the listed directory vanished: start over at the root
                    self._files_path, self.due["Files"] = "", 0.0
                self.files = {**self.files, "error": message, "loaded": True}
            else:
                self.git = {**self.git, "error": message, "loaded": True}

    async def poll(self):
        """Called from the shell's background poll: refresh the visible tab when it is due."""
        if not self.enabled or (not self.open and (not self.shell.preferences.values.get("details_sidebar") or self.shell.preferences.values.get("details_tab") not in {"Browser", "Git"})):
            return
        self._sync_workspace()
        await self.refresh()

    def fingerprint(self):
        """Comparable text of everything the poll can change (an unchanged one sends no snapshot)."""
        return repr((self.open, self.tab, self.projects, self.files, self.git)) if self.enabled else ""

    # -- actions from the client -------------------------------------------------------

    async def navigate(self, path):
        self._sync_workspace(kick=False)
        if not isinstance(path, str) or path not in self._dirs:
            raise ValueError("That folder is not listed")
        await self.refresh("Files", force=True, path=path)

    async def preview(self, path):
        """Show a listed file's start in the read-only modal; the host reads and bounds it."""
        self._sync_workspace(kick=False)
        if not isinstance(path, str) or path not in self._previewable:
            raise ValueError("That file is not listed")
        self._preview_sequence += 1
        sequence, workspace, session = self._preview_sequence, self.workspace, self.session
        try:
            result = await self.shell.client.file_preview(path, session=session)
            title, body = view.preview_panel(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the modal says why the file cannot be shown
            title, body = f"File · {view.clean(path, 1024)}", f"Path: {view.clean(path, 1024)}\nError: {view.clean(error_text(exc))}"
        if (sequence != self._preview_sequence or workspace != str(self.shell.workspace or "")
                or session != str(self.shell.controller.session or "")):
            return  # a newer preview was asked for, or the selected root changed meanwhile
        self.shell.show(title, body)

    async def open_session(self, workspace, session):
        if (workspace, session) not in self._openable:
            raise ValueError("That session is no longer listed")
        await self.shell.switch_project(workspace, session)

    async def handle(self, action):
        """One validated ``{"type": "sidebar", "op": ...}`` action."""
        if not self.enabled:
            return
        op = action.get("op")
        prefs = self.shell.preferences
        if op == "toggle":
            if self.open:
                await self.close()
            else:
                await self.show(self.tab)
        elif op == "close":
            await self.close()
        elif op == "tab":
            tab = action.get("tab")
            if tab not in {"Projects", "Sessions"}:
                raise ValueError("Unknown sidebar tab")
            prefs.set("sidebar_tab", tab)
            await self.refresh(force=True)
        elif op == "width":
            width = action.get("width")
            if isinstance(width, int) and not isinstance(width, bool):
                prefs.set("sidebar_width", view.clamp_width(width))
        elif op == "dir":
            await self.navigate(action.get("path"))
        elif op == "file":
            await self.preview(action.get("path"))
        elif op == "refresh":
            await self.refresh(force=True)
        elif op in {"browse_project", "project_path"}:
            if not self.projects.get("browser_open"):
                raise ValueError("The project picker is not open")
            path = action.get("path")
            if not isinstance(path, str) or not path:
                raise ValueError("A project path is required")
            await self.browse_project(path)
        elif op == "select_project":
            path = str(action.get("path", ""))
            if self.projects.get("browser_open") and (path == self._project_path or any(
                row["path"] == path and row["kind"] == "dir"
                for row in self.projects.get("browser_rows", [])
            )):
                await self.shell.switch_project(path, create=True)
                self._remember_project(path)
                await self.close()
        elif op == "cancel_project":
            self.projects["browser_open"] = False
            self._browser_sequence += 1
        elif op == "add_project":
            self.projects["browser_open"] = True
            await self.browse_project(self._project_path or str(self.shell.workspace))
        elif op == "open_project":
            workspace = action.get("workspace")
            if workspace not in {row.get("workspace") for row in self.projects.get("rows", [])}:
                raise ValueError("That project is no longer listed")
            await self.shell.switch_project(workspace, create=True)
            self._remember_project(workspace)
            await self.close()
        elif op == "open_session":
            await self.open_session(action.get("workspace"), action.get("session"))
        else:
            raise ValueError("Unknown sidebar action")

    def _remember_project(self, path):
        recent = self.shell.preferences.values.get("project_recent", [])
        # Do not seed persistent recents when launching from a generated worktree.
        paths = [path, *[p for p in recent if p != path]]
        self.shell.preferences.set("project_recent", [p for p in paths if not _temporary_project(p)][:100])

    # -- projection --------------------------------------------------------------------

    def snapshot(self) -> dict:
        """The ``sidebar`` snapshot block. ``available`` tells the client this host owns the sidebar."""
        if not self.enabled:
            return {"available": False, "open": False}
        self._sync_workspace()
        dashboard = getattr(self.shell.workflows, "dashboard", None)
        sessions = None
        if dashboard is not None and dashboard.visible:
            sessions = view.sessions_payload(dashboard.snapshot(), self._manager_session())
        # ``projects`` is updated in place (picker open/browse); the wire diff keeps the previous
        # snapshot by reference, so a shared dict would compare equal to itself and never be sent.
        return {"available": True, "open": self.open, "tab": self.tab, "width": self.width, "focus": self.focus,
                "reveal": self.reveal, "sessions": sessions, "projects": copy.deepcopy(self.projects),
                "files": self.files, "git": self.git}
