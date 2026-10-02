"""Side panels and the Settings screen for the Textual shell.

The session sidebar lists daemon sessions with a live status (working, needs
input, done) and an archive action. The details sidebar summarizes the reduced
``ConversationView``: session facts, files modified by tools, and MCP health
from the redacted ``Doctor`` report. Everything here renders host-provided data
only; the app owns every host call.
"""

from __future__ import annotations

from .session_groups import _day_label as _day_label, _session_groups

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.content import Content
from textual.markup import escape
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    ContentSwitcher,
    Input,
    OptionList,
    RadioButton,
    RadioSet,
    Static,
    Switch,
)
from textual.widgets.option_list import Option

from .text import sanitize
from .session_status import SESSION_WORDS as SESSION_WORDS, relative_time as relative_time, session_status as session_status, session_subline as session_subline
from .details import FileChange as FileChange, _int as _int, modified_files as modified_files, session_rows

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_EDIT_TOOLS = frozenset({"edit", "multiedit", "write", "apply_patch", "patch"})
STATUS_LABELS = {"working": "Working", "input": "Needs input", "done": "Done", "idle": "", "archived": "Archived"}


# ---------------------------------------------------------------- pure helpers

class TuiPreferences:
    """Small JSON-backed shell preferences; ``path=None`` keeps them in memory."""

    DEFAULTS: ClassVar[dict[str, Any]] = {
        "theme": "nexus-dark",
        "sessions_sidebar": True,
        "details_sidebar": True,
        "context_preview": True,
        "model_favorites": [],
        "model_recent": [],
    }

    MODEL_FAVORITES_LIMIT: ClassVar[int] = 100
    MODEL_FAVORITE_MAX_LENGTH: ClassVar[int] = 256

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.values = dict(self.DEFAULTS)
        self.values["model_favorites"] = []
        self.values["model_recent"] = []
        if path is not None:
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                stored = {}
            if isinstance(stored, dict):
                for key, default in self.DEFAULTS.items():
                    if key in {"model_favorites", "model_recent"}:
                        favorites = self._validated_model_favorites(stored.get(key))
                        if favorites is not None:
                            self.values[key] = favorites
                    elif isinstance(stored.get(key), type(default)):
                        self.values[key] = stored[key]

    @classmethod
    def _validated_model_favorites(cls, value: Any) -> list[str] | None:
        """Copy, validate, deduplicate, and bound globally saved model refs."""
        if not isinstance(value, list):
            return None
        favorites: list[str] = []
        seen: set[str] = set()
        for ref in value:
            if not isinstance(ref, str) or not ref.strip() or len(ref) > cls.MODEL_FAVORITE_MAX_LENGTH:
                continue
            if ref in seen:
                continue
            seen.add(ref)
            favorites.append(ref)
            if len(favorites) == cls.MODEL_FAVORITES_LIMIT:
                break
        return favorites

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def set(self, key: str, value: Any) -> None:
        if key in {"model_favorites", "model_recent"}:
            favorites = self._validated_model_favorites(value)
            if favorites is None:
                return
            value = favorites
        elif key not in self.DEFAULTS or not isinstance(value, type(self.DEFAULTS[key])):
            return
        self.values[key] = value
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.values, indent=2), encoding="utf-8")
        except OSError:
            pass  # preferences are a convenience; never fail the shell


# ---------------------------------------------------------------- top bar

TAB_TITLE_LIMIT = 36
_TAB_GLYPHS = {"working": "", "input": "●", "done": "✓", "idle": "·"}


def tab_title(title: str) -> str:
    """A tab label: the session title's first 36 characters, ellipsis announced."""
    text = sanitize(title or "New Session", 200)
    return text if len(text) <= TAB_TITLE_LIMIT else text[:TAB_TITLE_LIMIT - 1].rstrip() + "…"


def breadcrumb(workspace: str, git: Mapping[str, Any] | None, home: str | None = None) -> str:
    """``~/repos/nexus › ⎇ main`` (plus ``› worktree name`` in a linked worktree)."""
    path = str(workspace or "")
    home = home if home is not None else str(Path.home())
    if home and (path == home or path.startswith(home.rstrip("/") + "/")):
        path = "~" + path[len(home.rstrip("/")):]
    parts = [sanitize(path, 120)] if path else []
    git = git or {}
    if git.get("worktree") and git.get("worktree_name"):
        parts.append(f"worktree {sanitize(str(git['worktree_name']), 60)}")
    if git.get("branch"):
        parts.append(("◇ " if git.get("detached") else "⎇ ") + sanitize(str(git["branch"]), 80))
    return "  ›  ".join(parts)


