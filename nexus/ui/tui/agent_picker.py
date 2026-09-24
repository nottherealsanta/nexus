"""Searchable picker for agents eligible to be selected as the root agent."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList

from ..cli.render import sanitize


class AgentPicker(ModalScreen[str | None]):
    """Modal search with arrow/Enter keyboard support and clickable options."""

    def __init__(self, agents: list[dict], *, current: str) -> None:
        super().__init__()
        self.agents = agents[:100]
        self.root_only = any("contexts" in row for row in agents)
        if self.root_only:
            self.agents = [row for row in agents if "root" in row.get("contexts", ())][:100]
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-picker-dialog"):
            yield Input(placeholder="Search root agents…", id="agent-search")
            yield OptionList(*self._options(self.agents), id="agent-options")

    def on_mount(self) -> None:
        self.query_one("#agent-search", Input).focus()

    def _options(self, rows: list[dict]) -> list[str]:
        return [
            f"{sanitize(row.get('name', '?'), 64)}  {sanitize(row.get('description', ''), 100)}"
            + (" [" + sanitize(row.get("id", ""), 24) + "]" if row.get("id") else "")
            for row in rows
        ]

    def _name_at(self, index: int) -> str:
        label = str(self.query_one("#agent-options", OptionList).get_option_at_index(index).prompt)
        if " [" in label and label.endswith("]"):
            return label.rsplit(" [", 1)[1][:-1]
        return label.split("  ", 1)[0]

    @on(Input.Changed, "#agent-search")
    def _search(self, event: Input.Changed) -> None:
        query = event.value.casefold().strip()
        rows = [
            row for row in self.agents
            if query in str(row.get("name", "")).casefold()
            or query in str(row.get("description", "")).casefold()
        ]
        options = self.query_one("#agent-options", OptionList)
        options.clear_options()
        options.add_options(self._options(rows))
        if options.option_count:
            options.highlighted = 0

    @on(Input.Submitted, "#agent-search")
    def _submit_search(self, _: Input.Submitted) -> None:
        options = self.query_one("#agent-options", OptionList)
        if options.option_count:
            index = options.highlighted if options.highlighted is not None else 0
            self.dismiss(self._name_at(index))

    @on(OptionList.OptionSelected)
    def _select(self, event: OptionList.OptionSelected) -> None:
        if event.option_index >= 0:
            self.dismiss(self._name_at(event.option_index))

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
        elif event.key in {"down", "tab"} and isinstance(self.app.focused, Input):
            event.stop()
            self.query_one("#agent-options", OptionList).focus()
        elif event.key == "up" and isinstance(self.app.focused, OptionList):
            options = self.query_one("#agent-options", OptionList)
            if options.highlighted in (None, 0):
                event.stop()
                self.query_one("#agent-search", Input).focus()


__all__ = ["AgentPicker"]
