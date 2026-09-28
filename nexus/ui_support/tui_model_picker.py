"""Searchable, grouped terminal model selector (PLAN §14.11)."""

from __future__ import annotations

import re

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .text import sanitize


def _natural(text: str) -> tuple[tuple[int, str | int], ...]:
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", text))


def _date(row: dict, field: str) -> tuple[int, int, int]:
    value = row.get(field)
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return (0, 0, 0)
    return tuple(-int(part) for part in value.split("-"))


def sort_models(rows: list[dict]) -> list[dict]:
    """Newest update, then release, then natural name/reference (missing dates last)."""
    return sorted(rows, key=lambda row: (
        _date(row, "last_updated"),
        _date(row, "release_date"),
        _natural(str(row.get("name") or row.get("id") or "")),
        _natural(str(row.get("provider") or "") + "/" + str(row.get("id") or "")),
    ))


def _ref(row: dict) -> str:
    return f"{row['provider']}/{row['id']}"


class ModelPickerScreen(ModalScreen[tuple[str, str | None, bool] | None]):
    """One modal for click and /model; favorites are a user preference callback."""

    def __init__(self, rows: list[dict], *, current: str, current_effort: str | None,
                 stored_override: str | None, effort_source: str | None,
                 favorites: list[str], recent: list[str], on_favorites) -> None:
        super().__init__()
        self.rows = sort_models(rows)[:2000]
        self.current = current
        self.current_effort = current_effort
        self.stored_override = stored_override
        self.effort_source = effort_source
        self.favorites = list(favorites)
        self.recent = list(recent)
        self.on_favorites = on_favorites
        self._visible_rows: list[dict | None] = []
        self._pending_effort: str | None = None
        self._effort_touched = False

    def compose(self) -> ComposeResult:
        with Vertical(id="model-picker-dialog"):
            yield Static("Select model                                      esc", id="model-picker-title")
            yield Input(placeholder="Search models and providers", id="model-picker-search")
            yield OptionList(id="model-picker-options")
            yield Static("↑/↓ navigate · Enter select · Ctrl+F favorite · ←/→ effort · Esc close",
                         id="model-picker-help")

    def on_mount(self) -> None:
        self._render_models()
        self.query_one(Input).focus()

    def _render_models(self, *, selected_ref: str | None = None) -> None:
        query = self.query_one(Input).value.casefold().strip()
        matching = [row for row in self.rows if query in (
            f"{row.get('name', '')} {row.get('provider', '')} {row.get('id', '')}"
        ).casefold()]
        by_ref = {_ref(row): row for row in matching}
        groups: list[tuple[str, list[dict]]] = []
        if not query:
            groups.extend((title, [by_ref[ref] for ref in refs if ref in by_ref])
                          for title, refs in (("Favorites", self.favorites), ("Recent", self.recent)))
        providers = sorted({str(row["provider"]) for row in matching}, key=str.casefold)
        groups.extend((provider, [row for row in matching if row["provider"] == provider])
                      for provider in providers)
        options: list[Option] = []
        self._visible_rows = []
        seen: set[str] = set()
        for title, rows in groups:
            unique = [row for row in rows if _ref(row) not in seen]
            if not unique:
                continue
            options.append(Option(Text(sanitize(title, 64), style="bold #ad83d8"), disabled=True))
            self._visible_rows.append(None)
            for row in unique:
                ref = _ref(row)
                seen.add(ref)
                label = Text("  ")
                label.append(sanitize(str(row.get("name") or row["id"]), 70), style="bold")
                label.append("  " + sanitize(str(row["provider"]), 40), style="dim")
                if ref in self.favorites:
                    label.append("  ★", style="yellow")
                if ref == self.current:
                    label.append("  ◀", style="dim")
                options.append(Option(label))
                self._visible_rows.append(row)
        listing = self.query_one(OptionList)
        listing.clear_options()
        listing.add_options(options or [Option("No matching models", disabled=True)])
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
        levels = row.get("supported_efforts") or ()
        if not self._effort_touched:
            self._pending_effort = (self.stored_override if self.stored_override in levels else
                                    self.current_effort if _ref(row) == self.current
                                    and self.current_effort in levels else None)
        effort = self._pending_effort or "model default"
        self.query_one("#model-picker-help", Static).update(
            f"Effort: {effort} · ←/→ change · Enter select · Ctrl+F favorite · Esc close"
        )

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
        preserve_agent = (ref == self.current and self.effort_source == "agent"
                          and self.stored_override is None and self.current_effort in
                          (row.get("supported_efforts") or ()))
        commit = self._effort_touched or preserve_agent
        self.dismiss((ref, (self._pending_effort if self._effort_touched else self.current_effort)
                      if commit else None, commit))

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
        elif event.key == "down" and isinstance(self.focused, Input):
            event.stop()
            self.query_one(OptionList).focus()
        elif event.key == "enter" and isinstance(self.focused, Input):
            event.stop()
            event.prevent_default()
            row = self._selected_row()
            if row is not None:
                self.query_one(OptionList).focus()
                self.query_one(OptionList).action_select()
