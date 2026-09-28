"""Search and resume durable archived sessions through host callbacks (plan §3)."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Input, OptionList, Static
from textual.widgets._option_list import Option

from .text import sanitize


def _get(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)


def _single_line(value: Any, limit: int = 40) -> str:
    return re.sub(r"\s+", " ", sanitize(str(value or "Untitled session"), limit * 3)).strip()[:limit]


def _ago(ts: Any) -> str:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return "unknown"
    seconds = max(0, int(time.time() - ts))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


class ArchivedSessionsScreen(Screen[str | None]):
    """The archive browser; all durable data arrives through host callbacks."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "dismiss", "Cancel")]

    def __init__(
        self,
        *,
        load: Callable[..., Awaitable[Any]],
        preview: Callable[..., Awaitable[Any]],
        search: Callable[..., Awaitable[list[str]]],
        current: str,
    ) -> None:
        super().__init__()
        self._load = load
        self._preview = preview
        self._search = search
        self._current = current
        self._rows: list[Any] = []
        self._content_search = False
        self._generation = 0
        self._cursor = 0
        self._has_more = False

    def compose(self) -> ComposeResult:
        with Vertical(id="archived-dialog"):
            yield Static("Archived Sessions", id="archived-title", markup=False)
            with Horizontal(id="archived-search-row"):
                yield Input(placeholder="Search archived sessions...", id="archived-search")
                yield Static("◇ search content", id="archived-content-toggle", markup=False)
            with Horizontal(id="archived-body"):
                yield OptionList(id="archived-list")
                yield Static("", id="archived-detail", markup=False)
            yield Static("Enter resume · p preview · Esc cancel", id="archived-hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#archived-search", Input).focus()
        self.run_worker(self._refresh(), group="archived-query", exclusive=True)

    async def _refresh(self, *, append: bool = False) -> None:
        self._generation += 1
        generation = self._generation
        query = self.query_one("#archived-search", Input).value.strip()
        cursor = self._cursor if append else 0
        try:
            page = await self._load(query=query, limit=200, cursor=cursor)
            page_rows = list(_get(page, "sessions", ()))
            rows = page_rows
            if self._content_search and query:
                ids = set(await self._search(query, archived_only=True, limit=50))
                rows = [row for row in rows if _get(row, "id") in ids]
        except Exception as exc:  # noqa: BLE001 - advisory list failure stays in modal
            if generation == self._generation:
                self.query_one("#archived-detail", Static).update(f"Could not load archives: {sanitize(str(exc), 160)}")
            return
        if generation != self._generation or not self.is_mounted:
            return
        self._rows = [*self._rows, *rows] if append else rows
        self._cursor = cursor + len(page_rows)
        self._has_more = bool(_get(page, "has_more", False))
        options = []
        for row in self._rows:
            title = _single_line(_get(row, "title"))
            parent = _get(row, "parent_id", "")
            prefix = "├─ " if parent else ""
            label = Text()
            label.append(f"{prefix}{title:<40}", style="bold")
            label.append(f"  {_get(row, 'message_count', 0)} msgs  {_ago(_get(row, 'last_activity'))}", style="dim")
            options.append(Option(label))
        listing = self.query_one("#archived-list", OptionList)
        listing.clear_options()
        listing.add_options(options)
        if options:
            listing.highlighted = 0
            self._show_detail(0)
        else:
            self.query_one("#archived-detail", Static).update("No archived sessions")

    def _show_detail(self, index: int) -> None:
        if not 0 <= index < len(self._rows):
            return
        row = self._rows[index]
        archived = _ago(_get(row, "archived_at"))
        reason = _get(row, "reason", "user")
        lines = [
            _single_line(_get(row, "title"), 100), "",
            f"ID: {str(_get(row, 'id', ''))[:12]}",
            f"{_get(row, 'message_count', 0)} msgs · {_ago(_get(row, 'last_activity'))}",
            f"Archived {archived} ({'auto' if reason == 'auto' else 'manual'})", "",
            "Press p to preview content",
        ]
        self.query_one("#archived-detail", Static).update("\n".join(lines))

    @on(Input.Changed, "#archived-search")
    def _query_changed(self, _event: Input.Changed) -> None:
        self.run_worker(self._refresh(), group="archived-query", exclusive=True)

    @on(OptionList.OptionHighlighted, "#archived-list")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_index is not None:
            self._show_detail(event.option_index)

    @on(OptionList.OptionSelected, "#archived-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_index is not None and 0 <= event.option_index < len(self._rows):
            self.dismiss(str(_get(self._rows[event.option_index], "id")))

    def on_click(self, event) -> None:
        if getattr(event.widget, "id", None) == "archived-content-toggle":
            event.stop()
            self._content_search = not self._content_search
            self.query_one("#archived-content-toggle", Static).update(
                ("◆" if self._content_search else "◇") + " search content"
            )
            self.run_worker(self._refresh(), group="archived-query", exclusive=True)

    async def on_key(self, event) -> None:
        if event.key == "p" and isinstance(self.focused, OptionList):
            event.stop()
            index = self.query_one("#archived-list", OptionList).highlighted
            if index is None or index >= len(self._rows):
                return
            try:
                result = await self._preview(str(_get(self._rows[index], "id")))
                text = str(_get(result, "text", ""))
                if _get(result, "truncated", False):
                    text += "\npreview truncated"
                self.query_one("#archived-detail", Static).update(text + "\n---")
            except Exception as exc:  # noqa: BLE001 - preview is advisory
                self.query_one("#archived-detail", Static).update(sanitize(str(exc), 160))
        elif event.key == "pagedown" and self._has_more:
            event.stop()
            self.run_worker(self._refresh(append=True), group="archived-query", exclusive=True)