class SessionTab(Horizontal):
    """One open session: status glyph, title (first 36 chars) and a close ×."""

    def __init__(self, session_id: str) -> None:
        super().__init__(classes="session-tab")
        self.session_id = session_id
        self.status = "idle"
        self.title_text = ""
        self.active = False
        self._frame = 0
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Static("·", classes="tab-glyph", markup=False)
        yield Static("", classes="tab-title", markup=False)
        close = Static("×", classes="tab-close", markup=False)
        close.tooltip = "Close tab · the session keeps running"
        yield close

    def update_tab(self, title: str, status: str, *, active: bool) -> None:
        self.title_text, self.status, self.active = title, status, active
        if self.is_mounted:
            self._apply()

    def on_mount(self) -> None:
        self._apply()

    def _apply(self) -> None:
        status = self.status
        if status == "working" and self._timer is None:
            self._timer = self.set_interval(0.12, self._spin)
        elif status != "working" and self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.query_one(".tab-title", Static).update(tab_title(self.title_text))
        self.tooltip = f"{sanitize(self.title_text or 'New Session', 200)}\n{self.session_id}"
        for name in ("working", "input", "done", "idle"):
            self.set_class(status == name, f"-{name}")
        self.set_class(self.active, "-active")
        self._paint()

    def _spin(self) -> None:
        self._frame = (self._frame + 1) % len(_SPINNER)
        self._paint()

    def _paint(self) -> None:
        glyph = _SPINNER[self._frame] if self.status == "working" else _TAB_GLYPHS.get(self.status, "·")
        self.query_one(".tab-glyph", Static).update(glyph)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def on_click(self, event) -> None:
        event.stop()
        close = self.query_one(".tab-close", Static)
        target = SessionTabs.CloseRequested if event.widget is close else SessionTabs.OpenRequested
        self.post_message(target(self.session_id))


class SessionTabs(Horizontal):
    """The first top-bar row: sessions sidebar toggle, one tab per open session
    (status glyph, title, ×), new session, details toggle.

    Which sessions are open is the app's decision (``set_tabs``); this widget
    only renders and posts messages.
    """

    class OpenRequested(Message):
        def __init__(self, session: str) -> None:
            super().__init__()
            self.session = session

    class CloseRequested(Message):
        def __init__(self, session: str) -> None:
            super().__init__()
            self.session = session

    _TARGETS: ClassVar[dict[str, str]] = {
        "topbar-sidebar-toggle": "SidebarToggled",
        "topbar-details-toggle": "DetailsToggled",
        "topbar-new": "NewRequested",
    }

    def compose(self) -> ComposeResult:
        toggle = Static("▌", id="topbar-sidebar-toggle", classes="topbar-button", markup=False)
        toggle.tooltip = "Toggle sessions sidebar · ctrl+b"
        yield toggle
        yield HorizontalScroll(id="session-tab-list")
        new = Static("+", id="topbar-new", classes="topbar-button", markup=False)
        new.tooltip = "New session · ctrl+n"
        yield new
        details = Static("▐", id="topbar-details-toggle", classes="topbar-button", markup=False)
        details.tooltip = "Toggle details sidebar · ctrl+l"
        yield details

    def set_tabs(self, tabs: list[tuple[str, str, str]], current: str) -> None:
        """``tabs`` is ``(session id, title, status)`` in display order."""
        strip = self.query_one("#session-tab-list", HorizontalScroll)
        existing = {tab.session_id: tab for tab in strip.query(SessionTab)}
        wanted = [session for session, _, _ in tabs]
        for session, tab in existing.items():
            if session not in wanted:
                tab.remove()
        order = [existing.get(session) or SessionTab(session) for session in wanted]
        fresh = [tab for tab in order if tab.session_id not in existing]
        if fresh:
            strip.mount_all(fresh)

        for (session, title, status), tab in zip(tabs, order, strict=True):
            tab.update_tab(title, status, active=session == current)

        def arrange() -> None:
            # Re-read the strip: a later ``set_tabs`` may have replaced tabs since.
            mounted = {tab.session_id: tab for tab in strip.children if isinstance(tab, SessionTab)}
            ordered = [mounted[session] for session in wanted if session in mounted]
            if list(mounted.values()) != ordered:
                for index, tab in enumerate(ordered):
                    strip.move_child(tab, before=index)
            if current in mounted:
                strip.scroll_to_widget(mounted[current], animate=False)

        self.call_after_refresh(arrange)

    def set_toggles(self, *, sessions: bool, details: bool) -> None:
        self.query_one("#topbar-sidebar-toggle").set_class(sessions, "-on")
        self.query_one("#topbar-details-toggle").set_class(details, "-on")

    def on_click(self, event) -> None:
        name = self._TARGETS.get(getattr(event.widget, "id", None) or "")
        if name:
            event.stop()
            self.post_message(getattr(TopBar, name)())


