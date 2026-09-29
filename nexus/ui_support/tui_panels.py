"""Side panels and the Settings screen for the Textual shell.

The session sidebar lists daemon sessions with a live status (working, needs
input, done) and an archive action. The details sidebar summarizes the reduced
``ConversationView``: session facts, files modified by tools, and MCP health
from the redacted ``Doctor`` report. Everything here renders host-provided data
only; the app owns every host call.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.markup import escape
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    ContentSwitcher,
    Input,
    OptionList,
    RadioButton,
    RadioSet,
    Static,
    Switch,
)
from textual.widgets.option_list import Option

from .context import context_usage
from .text import sanitize
from .timeline import split_diff_files

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_EDIT_TOOLS = frozenset({"edit", "multiedit", "write", "apply_patch", "patch"})
STATUS_LABELS = {"working": "Working", "input": "Needs input", "done": "Done", "idle": "", "archived": "Archived"}


# ---------------------------------------------------------------- pure helpers

@dataclass(frozen=True)
class FileChange:
    path: str
    added: int = 0
    removed: int = 0
    created: bool = False
    hunks: tuple[str, ...] = ()


def modified_files(view: Any) -> list[FileChange]:
    """Aggregate successful file edits across the session and its subagents."""
    files: dict[str, FileChange] = {}

    def visit(turns: Iterable[Any]) -> None:
        for turn in turns or ():
            for tool in getattr(turn, "tools", ()) or ():
                if str(tool.name).casefold() not in _EDIT_TOOLS or tool.status != "completed":
                    continue
                if tool.is_error or tool.error:
                    continue
                diff = tool.diff if isinstance(tool.diff, Mapping) else {}
                inputs = tool.input if isinstance(tool.input, Mapping) else {}
                metrics = tool.metrics if isinstance(tool.metrics, Mapping) else {}
                parts = split_diff_files(diff)
                if len(parts) > 1:
                    # A multi-file patch: attribute each file's own lines.
                    for path, hunk in parts:
                        prior = files.get(path, FileChange(path))
                        added = sum(1 for line in hunk.splitlines() if line.startswith("+"))
                        removed = sum(1 for line in hunk.splitlines() if line.startswith("-"))
                        files[path] = FileChange(
                            path, prior.added + added, prior.removed + removed,
                            prior.created, prior.hunks + (hunk,),
                        )
                    continue
                path = diff.get("path") or inputs.get("path") or inputs.get("file_path")
                if not isinstance(path, str) or not path:
                    continue
                prior = files.get(path, FileChange(path))
                hunk = diff.get("hunk")
                files[path] = FileChange(
                    path,
                    prior.added + _int(diff.get("added_lines")),
                    prior.removed + _int(diff.get("removed_lines")),
                    prior.created or bool(metrics.get("created")),
                    prior.hunks + ((hunk,) if isinstance(hunk, str) and hunk else ()),
                )

    visit(getattr(view, "turns", ()))
    for agent in (getattr(view, "agents", {}) or {}).values():
        visit(getattr(agent.body, "turns", ()))
    return list(files.values())


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def session_status(summary: Any, seen: Mapping[str, int], current: str) -> str:
    """``working``, ``input``, ``done`` (finished since last viewed), or ``idle``."""
    state = getattr(summary, "state", "idle")
    if state == "running":
        return "working"
    if state in {"awaiting_permission", "awaiting_input"}:
        return "input"
    last_seen = seen.get(summary.id)
    if summary.id != current and last_seen is not None and summary.last_seq > last_seen:
        return "done"
    return "idle"


def relative_time(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return ""
    seconds = max(0.0, (now or time.time()) - float(ts))
    if seconds < 45:
        return "just now"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = round(minutes / 60)
    return f"{hours}h ago" if hours < 24 else f"{round(hours / 24)}d ago"


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

class TopBar(Horizontal):
    """The shell's one-row nav bar: sidebar toggle, breadcrumb, status, actions.

    Pure presentation; it posts messages and the app owns every action.
    """

    class SidebarToggled(Message):
        pass

    class DetailsToggled(Message):
        pass

    class NewRequested(Message):
        pass

    _TARGETS: ClassVar[dict[str, str]] = {
        "topbar-sidebar-toggle": "SidebarToggled",
        "topbar-details-toggle": "DetailsToggled",
        "topbar-new": "NewRequested",
    }

    def compose(self) -> ComposeResult:
        toggle = Static("▌", id="topbar-sidebar-toggle", classes="topbar-button", markup=False)
        toggle.tooltip = "Toggle sessions sidebar · ctrl+b"
        yield toggle
        yield Static("", id="topbar-crumb", markup=False)
        yield Static("", id="topbar-status", markup=False)
        new = Static("+", id="topbar-new", classes="topbar-button", markup=False)
        new.tooltip = "New session · ctrl+n"
        yield new
        details = Static("▐", id="topbar-details-toggle", classes="topbar-button", markup=False)
        details.tooltip = "Toggle details sidebar · ctrl+l"
        yield details

    def set_session(self, title: str, status: str) -> None:
        self.query_one("#topbar-crumb", Static).update(sanitize(title or "New Session", 120))
        glyph = {"working": "◌ ", "input": "● ", "done": "✓ "}.get(status, "")
        self.query_one("#topbar-status", Static).update(f"{glyph}{STATUS_LABELS.get(status) or 'Idle'}")
        for name in ("working", "input", "done"):
            self.set_class(status == name, f"-{name}")

    def set_toggles(self, *, sessions: bool, details: bool) -> None:
        self.query_one("#topbar-sidebar-toggle").set_class(sessions, "-on")
        self.query_one("#topbar-details-toggle").set_class(details, "-on")

    def on_click(self, event) -> None:
        name = self._TARGETS.get(getattr(event.widget, "id", None) or "")
        if name:
            event.stop()
            self.post_message(getattr(self, name)())


# ---------------------------------------------------------------- session sidebar

class SessionRow(Horizontal):
    """One session line: status glyph, title, status or age, and delete target."""

    can_focus = True

    def __init__(self, session_id: str) -> None:
        super().__init__(classes="session-row")
        self.session_id = session_id
        self.status = "idle"
        self._frame = 0
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Static("·", classes="session-glyph", markup=False)
        yield Static("", classes="session-title", markup=False)
        yield Static("", classes="session-sub", markup=False)
        yield Static("×", classes="session-delete", markup=False)

    def update_row(self, summary: Any, status: str, *, active: bool) -> None:
        self.status = status
        if status == "working" and self._timer is None:
            self._timer = self.set_interval(0.12, self._spin)
        elif status != "working" and self._timer is not None:
            self._timer.stop()
            self._timer = None
        title = sanitize(summary.title or "Untitled session", 80)
        sub = STATUS_LABELS[status] if status != "archived" else ""
        self.query_one(".session-title", Static).update(title)
        self.query_one(".session-sub", Static).update(sub or relative_time(summary.last_activity))
        self.tooltip = f"{title}\n{summary.id}"
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
        if self.has_class("-active") and self.status in {"idle", "done"}:
            glyph = "●"
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
        groups: list[tuple[str, list[Any]]] = []
        for summary in self._summaries:
            if query and query not in f"{summary.title} {summary.id}".casefold():
                continue
            day = _day_label(summary.last_activity)
            if not groups or groups[-1][0] != day:
                groups.append((day, []))
            groups[-1][1].append(summary)
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
            container.query(".session-day").remove()
        existing = {row.session_id: row for row in container.query(SessionRow)}
        for session_id, row in existing.items():
            if session_id not in wanted:
                row.remove()
        new_rows = [SessionRow(session_id) for session_id in wanted if session_id not in existing]
        if new_rows:
            container.mount_all(new_rows)
        self.call_after_refresh(self._finish_render, groups, relayout)
        self.query_one("#sessions-heading", Static).update(
            f"SESSIONS  {len(self._summaries)}" if self._summaries else "SESSIONS"
        )

    def _finish_render(self, groups: list[tuple[str, list[Any]]], relayout: bool = True) -> None:
        container = self.query_one("#session-list", VerticalScroll)
        by_id = {row.session_id: row for row in container.query(SessionRow) if row.is_mounted}
        previous = None
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
                    container.mount(Static(day, classes="session-day", markup=False), before=row)
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
        tools = [tool for turn in view.turns for tool in turn.tools]
        usage = view.usage
        rows = [
            ("Status", phase.replace("_", " ")),
            ("Agent", agent),
            ("Model", model or "default"),
            ("Effort", effort or "default"),
            ("Turns", str(len(view.turns))),
            ("Tool calls", str(len(tools))),
        ]
        if usage.input_tokens or usage.output_tokens:
            rows.append(("Tokens", f"{usage.input_tokens:,} in · {usage.output_tokens:,} out"))
        ctx = context_usage(view)
        if ctx != "Preview":
            rows.append(("Context", ctx))
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
            yield Static("Appearance", classes="settings-heading", markup=False)
            yield Static("Theme for this terminal shell.", classes="settings-help", markup=False)
            with RadioSet(id="settings-theme"):
                for name, label in self._themes:
                    yield RadioButton(label, value=self._prefs["theme"] == name, name=name)
        with VerticalScroll(id="layout"):
            yield Static("Layout", classes="settings-heading", markup=False)
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

def _day_label(ts: float | None, today: date | None = None) -> str:
    if not ts:
        return "Earlier"
    day = datetime.fromtimestamp(ts, tz=UTC).astimezone().date()
    today = today or datetime.now(tz=UTC).astimezone().date()
    if day == today:
        return "Today"
    return day.strftime("%a %b %-d %Y")


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
    ) -> None:
        super().__init__()
        self._load, self._archive, self._restore = load, archive, restore
        self._current, self._seen = current, seen
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
        self._render_options()

    def _render_options(self) -> None:
        options = self.query_one("#sessions-options", OptionList)
        query = self.query_one("#sessions-search", Input).value.strip().casefold()
        rows = [s for s in self._summaries if not query or query in f"{s.title} {s.id}".casefold()]
        highlighted = options.highlighted_option.id if options.highlighted_option else self._current
        options.clear_options()
        items: list[Option] = []
        last_day = None
        for summary in rows:
            day = _day_label(summary.last_activity)
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
        if option is not None and option.id and await self._archive(option.id):
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
