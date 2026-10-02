"""Searchable, grouped terminal model selector (PLAN §14.11)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .fuzzy import highlight_spans
from .model_choice import _ref, model_groups, preselected_effort, recent_models, selection_effort
from .model_choice import sort_models as sort_models  # noqa: F401 - re-exported
from .text import sanitize


class ModelPickerScreen(ModalScreen[tuple[str, str | None, bool] | None]):
    """One modal for click and /model; favorites are a user preference callback."""

    def __init__(self, rows: list[dict], *, current: str, current_effort: str | None,
                 stored_override: str | None, effort_source: str | None,
                 favorites: list[str], recent: list[str], on_favorites,
                 on_refresh: Callable[[], Awaitable[list[dict]]] | None = None) -> None:
        super().__init__()
        self.rows = recent_models(rows)[:2000]
        self.sort_mode = "updated"
        self.current = current
        self.current_effort = current_effort
        self.stored_override = stored_override
        self.effort_source = effort_source
        self.favorites = list(favorites)
        self.recent = list(recent)
        self.on_favorites = on_favorites
        self.on_refresh = on_refresh
        self._refreshing = False
        self._visible_rows: list[dict | None] = []
        self._pending_effort: str | None = None
        self._effort_touched = False

    def compose(self) -> ComposeResult:
        with Vertical(id="model-picker-dialog"):
            with Horizontal(id="model-picker-head"):
                yield Static("Select model                                      esc", id="model-picker-title")
                yield Static("↻ ctrl+r", id="model-picker-refresh")
            yield Input(placeholder="Search models and providers", id="model-picker-search")
            yield OptionList(id="model-picker-options")

    def on_mount(self) -> None:
        self._render_models()
        self.query_one(Input).focus()

    def _render_models(self, *, selected_ref: str | None = None) -> None:
        query = self.query_one(Input).value.strip()
        groups, self._highlights = model_groups(
            self.rows, query=query, sort_mode=self.sort_mode, favorites=self.favorites, recent=self.recent)
        self.query_one("#model-picker-title", Static).update(
            f"Select model · {'Updated ↓' if self.sort_mode == 'updated' else 'Name A–Z'}"
        )
        options: list[Option] = []
        self._visible_rows = []
        for title, unique in groups:
            options.append(Option(Text(sanitize(title, 64), style="bold #ad83d8"), disabled=True))
            self._visible_rows.append(None)
            for row in unique:
                ref = _ref(row)
                label = Text("  ")
                name = Text(sanitize(str(row.get("name") or row["id"]), 70), style="bold")
                for start, end in highlight_spans(self._highlights.get(ref, ())):
                    name.stylize("bold underline #fab283", start, end)
                label.append_text(name)
                if ref in self.favorites:
                    label.append("  ★", style="yellow")
                if ref == self.current:
                    label.append("  ◀", style="dim")
                provider = sanitize(str(row["provider"]), 40)
                width = self.query_one(OptionList).size.width - 2  # scrollbar gutter
                pad = max(2, width - label.cell_len - len(provider) - 1)
                label.append(" " * pad)
                label.append(provider, style="dim")
                options.append(Option(label))
                self._visible_rows.append(row)
        listing = self.query_one(OptionList)
        listing.clear_options()
        empty = ("No matching models" if query else
                 "No selectable models · configure a provider in nexus.toml or ~/.nexus/config.toml")
        listing.add_options(options or [Option(empty, disabled=True)])
        chosen = selected_ref or (self.current if not query else "")
        index = next((i for i, row in enumerate(self._visible_rows)
                      if row is not None and _ref(row) == chosen), None)
        if index is None:
            index = next((i for i, row in enumerate(self._visible_rows) if row is not None), None)
        listing.highlighted = index
        if index is not None:
            listing.scroll_to_highlight()
        self._sync_effort()

    def _selected_row(self) -> dict | None:
        index = self.query_one(OptionList).highlighted
        return self._visible_rows[index] if index is not None and index < len(self._visible_rows) else None

    def _sync_effort(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        if not self._effort_touched:
            self._pending_effort = preselected_effort(
                row, current=self.current, current_effort=self.current_effort, stored_override=self.stored_override)

    async def _refresh_catalogue(self) -> None:
        """Re-fetch models.dev through the host, then rebuild the list in place."""
        if self.on_refresh is None or self._refreshing:
            return
        self._refreshing = True
        button = self.query_one("#model-picker-refresh", Static)
        button.update("↻ refreshing…")
        try:
            rows = await self.on_refresh()
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal, list is kept
            button.update(f"↻ failed · {sanitize(str(exc), 60)}")
        else:
            row = self._selected_row()
            self.rows = recent_models(rows)[:2000]
            self._render_models(selected_ref=_ref(row) if row else None)
            button.update(f"↻ {len(self.rows)} models")
        finally:
            self._refreshing = False

    def on_click(self, event) -> None:
        if getattr(event.widget, "id", None) == "model-picker-refresh":
            event.stop()
            self.run_worker(self._refresh_catalogue(), group="model-refresh", exclusive=True)

    def on_resize(self) -> None:
        if self._visible_rows:
            row = self._selected_row()
            self._render_models(selected_ref=_ref(row) if row else None)

    @on(Input.Changed)
    def _search(self) -> None:
        self._effort_touched = False
        self._render_models()

    @on(OptionList.OptionHighlighted)
    def _highlight(self) -> None:
        self._effort_touched = False
        self._sync_effort()

    @on(OptionList.OptionSelected)
    def _select(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        ref = _ref(row)
        effort, commit = selection_effort(
            row, current=self.current, current_effort=self.current_effort, stored_override=self.stored_override,
            effort_source=self.effort_source, pending=self._pending_effort, touched=self._effort_touched)
        self.dismiss((ref, effort, commit))

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)
        elif event.key == "ctrl+f":
            event.stop()
            event.prevent_default()
            row = self._selected_row()
            if row is not None:
                ref = _ref(row)
                self.favorites = ([value for value in self.favorites if value != ref]
                                  if ref in self.favorites else [ref, *self.favorites][:100])
                self.on_favorites(self.favorites)
                self._render_models(selected_ref=ref)
        elif event.key == "ctrl+r":
            event.stop()
            event.prevent_default()
            self.run_worker(self._refresh_catalogue(), group="model-refresh", exclusive=True)
        elif event.key == "ctrl+s":
            event.stop()
            event.prevent_default()
            row = self._selected_row()
            self.sort_mode = "name" if self.sort_mode == "updated" else "updated"
            self._render_models(selected_ref=_ref(row) if row else None)
        elif event.key in {"left", "right"} and isinstance(self.focused, OptionList):
            event.stop()
            event.prevent_default()
            row = self._selected_row()
            if row is not None:
                levels = [None, *(row.get("supported_efforts") or ())]
                try:
                    index = levels.index(self._pending_effort)
                except ValueError:
                    index = 0
                self._pending_effort = levels[(index + (1 if event.key == "right" else -1)) % len(levels)]
                self._effort_touched = True
                self._sync_effort()
        elif event.key in {"down", "up"} and isinstance(self.focused, Input):
            event.stop()
            event.prevent_default()
            listing = self.query_one(OptionList)
            listing.focus()
            if event.key == "down":
                listing.action_cursor_down()
            else:
                listing.action_cursor_up()
        elif event.key == "enter" and isinstance(self.focused, Input):
            event.stop()
            event.prevent_default()
            row = self._selected_row()
            if row is not None:
                self.query_one(OptionList).focus()
                self.query_one(OptionList).action_select()