class TopBar(Horizontal):
    """The second top-bar row: a breadcrumb of where the session works
    (directory › worktree › branch) on the left, its status in words on the
    right. The status glyph lives on the session's tab above.

    Pure presentation; it posts messages and the app owns every action.
    """

    class SidebarToggled(Message):
        pass

    class DetailsToggled(Message):
        pass

    class NewRequested(Message):
        pass

    def __init__(self, *, details_toggle: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self._details_toggle = details_toggle

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar-crumb", markup=False)
        yield Static("", id="topbar-status", markup=False)
        if self._details_toggle:
            details = Static("▐", id="topbar-details-toggle", classes="topbar-button", markup=False)
            details.tooltip = "Toggle details sidebar · ctrl+l"
            yield details

    def set_location(self, text: str) -> None:
        self.query_one("#topbar-crumb", Static).update(text)

    def set_session(self, title: str, status: str) -> None:
        """Sub-agent pages: the title stands in for the breadcrumb."""
        self.set_location(sanitize(title or "New Session", 120))
        self.set_status(status)

    def set_status(self, status: str) -> None:
        self.query_one("#topbar-status", Static).update(STATUS_LABELS.get(status) or "Idle")
        for name in ("working", "input", "done"):
            self.set_class(status == name, f"-{name}")

    def set_toggles(self, *, sessions: bool, details: bool) -> None:
        for toggle in self.query("#topbar-details-toggle"):
            toggle.set_class(details, "-on")

    def on_click(self, event) -> None:
        if getattr(event.widget, "id", None) == "topbar-details-toggle":
            event.stop()
            self.post_message(self.DetailsToggled())


# ---------------------------------------------------------------- session sidebar

class SessionRow(Horizontal):
    """One session as a two-line card: status glyph and title, then the status
    in words (or message count) and age; the current session has a left bar."""

    can_focus = True

    def __init__(self, session_id: str) -> None:
        super().__init__(classes="session-row")
        self.session_id = session_id
        self.status = "idle"
        self._frame = 0
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Static("·", classes="session-glyph", markup=False)
        with Vertical(classes="session-text"):
            yield Static("", classes="session-title", markup=False)
            yield Static("", classes="session-sub", markup=False)
        yield Static("×", classes="session-delete", markup=False)

    def update_row(self, summary: Any, status: str, *, active: bool) -> None:
        # The title lives in a nested container that composes after this row
        # mounts; keep the latest values and apply them once it exists.
        self._pending = (summary, status, active)
        self.status = status
        self.set_class(active, "-active")
        if self.query(".session-title"):
            self._apply_row()

    def on_mount(self) -> None:
        self.call_after_refresh(self._apply_row)

    def _apply_row(self) -> None:
        pending = getattr(self, "_pending", None)
        if pending is None or not self.query(".session-title"):
            return
        summary, status, active = pending
        self.status = status
        if status == "working" and self._timer is None:
            self._timer = self.set_interval(0.12, self._spin)
        elif status != "working" and self._timer is not None:
            self._timer.stop()
            self._timer = None
        title = sanitize(summary.title or "Untitled session", 80)
        self.query_one(".session-title", Static).update(title)
        self.query_one(".session-sub", Static).update(session_subline(summary, status))
        project = getattr(self.parent.parent, "project_rows", {}).get(self.session_id) if self.parent else None
        self.tooltip = f"{title}\n{project.workspace if project else summary.id}"
        self.query_one(".session-delete", Static).display = project is None
        for name in ("working", "input", "done", "idle", "archived"):
            self.set_class(status == name, f"-{name}")
        self.set_class(active, "-active")
        self._paint_glyph()

    def _spin(self) -> None:
        if self.status == "working":
            self._frame = (self._frame + 1) % len(_SPINNER)
            self._paint_glyph()

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _paint_glyph(self) -> None:
        glyph = {"working": _SPINNER[self._frame], "input": "●", "done": "✓", "archived": "◇"}.get(self.status, "·")
        self.query_one(".session-glyph", Static).update(glyph)

    def on_click(self, event) -> None:
        event.stop()
        delete = self.query_one(".session-delete", Static)
        target = SessionSidebar.DeleteRequested if event.widget is delete else SessionSidebar.OpenRequested
        self.post_message(target(self.session_id))

    def on_key(self, event) -> None:
        if event.key in {"enter", "space"}:
            self.post_message(SessionSidebar.OpenRequested(self.session_id))
        elif event.key in {"delete", "backspace"}:
            self.post_message(SessionSidebar.DeleteRequested(self.session_id))
        elif event.key == "ctrl+z":
            self.post_message(SessionSidebar.RestoreRequested())
        elif event.key in {"down", "j", "up", "k"}:
            rows = list(self.parent.query(SessionRow)) if self.parent else []
            index = rows.index(self) + (1 if event.key in {"down", "j"} else -1)
            if 0 <= index < len(rows):
                rows[index].focus()
        else:
            return
        event.stop()
        event.prevent_default()


class SessionSidebar(Vertical):
    """Every workspace session, grouped by day like ``/sessions``, with live
    status, filter, new, delete, and an ``Archived`` group."""

    class OpenRequested(Message):
        def __init__(self, session: str) -> None:
            super().__init__()
            self.session = session

    class DeleteRequested(Message):
        def __init__(self, session: str) -> None:
            super().__init__()
            self.session = session

    class NewRequested(Message):
        pass

    class RestoreRequested(Message):
        pass

    class ArchivedRequested(Message):
        pass

    class Dismissed(Message):
        pass

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._summaries: list[Any] = []
        self._archived: list[Any] = []
        self._current = ""
        self.project_rows: dict[str, Any] = {}
        self.workspace = ""
        self.projects_truncated = False
        self._current_running = False
        self._layout: tuple[tuple[str, tuple[str, ...]], ...] | None = None
        #: last_seq observed per session; a later seq on an idle session is "done".
        self.seen: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        yield Static("+ New session      ctrl+n", id="new-session", markup=False)
        yield Input(placeholder="Filter sessions", id="session-filter")
        yield Static("SESSIONS", id="sessions-heading", markup=False)
        yield VerticalScroll(id="session-list")
        archived = Static("", id="sidebar-archived", markup=False)
        archived.display = False
        yield archived
        yield Static("↵ open · del delete · ctrl+z undo", id="sidebar-hint", markup=False)

    def set_sessions(self, summaries: list[Any], current: str, archived: list[Any] | None = None) -> None:
        self._summaries, self._current = list(summaries), current
        self._archived = list(archived or ())
        for summary in summaries:
            if summary.id not in self.seen or summary.id == current:
                self.seen[summary.id] = max(self.seen.get(summary.id, 0), summary.last_seq)
        self._render_rows()

    def set_projects(self, rows: list[Any], workspace: str, *, truncated: bool = False) -> None:
        self.workspace = workspace
        self.projects_truncated = truncated
        self.project_rows = {}
        for row in rows:
            if row.workspace != workspace:
                key = f"{row.project_id}:{row.session.id}"
                self.project_rows[key] = row
        self._render_rows()

    def current_summary(self, session: str | None = None) -> Any | None:
        session = self._current if session is None else session
        return next((row for row in [*self._summaries, *self._archived] if row.id == session), None)

    def current_status(self) -> str:
        summary = self.current_summary()
        if self._current_running:
            return "working"
        return session_status(summary, self.seen, self._current) if summary is not None else "idle"

    def set_current_running(self, running: bool) -> None:
        self._current_running = running
        summary = self.current_summary()
        widget = next((row for row in self.query(SessionRow) if row.session_id == self._current), None)
        if summary is not None and widget is not None:
            widget.update_row(summary, self.current_status(), active=True)

    def set_archived_count(self, count: int) -> None:
        row = self.query_one("#sidebar-archived", Static)
        row.update(f"Archived · {count}" if count > 0 else "")
        row.display = count > 0

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for summary in self._summaries:
            status = ("working" if summary.id == self._current and self._current_running else
                      session_status(summary, self.seen, self._current))
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _groups(self) -> list[tuple[str, list[Any]]]:
        """Filtered rows grouped exactly as ``SessionsScreen`` groups them."""
        query = self.query_one("#session-filter", Input).value.strip().casefold()
        rows = [(self.workspace, summary) for summary in self._summaries]
        for key, row in self.project_rows.items():
            fields = {field: getattr(row.session, field) for field in row.session.__struct_fields__}
            fields["id"] = key
            rows.append((row.workspace, SimpleNamespace(**fields)))
        groups = _session_groups(rows, query)
        archived = [s for s in self._archived if not query or query in f"{s.title} {s.id}".casefold()]
        if archived:
            groups.append(("Archived", archived))
        return groups

    def _render_rows(self) -> None:
        if not self.is_mounted:
            return
        groups = self._groups()
        wanted = [s.id for _, rows in groups for s in rows]
        layout = tuple((day, tuple(s.id for s in rows)) for day, rows in groups)
        relayout, self._layout = layout != self._layout, layout
        container = self.query_one("#session-list", VerticalScroll)
        if relayout:
            container.query(".session-day, .session-project").remove()
        existing = {row.session_id: row for row in container.query(SessionRow)}
        for session_id, row in existing.items():
            if session_id not in wanted:
                row.remove()
        new_rows = [SessionRow(session_id) for session_id in wanted if session_id not in existing]
        mounted = container.mount_all(new_rows) if new_rows else None
        async def finish() -> None:
            if mounted is not None:
                await mounted
            if layout == self._layout and self.is_mounted:
                self._finish_render(groups, relayout)
        self.call_after_refresh(finish)
        self.query_one("#sessions-heading", Static).update(
            f"SESSIONS  {len(self._summaries) + len(self.project_rows)}" + (" · first 1000" if self.projects_truncated else "")
        )

    def _finish_render(self, groups: list[tuple[str, list[Any]]], relayout: bool = True) -> None:
        container = self.query_one("#session-list", VerticalScroll)
        by_id = {row.session_id: row for row in container.query(SessionRow) if row.is_mounted}
        previous = None
        last_project = None
        for day, rows in groups:
            first = True
            for summary in rows:
                row = by_id.get(summary.id)
                if row is None:
                    continue
                status = ("archived" if day == "Archived" and summary in self._archived else "working"
                          if summary.id == self._current and self._current_running else
                          session_status(summary, self.seen, self._current))
                row.update_row(summary, status, active=summary.id == self._current)
                if not relayout:
                    continue
                if previous is not None:
                    container.move_child(row, after=previous)
                elif container.children and container.children[0] is not row:
                    container.move_child(row, before=0)
                if first:
                    project, separator, date_label = day.rpartition(" · ")
                    if separator and project != last_project:
                        heading = Static(sanitize(project), classes="session-project", markup=False)
                        container.mount(heading, before=row)
                        last_project = project
                    container.mount(Static(date_label if separator else day, classes="session-day", markup=False), before=row)
                    first = False
                previous = row

    @on(Input.Changed, "#session-filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._render_rows()

    @on(Input.Submitted, "#session-filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        first = next(iter(self.query(SessionRow)), None)
        if first is not None:
            first.focus()

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.post_message(self.Dismissed())

    def on_click(self, event) -> None:
        if getattr(event.widget, "id", None) == "new-session":
            event.stop()
            self.post_message(self.NewRequested())
        elif getattr(event.widget, "id", None) == "sidebar-archived":
            event.stop()
            self.post_message(self.ArchivedRequested())


# ---------------------------------------------------------------- details sidebar

class FileRow(Static):
    """A modified file; Enter or click toggles its bounded diff preview."""

    can_focus = True

    def __init__(self, change: FileChange, expanded: bool) -> None:
        super().__init__(classes="file-row")
        self.change = change
        self.expanded = expanded

    def render(self) -> str:
        change = self.change
        kind = "A" if change.created else "M"
        head, _, base = change.path.rpartition("/")
        stats = (
            f"[$nx-success]+{change.added}[/] [$nx-error]-{change.removed}[/]"
            if change.added or change.removed else ("[$nx-success]new[/]" if change.created else "")
        )
        folder = f"[$nx-quiet]{escape(head)}/[/]" if head else ""
        marker = "▾" if self.expanded else "▸"
        color = "$nx-success" if kind == "A" else "$nx-warning"
        return f"[$nx-quiet]{marker}[/] [{color}]{kind}[/] {folder}{escape(base)}  {stats}"

    def on_click(self, event) -> None:
        event.stop()
        self.post_message(DetailsSidebar.FileToggled(self.change.path))

    def on_key(self, event) -> None:
        if event.key in {"enter", "space"}:
            event.stop()
            self.post_message(DetailsSidebar.FileToggled(self.change.path))


def _diff_markup(hunks: Iterable[str], limit: int = 60) -> str:
    lines: list[str] = []
    for hunk in hunks:
        for line in hunk.splitlines():
            if line.startswith(("---", "+++")):
                continue
            text = escape(sanitize(line, 200))
            if line.startswith("+"):
                lines.append(f"[$nx-success]{text}[/]")
            elif line.startswith("-"):
                lines.append(f"[$nx-error]{text}[/]")
            elif line.startswith("@@"):
                lines.append(f"[$nx-quiet]{text}[/]")
            else:
                lines.append(f"[$nx-muted]{text}[/]")
    if len(lines) > limit:
        lines = [*lines[:limit], f"[$nx-quiet]… {len(lines) - limit} more lines[/]"]
    return "\n".join(lines) or "[$nx-quiet]No diff preview reported.[/]"


class DetailsSidebar(VerticalScroll):
    """Session facts, modified files, and MCP server health."""

    class FileToggled(Message):
        def __init__(self, path: str) -> None:
            super().__init__()
            self.path = path

    class McpRefreshRequested(Message):
        pass

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.expanded: set[str] = set()
        self._files_signature: tuple = ()
        self._mcp_markup = "[$nx-quiet]Checking servers…[/]"

    def compose(self) -> ComposeResult:
        yield Static("SESSION", classes="panel-title", markup=False)
        yield Static("", id="details-session")
        yield Static("MODIFIED FILES", id="files-title", classes="panel-title", markup=False)
        yield Vertical(id="details-files")
        yield Horizontal(
            Static("MCP SERVERS", id="mcp-title", classes="panel-title", markup=False),
            Static("↻", id="mcp-refresh", markup=False),
            id="mcp-heading",
        )
        yield Static(self._mcp_markup, id="details-mcp")

    def set_view(self, view: Any, *, phase: str, agent: str, model: str, effort: str | None) -> None:
        if not self.is_mounted:
            return
        rows = session_rows(view, phase=phase, agent=agent, model=model, effort=effort)
        self.query_one("#details-session", Static).update(
            "\n".join(f"[$nx-quiet]{label:<11}[/]{escape(sanitize(value, 60))}" for label, value in rows)
        )
        self._render_files(modified_files(view))

    def _render_files(self, files: list[FileChange]) -> None:
        signature = tuple((f.path, f.added, f.removed, f.created, len(f.hunks)) for f in files)
        signature += (tuple(sorted(self.expanded)),)
        if signature == self._files_signature:
            return
        self._files_signature = signature
        box = self.query_one("#details-files", Vertical)
        box.remove_children()
        title = self.query_one("#files-title", Static)
        title.update(f"MODIFIED FILES  {len(files)}" if files else "MODIFIED FILES")
        if not files:
            box.mount(Static("No files changed yet.", classes="panel-empty", markup=False))
            return
        widgets: list[Static] = []
        for change in files:
            widgets.append(FileRow(change, change.path in self.expanded))
            if change.path in self.expanded:
                widgets.append(Static(_diff_markup(change.hunks), classes="file-diff"))
        added = sum(f.added for f in files)
        removed = sum(f.removed for f in files)
        widgets.append(Static(
            f"+{added} -{removed} across {len(files)} file{'s' if len(files) != 1 else ''}",
            classes="panel-empty", markup=False,
        ))
        box.mount_all(widgets)

    def toggle_file(self, path: str) -> None:
        self.expanded.symmetric_difference_update({path})
        self._files_signature = ()

    def set_mcp(self, report: Mapping[str, Any] | None, *, error: str | None = None) -> None:
        self._mcp_markup = mcp_markup(report, error=error)
        if self.is_mounted:
            self.query_one("#details-mcp", Static).update(self._mcp_markup)

    def on_click(self, event) -> None:
        if getattr(event.widget, "id", None) == "mcp-refresh":
            event.stop()
            self.post_message(self.McpRefreshRequested())


def mcp_markup(report: Mapping[str, Any] | None, *, error: str | None = None) -> str:
    """Render the redacted Doctor MCP section as themed markup."""
    if error:
        return f"[$nx-error]Unavailable: {escape(sanitize(error, 120))}[/]"
    if report is None:
        return "[$nx-quiet]Checking servers…[/]"
    mcp = report.get("mcp")
    if not isinstance(mcp, Mapping):
        return "[$nx-quiet]MCP is disabled for this workspace.[/]"
    servers = [row for row in mcp.get("servers") or () if isinstance(row, Mapping)]
    if not servers:
        return "[$nx-quiet]No servers in .agents/mcp.json[/]"
    lines = []
    for row in servers:
        health = str(row.get("health") or "unknown").casefold()
        enabled = row.get("enabled", True) is not False
        color = {"ready": "$nx-success", "failed": "$nx-error"}.get(health, "$nx-warning")
        if not enabled:
            color, health = "$nx-quiet", "disabled"
        counts = " · ".join(
            f"{row[key]} {label}" for key, label in (("tool_count", "tools"), ("resource_count", "res"))
            if isinstance(row.get(key), int) and row[key]
        )
        name = escape(sanitize(str(row.get("name") or "server"), 40))
        lines.append(f"[{color}]●[/] {name}  [$nx-quiet]{counts or health}[/]")
        if row.get("last_error") and not row.get("connected"):
            lines.append(f"  [$nx-error]{escape(sanitize(str(row['last_error']), 80))}[/]")
    return "\n".join(lines)


# ---------------------------------------------------------------- settings

class SettingsScreen(ModalScreen[None]):
    """Two-pane settings: appearance, layout, keyboard reference, workspace."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "dismiss", "Close")]
    SECTIONS = (("appearance", "Appearance"), ("layout", "Layout"), ("keys", "Keyboard"), ("workspace", "Workspace"))

    class Changed(Message):
        def __init__(self, key: str, value: Any) -> None:
            super().__init__()
            self.key = key
            self.value = value

    def __init__(self, preferences: TuiPreferences, themes: list[tuple[str, str]],
                 shortcuts: Iterable[tuple[str, str]], workspace: str) -> None:
        super().__init__()
        self._prefs = preferences
        self._themes = themes
        self._shortcuts = list(shortcuts)
        self._workspace = workspace

    def compose(self) -> ComposeResult:
        with Horizontal(id="settings-dialog"):
            with Vertical(id="settings-nav"):
                yield Static("Settings", id="settings-title", markup=False)
                yield OptionList(*(label for _, label in self.SECTIONS), id="settings-sections")
                yield Static("esc close", id="settings-hint", markup=False)
            with ContentSwitcher(initial="appearance", id="settings-panes"):
                yield from self.compose_general_panes()

    def compose_general_panes(self) -> ComposeResult:
        """Appearance, Layout, Keyboard and Workspace panes (reused by Settings)."""
        with VerticalScroll(id="appearance"):
            with Horizontal(classes="settings-heading-row"):
                yield Static("Appearance", classes="settings-heading", markup=False)
                yield Button("Reset to default", id="settings-reset-appearance")
            yield Static("Theme for this terminal shell.", classes="settings-help", markup=False)
            with RadioSet(id="settings-theme"):
                for name, label in self._themes:
                    yield RadioButton(label, value=self._prefs["theme"] == name, name=name)
        with VerticalScroll(id="layout"):
            with Horizontal(classes="settings-heading-row"):
                yield Static("Layout", classes="settings-heading", markup=False)
                yield Button("Reset to default", id="settings-reset-layout")
            yield Static("Panels hide automatically on narrow terminals.", classes="settings-help", markup=False)
            for key, label, hint in (
                ("sessions_sidebar", "Sessions sidebar", "ctrl+b"),
                ("details_sidebar", "Details sidebar", "ctrl+l"),
                ("context_preview", "Show context header", ""),
            ):
                with Horizontal(classes="settings-row"):
                    yield Static(label, classes="settings-label", markup=False)
                    yield Static(hint, classes="settings-key", markup=False)
                    yield Switch(value=bool(self._prefs[key]), id=f"pref-{key}")
        with VerticalScroll(id="keys"):
            yield Static("Keyboard", classes="settings-heading", markup=False)
            width = max((len(key) for key, _ in self._shortcuts), default=8)
            yield Static(
                "\n".join(f"[$nx-accent]{escape(key):<{width}}[/]  {escape(text)}" for key, text in self._shortcuts),
                id="settings-keys",
            )
        with VerticalScroll(id="workspace"):
            yield Static("Workspace", classes="settings-heading", markup=False)
            yield Static(escape(sanitize(self._workspace, 200)), classes="settings-help")
            yield Static("[$nx-quiet]Loading workspace health…[/]", id="settings-health")

    def on_mount(self) -> None:
        self.query_one("#settings-sections", OptionList).highlighted = self.initial_section_index()
        self.query_one("#settings-sections", OptionList).focus()

    def initial_section_index(self) -> int:
        """Sidebar row highlighted on open (overridable)."""
        return 0

    def set_health(self, report: Mapping[str, Any] | None, *, error: str | None = None) -> None:
        if not self.is_mounted:
            return
        if error or report is None:
            text = f"[$nx-error]Unavailable: {escape(sanitize(error or 'no report', 120))}[/]"
        else:
            providers = ", ".join(str(row.get("name")) for row in report.get("providers") or () if isinstance(row, Mapping))
            extensions = report.get("extensions") if isinstance(report.get("extensions"), Mapping) else {}
            rows = [("Providers", providers or "none"), ("Sessions", str(report.get("sessions", "?")))]
            if extensions:
                rows.append(("Extensions", f"{extensions.get('loaded', 0)} loaded · gen {extensions.get('generation', 0)}"))
            text = "\n".join(f"[$nx-quiet]{label:<11}[/]{escape(sanitize(value, 80))}" for label, value in rows)
            text += "\n\n[$nx-quiet]MCP SERVERS[/]\n" + mcp_markup(report)
        self.query_one("#settings-health", Static).update(text)

    @on(OptionList.OptionHighlighted, "#settings-sections")
    def _section(self, event: OptionList.OptionHighlighted) -> None:
        self.show_section(event.option_index)

    def show_section(self, index: int) -> None:
        """Switch the right-hand pane to the highlighted section (overridable)."""
        self.query_one("#settings-panes", ContentSwitcher).current = self.SECTIONS[index][0]

    @on(OptionList.OptionSelected, "#settings-sections")
    def _enter_section(self, event: OptionList.OptionSelected) -> None:
        self.enter_section(event.option_index)

    def enter_section(self, index: int) -> None:
        """Move focus into the chosen section (overridable)."""
        pane = self.query_one(f"#{self.SECTIONS[index][0]}")
        target = next(iter(pane.query("RadioSet, Switch")), None)
        (target or pane).focus()

    @on(RadioSet.Changed, "#settings-theme")
    def _theme(self, event: RadioSet.Changed) -> None:
        if event.pressed.name:
            self.post_message(self.Changed("theme", event.pressed.name))

    @on(Switch.Changed)
    def _switch(self, event: Switch.Changed) -> None:
        if event.switch.id and event.switch.id.startswith("pref-"):
            self.post_message(self.Changed(event.switch.id.removeprefix("pref-"), bool(event.value)))


# ---------------------------------------------------------------- sessions dialog



class SessionsScreen(ModalScreen[str | None]):
    """Searchable sessions grouped by day, with status and archive."""

    #: Priority bindings: the focused search Input binds ctrl+d/ctrl+z itself.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss_none", "Close"),
        Binding("ctrl+d", "archive", "Archive", priority=True),
        Binding("ctrl+z", "restore", "Undo archive", priority=True),
        Binding("ctrl+n", "new", "New session", priority=True),
    ]

    def __init__(
        self,
        *,
        load: Callable[[], Awaitable[list[Any]]],
        archive: Callable[[str], Awaitable[bool]],
        restore: Callable[[], Awaitable[None]],
        current: str,
        seen: Mapping[str, int],
        load_projects: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        super().__init__()
        self._load, self._archive, self._restore = load, archive, restore
        self._current, self._seen = current, seen
        self._load_projects = load_projects
        self._workspace = ""
        self.project_rows: dict[str, Any] = {}
        self._summaries: list[Any] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="sessions-dialog"):
            with Horizontal(id="sessions-header"):
                yield Static("Sessions", id="sessions-title", markup=False)
                yield Static("esc", id="sessions-esc", markup=False)
            yield Input(placeholder="Search", id="sessions-search")
            yield OptionList(id="sessions-options")
            yield Static(
                "[b $nx-text]open[/] ↵  [b $nx-text]new[/] ctrl+n  "
                "[b $nx-text]archive[/] ctrl+d  [b $nx-text]undo[/] ctrl+z",
                id="sessions-footer",
            )

    async def on_mount(self) -> None:
        self.query_one("#sessions-search", Input).focus()
        await self.reload()

    async def reload(self) -> None:
        self._summaries = await self._load()
        if self._load_projects is not None:
            try:
                result = await self._load_projects()
                self._workspace = result.workspace
                self.project_rows = {f"{row.project_id}:{row.session.id}": row for row in result.sessions if row.workspace != result.workspace}
            except Exception:
                pass
        self._render_options()

    def _render_options(self) -> None:
        options = self.query_one("#sessions-options", OptionList)
        query = self.query_one("#sessions-search", Input).value.strip().casefold()
        source = [(self._workspace, row) for row in self._summaries]
        for key, row in self.project_rows.items():
            fields = {field: getattr(row.session, field) for field in row.session.__struct_fields__}
            fields["id"] = key
            source.append((row.workspace, SimpleNamespace(**fields)))
        groups = _session_groups(source, query)
        rows = [row for _, group in groups for row in group]
        labels = {row.id: label for label, group in groups for row in group}
        highlighted = options.highlighted_option.id if options.highlighted_option else self._current
        options.clear_options()
        items: list[Option] = []
        last_day = None
        for summary in rows:
            day = labels[summary.id]
            if day != last_day:
                if last_day is not None:
                    items.append(Option(Content(""), disabled=True))
                items.append(Option(Content.from_markup(f"[b $nx-purple]{escape(day)}[/]"), disabled=True))
                last_day = day
            status = session_status(summary, self._seen, self._current)
            glyph = "●" if summary.id == self._current else {"working": "◌", "input": "●", "done": "✓"}.get(status, " ")
            color = {"working": "$nx-accent", "input": "$nx-warning", "done": "$nx-success"}.get(status, "$nx-text")
            title = escape(sanitize(summary.title or "Untitled session", 90))
            label = STATUS_LABELS[status]
            tail = f"  [{color}]{label}[/]" if label else ""
            items.append(Option(Content.from_markup(f"[{color}]{glyph}[/] {title}{tail}"), id=summary.id))
        if not rows:
            items.append(Option(Content.from_markup("[$nx-quiet]No matching sessions[/]"), disabled=True))
        options.add_options(items)
        ids = [item.id for item in items if item.id]
        if ids:
            options.highlighted = options.get_option_index(highlighted if highlighted in ids else ids[0])

    @on(Input.Changed, "#sessions-search")
    def _search(self, event: Input.Changed) -> None:
        event.stop()
        self._render_options()

    def on_key(self, event) -> None:
        options = self.query_one("#sessions-options", OptionList)
        if event.key in {"down", "up"} and self.focused is self.query_one("#sessions-search", Input):
            event.stop()
            options.action_cursor_down() if event.key == "down" else options.action_cursor_up()
        elif event.key == "enter" and options.highlighted_option is not None and not options.has_focus:
            event.stop()
            self.dismiss(options.highlighted_option.id)

    @on(OptionList.OptionSelected, "#sessions-options")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss(event.option.id)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def action_new(self) -> None:
        self.dismiss("")

    async def action_archive(self) -> None:
        option = self.query_one("#sessions-options", OptionList).highlighted_option
        if option is not None and option.id and option.id not in self.project_rows and await self._archive(option.id):
            await self.reload()

    async def action_restore(self) -> None:
        await self._restore()
        await self.reload()


__all__ = [
    "DetailsSidebar",
    "FileChange",
    "FileRow",
    "SessionRow",
    "SessionSidebar",
    "SessionsScreen",
    "SettingsScreen",
    "TopBar",
    "TuiPreferences",
    "mcp_markup",
    "modified_files",
    "relative_time",
    "session_status",
]
