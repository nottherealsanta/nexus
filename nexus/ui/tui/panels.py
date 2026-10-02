"""Side panels, Sessions dialog, and Settings wiring for the Textual shell.

A mixin over :class:`NexusTextualApp`: every panel reads the controller's
reduced view or a host command (``SessionList``, ``SessionDelete``/``Restore``,
``Doctor``); nothing here touches the runtime or session files.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from pathlib import Path
from typing import Any

from textual.containers import Horizontal

from ...client.protocol import ClientError
from ...ui_support.text import sanitize
from ...ui_support.tui_archived import ArchivedSessionsScreen
from ...ui_support.tui_panels import (
    session_status,
    DetailsSidebar,
    SessionSidebar,
    SessionsScreen,
    SessionTabs,
    SettingsScreen,
    TopBar,
    breadcrumb,
)
from ...ui_support.tui_settings import SettingsConsole
from .agent_picker import _display_name
from .theme import NEXUS_THEMES
from .widgets import ContextPreview


class MainLayout(Horizontal):
    """The full-width row; it relays its own resizes because ``App._on_resize``
    stops the event before an app-level ``on_resize`` handler would run."""

    def on_resize(self, _event: object) -> None:
        self.app._sync_layout()  # type: ignore[attr-defined]


class PanelsMixin:
    """Behavior for the sidebars and the Sessions/Settings dialogs."""

    SHORTCUT_TABLE: tuple[tuple[str, str | None, str], ...] = ()
    _health: dict[str, Any] | None
    _health_error: str | None
    _last_archive: tuple[str, str] | None

    def on_click(self, event) -> None:
        # A click on a dialog's backdrop (the screen itself, not a child)
        # closes it exactly as Escape does, whatever that screen binds it to.
        if len(self.screen_stack) > 1 and event.widget is self.screen:
            event.stop()
            self.simulate_key("escape")

    def _preview_visible(self) -> bool:
        """The scrollable context header now owns the request preview."""
        return False

    def _sync_layout(self) -> None:
        self._sync_panels()
        self._sync_logs_layout()
        self._sync_main_width()

    def _sync_panels(self) -> None:
        if not self.is_mounted:
            return
        width = self.size.width
        details = bool(self.prefs["details_sidebar"]) and width >= 120 and not self._logs_open
        self.query_one(DetailsSidebar).display = details
        docked = self._sessions_docked_fits()
        sidebar = self.query_one(SessionSidebar)
        sidebar.display = bool(self.prefs["sessions_sidebar"]) if docked else self._sessions_overlay
        sidebar.set_class(not docked, "-overlay")
        self.query_one("#context-header").display = bool(self.prefs["context_preview"])
        self.query_one(SessionTabs).set_toggles(sessions=sidebar.display, details=details)

    #: Narrow widths show the sessions sidebar as a transient overlay instead.
    _sessions_overlay = False

    def _sessions_docked_fits(self) -> bool:
        details = bool(self.prefs["details_sidebar"]) and self.size.width >= 120 and not self._logs_open
        return self.size.width >= (170 if details else 110)

    def _sync_topbar(self) -> None:
        if not self.is_mounted:
            return
        sidebar = self.query_one(SessionSidebar)
        session = self.controller.session
        summary = sidebar.current_summary(session)
        running = self.controller.view.phase == "running"
        status = sidebar.current_status() if summary is not None and session == sidebar._current else "idle"
        status = "working" if running else status
        health = self._health or {}
        bar = self.query_one(TopBar)
        bar.set_location(breadcrumb(str(health.get("workspace") or Path.cwd()), health.get("git")))
        bar.set_status(status)
        self._sync_tabs(status)

    #: Sessions open as tabs, in the order they were opened (see ``_sync_tabs``).
    _tabs: list[str]
    #: Tabs the user closed, with the status they had then; a closed session
    #: comes back on its own only when its status changes (e.g. it needs input).
    _closed_tabs: dict[str, str]

    def _sync_tabs(self, current_status: str | None = None) -> None:
        """Tabs show every active session: the current one, any you opened, and
        any that is working, waiting for input, or finished unseen."""
        if not hasattr(self, "_tabs"):
            self._tabs, self._closed_tabs = [], {}
        sidebar = self.query_one(SessionSidebar)
        current = self.controller.session
        statuses: dict[str, str] = {}
        titles: dict[str, str] = {}
        for summary in sidebar._summaries:
            titles[summary.id] = summary.title
            statuses[summary.id] = session_status(summary, sidebar.seen, current)
        if current_status is not None:
            statuses[current] = current_status
        for session, status in statuses.items():
            closed = self._closed_tabs.get(session)
            if closed is not None and closed != status and status != "idle":
                del self._closed_tabs[session]
            if status in {"working", "input", "done"} and session not in self._tabs and session not in self._closed_tabs:
                self._tabs.append(session)
        self._closed_tabs.pop(current, None)
        if current not in self._tabs:
            self._tabs.append(current)
        known = set(titles) | {current}
        self._tabs = [session for session in self._tabs if session in known][:50]
        self.query_one(SessionTabs).set_tabs(
            [(session, titles.get(session, ""), statuses.get(session, "idle")) for session in self._tabs], current
        )

    async def on_session_tabs_open_requested(self, message: SessionTabs.OpenRequested) -> None:
        if message.session != self.controller.session:
            await self._switch_session(message.session)
            await self._poll_sessions()

    async def on_session_tabs_close_requested(self, message: SessionTabs.CloseRequested) -> None:
        """Closing a tab only hides it; the session keeps running on the daemon."""
        if not hasattr(self, "_tabs"):
            self._tabs, self._closed_tabs = [], {}
        session = message.session
        sidebar = self.query_one(SessionSidebar)
        summary = sidebar.current_summary(session)
        self._closed_tabs[session] = session_status(summary, sidebar.seen, self.controller.session) if summary else "idle"
        index = self._tabs.index(session) if session in self._tabs else 0
        self._tabs = [tab for tab in self._tabs if tab != session]
        if session == self.controller.session:
            following = self._tabs[min(index, len(self._tabs) - 1)] if self._tabs else f"session-{uuid.uuid4().hex[:8]}"
            await self._switch_session(following)
            self._closed_tabs[session] = self._closed_tabs.get(session, "idle")
        await self._poll_sessions()
        self._sync_topbar()

    def _close_sessions_overlay(self) -> None:
        if self._sessions_overlay:
            self._sessions_overlay = False
            self._sync_panels()
            self.query_one("#chat-editor").focus()

    def _sync_details(self) -> None:
        details = self.query_one(DetailsSidebar)
        if not details.display:
            return
        c = self.controller
        phase = "working" if c.view.active_turn is not None else c.view.phase
        model = "/".join(part for part in (c.provider, c.model) if part)
        details.set_view(c.view, phase=phase, agent=_display_name(c.agent_name), model=model,
                         effort=c.reasoning_effort)

    async def _poll_sessions(self) -> None:
        try:
            result_method = getattr(self.controller.client, "session_list_result", None)
            if callable(result_method):
                result = await result_method()
                summaries = list(result.sessions)
                archived_count = result.archived_count
            else:
                summaries = await self.controller.client.list_sessions()
                archived_count = 0
            archived = getattr(self, "_sidebar_archived_rows", [])
            refresh_at = getattr(self, "_sidebar_archive_refresh_at", 0.0)
            if archived_count == 0:
                archived = []
            elif len(archived) != min(archived_count, 10_000) or time.monotonic() >= refresh_at:
                fresh = []
                cursor = 0
                try:
                    while len(fresh) < min(archived_count, 10_000):
                        page = await self.controller.client.list_archived_sessions(limit=200, cursor=cursor)
                        fresh.extend(page.sessions)
                        cursor += len(page.sessions)
                        if not page.has_more or not page.sessions:
                            break
                    archived = fresh
                    self._sidebar_archive_refresh_at = time.monotonic() + 30
                except ClientError:
                    pass
            self._sidebar_archived_rows = archived
        except Exception:  # noqa: BLE001 - side panels never break the shell
            return
        if self.is_mounted:
            sidebar = self.query_one(SessionSidebar)
            try:
                projects = await self.controller.client.project_sessions()
                workspace = projects.workspace
                sidebar.set_projects(projects.sessions, workspace, truncated=projects.truncated)
            except Exception:  # older test transports may not expose the project index
                pass
            sidebar.set_sessions(summaries, self.controller.session, archived)
            sidebar.set_archived_count(archived_count)
            self._sync_topbar()

    async def _poll_health(self) -> None:
        try:
            result = await self.controller.client.doctor()
            self._health, self._health_error = dict(getattr(result, "report", {}) or {}), None
        except Exception as exc:  # noqa: BLE001 - health is advisory
            self._health, self._health_error = None, str(exc) or type(exc).__name__
        try:
            update = await self.controller.client.update_status()
            self._update_notice = (
                f"{sanitize(str(update.available))} available: {sanitize(str(update.command))}"
                if update.available
                else ""
            )
        except Exception:  # noqa: BLE001 - the notice is advisory
            self._update_notice = ""
        if not self.is_mounted:
            return
        self._sync_status()
        self._sync_topbar()
        self.query_one(DetailsSidebar).set_mcp(self._health, error=self._health_error)
        if isinstance(self.screen, SettingsScreen):
            self.screen.set_health(self._health, error=self._health_error)

    def action_toggle_sessions(self) -> None:
        if self._sessions_docked_fits():
            self.prefs.set("sessions_sidebar", not self.prefs["sessions_sidebar"])
        else:
            self._sessions_overlay = not self._sessions_overlay
        self._sync_panels()
        if self._sessions_overlay and not self._sessions_docked_fits():
            self.query_one("#session-filter").focus()
        elif not self.query_one(SessionSidebar).display:
            self.query_one("#chat-editor").focus()

    def on_top_bar_sidebar_toggled(self, _: TopBar.SidebarToggled) -> None:
        self.action_toggle_sessions()

    def on_top_bar_details_toggled(self, _: TopBar.DetailsToggled) -> None:
        self.action_toggle_details()

    async def on_top_bar_new_requested(self, _: TopBar.NewRequested) -> None:
        await self.on_session_sidebar_new_requested(None)

    def on_session_sidebar_dismissed(self, _: SessionSidebar.Dismissed) -> None:
        self._close_sessions_overlay()

    def action_toggle_details(self) -> None:
        self.prefs.set("details_sidebar", not self.prefs["details_sidebar"])
        self._sync_panels()
        self._sync_details()

    def action_open_settings(self, category: str = "appearance") -> None:
        if not self._is_main_screen():
            return
        shortcuts = [(key.replace("ctrl+", "ctrl ").replace("shift+", "shift "), text) for key, _, text in self.SHORTCUT_TABLE]
        workspace = (self._health or {}).get("workspace") or str(Path.cwd())
        themes = [(theme.name, "Dark" if theme.dark else "Light") for theme in NEXUS_THEMES]
        client = self.controller.client
        self.push_screen(SettingsConsole(
            self.prefs, themes, shortcuts, str(workspace),
            inventory=client.settings_inventory,
            read=client.settings_read,
            write=client.settings_write,
            delete=client.settings_delete,
            reset=client.settings_reset,
            category=category,
            list_agents=client.list_agents,
            default_agent=client.default_agent,
            set_default_agent=client.set_default_agent,
            list_models=lambda: client.list_models(selectable_only=True),
            providers=client,
        ))
        self.call_after_refresh(lambda: isinstance(self.screen, SettingsScreen)
                                and self.screen.set_health(self._health, error=self._health_error))

    def _default_agent_changed(self) -> None:
        """A session without its own agent selection now runs the new default."""
        async def refresh() -> None:
            with contextlib.suppress(ClientError):
                await self.controller.refresh_agent_metadata()
            self._sync_agent()
            self._start_context_preview()
        self.run_worker(refresh(), group="default-agent", exclusive=True)

    def on_settings_screen_changed(self, message: SettingsScreen.Changed) -> None:
        self.prefs.set(message.key, message.value)
        if message.key == "theme":
            self.theme = message.value
        self._sync_panels()
        self._sync_details()
        self.query_one("#context-preview", ContextPreview).display = self._preview_visible()

    async def on_session_sidebar_open_requested(self, message: SessionSidebar.OpenRequested) -> None:
        self._close_sessions_overlay()
        project = self.query_one(SessionSidebar).project_rows.get(message.session)
        if project is not None:
            try:
                from ..cli.uds import open_client
                result = await self.controller.client.open_project_session(project.workspace, project.session.id)
                client = await open_client(project.workspace, socket_path=result.socket_path)
                await self.voice.cancel()
                await self.controller.switch_session(project.session.id)
                previous = self.controller.replace_client(client)
                await previous.aclose()
                self._reconnect_factory = lambda: open_client(project.workspace, socket_path=result.socket_path)
                self._tabs, self._closed_tabs = [], {}
                self._sidebar_archived_rows = []
                self._health = None
                await self._switch_session(project.session.id)
                await self._poll_health()
                await self._poll_sessions()
            except (ClientError, OSError) as exc:
                self.notify(f"Could not open project · {sanitize(str(exc), 160)}", severity="error")
            return
        if message.session != self.controller.session:
            if message.session in {row.id for row in getattr(self, "_sidebar_archived_rows", ())}:
                try:
                    await self.controller.client.unarchive_session(message.session)
                except ClientError as exc:
                    self.notify(f"Could not resume · {sanitize(str(exc), 160)}", severity="error")
                    return
            await self._switch_session(message.session)
            await self._poll_sessions()

    async def on_session_sidebar_new_requested(self, _: SessionSidebar.NewRequested | None) -> None:
        self._close_sessions_overlay()
        await self.action_new_session()
        await self._poll_sessions()

    async def on_session_sidebar_delete_requested(self, message: SessionSidebar.DeleteRequested) -> None:
        if message.session not in self.query_one(SessionSidebar).project_rows:
            await self._delete_session(message.session)

    async def _delete_session(self, session: str) -> bool:
        try:
            trash_id, _ = await self.controller.client.delete(
                session, force=True, reason="Deleted from Textual sidebar"
            )
        except ClientError as exc:
            self.notify(f"Could not delete · {sanitize(str(exc), 160)}", severity="error")
            return False
        self._last_delete = trash_id
        self._last_archive = None
        self.notify(f"Deleted {sanitize(session, 60)} · ctrl+z to undo", timeout=6)
        if session == self.controller.session:
            remaining = [row.id for row in await self.controller.client.list_sessions()]
            await self._switch_session(remaining[0] if remaining else f"session-{uuid.uuid4().hex[:8]}")
        await self._poll_sessions()
        return True

    async def _archive_session(self, session: str) -> bool:
        """Archive a session durably; refuse while it has work."""
        try:
            await self.controller.client.archive_session(session)
        except ClientError as exc:
            self.notify(f"Could not archive · {sanitize(str(exc), 160)}", severity="error")
            return False
        self._last_archive = (session, "")
        self._last_delete = None
        self.notify(f"Archived {sanitize(session, 60)} · ctrl+z to undo", timeout=6)
        if session == self.controller.session:
            remaining = [row.id for row in await self.controller.client.list_sessions()]
            await self._switch_session(remaining[0] if remaining else f"session-{uuid.uuid4().hex[:8]}")
        await self._poll_sessions()
        return True

    def _open_sessions_dialog(self) -> None:
        if not self._is_main_screen():
            return

        async def picked(session: str | None) -> None:
            if session == "":
                await self.action_new_session()
            elif session and session != self.controller.session:
                if session in dialog.project_rows:
                    self.query_one(SessionSidebar).project_rows.update(dialog.project_rows)
                    await self.on_session_sidebar_open_requested(SessionSidebar.OpenRequested(session))
                else:
                    await self._switch_session(session)
            await self._poll_sessions()

        dialog = SessionsScreen(
                load=self.controller.client.list_sessions,
                archive=self._archive_session,
                restore=self.action_restore_archived,
                current=self.controller.session,
                seen=self.query_one(SessionSidebar).seen,
                load_projects=self.controller.client.project_sessions,
            )
        self.push_screen(dialog, callback=lambda session: self.run_worker(picked(session), group="sessions-pick"))

    async def on_session_sidebar_restore_requested(self, _: SessionSidebar.RestoreRequested) -> None:
        await self.action_restore_archived()

    async def action_restore_archived(self) -> None:
        trash_id = getattr(self, "_last_delete", None)
        if trash_id:
            self._last_delete = None
            try:
                await self.controller.client.restore(trash_id)
            except ClientError as exc:
                self.notify(f"Could not restore · {sanitize(str(exc), 160)}", severity="error")
            await self._poll_sessions()
            return
        if self._last_archive is None:
            return
        session, _ = self._last_archive
        self._last_archive = None
        try:
            await self.controller.client.unarchive_session(session)
            self.notify(f"Restored {sanitize(session, 60)}")
        except ClientError as exc:
            self.notify(f"Could not restore · {sanitize(str(exc), 160)}", severity="error")
        await self._poll_sessions()

    def on_session_sidebar_archived_requested(self, _: SessionSidebar.ArchivedRequested) -> None:
        self._open_archived_dialog()

    def _open_archived_dialog(self) -> None:
        if not self._is_main_screen():
            return

        async def picked(session: str | None) -> None:
            if session:
                try:
                    await self.controller.client.unarchive_session(session)
                    await self._switch_session(session)
                except ClientError as exc:
                    self.notify(f"Could not resume · {sanitize(str(exc), 160)}", severity="error")
            await self._poll_sessions()

        client = self.controller.client
        self.push_screen(
            ArchivedSessionsScreen(
                load=client.list_archived_sessions,
                preview=client.preview_session,
                search=client.search_sessions,
                current=self.controller.session,
            ),
            callback=lambda session: self.run_worker(picked(session), group="archived-pick"),
        )

    def on_details_sidebar_file_toggled(self, message: DetailsSidebar.FileToggled) -> None:
        self.query_one(DetailsSidebar).toggle_file(message.path)
        self._sync_details()

    def on_details_sidebar_mcp_refresh_requested(self, _: DetailsSidebar.McpRefreshRequested) -> None:
        self.query_one(DetailsSidebar).set_mcp(None)
        self.run_worker(self._poll_health(), group="health", exclusive=True)
