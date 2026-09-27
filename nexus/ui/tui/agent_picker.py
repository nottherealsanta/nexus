"""Searchable picker for root agents and selectable models."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Key
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import OptionList

from ..cli.render import sanitize


class _PickerOptionList(OptionList):
    """OptionList with picker typeahead handled before its own key bindings."""

    def on_key(self, event: Key) -> None:
        panel = self.parent
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            panel.post_message(panel.Cancelled())
        elif event.key in {"left", "right"} and panel.kind == "model":
            event.stop()
            event.prevent_default()
            panel.cycle_effort(-1 if event.key == "left" else 1)
        elif event.key in {"left", "right"} and panel.kind == "effort":
            event.stop()
            event.prevent_default()
            options = panel.query_one("#agent-options", OptionList)
            if options.option_count:
                current = options.highlighted or 0
                options.highlighted = (current + (-1 if event.key == "left" else 1)) % options.option_count
        elif event.key == "backspace":
            if panel._filter:
                event.stop()
                event.prevent_default()
                panel._filter = panel._filter[:-1]
                panel._filter_options()
        elif event.is_printable and (
            (event.character and event.character.isprintable())
            or (len(event.key) == 1 and event.key.isprintable())
        ):
            event.stop()
            event.prevent_default()
            panel._filter += event.character or event.key
            panel._filter_options()

def _display_name(name: object) -> str:
    """Uppercase the initial for presentation without changing the agent id."""
    text = str(name or "?")
    return text[:1].upper() + text[1:]


class AgentPickerPanel(Vertical):
    """Reusable typeahead-filtered option list, embeddable above the composer."""

    class Selected(Message):
        def __init__(
            self, value: str, effort: str | None = None, *, commit_effort: bool = False
        ) -> None:
            super().__init__()
            self.value = value
            self.effort = effort
            self.commit_effort = commit_effort

    class Cancelled(Message):
        pass

    def __init__(self, agents: list[dict] | None = None, *, current: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.display = False
        self.agents: list[dict] = []
        self._visible_agents: list[dict] = []
        self._filter = ""
        self.current = current
        self.kind = "agent"
        self._pending_effort: str | None = None
        self._effort_choices: list[str | None] = [None]
        self._effort_touched = False
        self._current_model = ""
        self._current_effort: str | None = None
        self._stored_override: str | None = None
        self._effort_source: str | None = None
        if agents is not None:
            self.set_agents(agents, current=current)

    def compose(self) -> ComposeResult:
        yield _PickerOptionList(*self._options(self.agents), id="agent-options")
        from textual.widgets import Static

        yield Static("", id="picker-help", markup=False)

    def on_mount(self) -> None:
        if self.display:
            self.query_one("#agent-options", OptionList).focus()

    def set_agents(self, agents: list[dict], *, current: str = "") -> None:
        self.agents = agents[:100]
        self.current = current
        self._filter = ""
        selected_value = current
        if any("contexts" in row for row in agents):
            self.agents = [row for row in agents if "root" in row.get("contexts", ())][:100]
        self._visible_agents = list(self.agents)
        if self.is_mounted:
            options = self.query_one("#agent-options", OptionList)
            options.clear_options()
            options.add_options(self._options(self._visible_agents))
            selected = next(
                (
                    index
                    for index, row in enumerate(self._visible_agents)
                    if str(row.get("_value") or row.get("name") or "") == selected_value
                ),
                0,
            )
            options.highlighted = selected if options.option_count else None
            self._sync_help()

    def configure(
        self,
        kind: str,
        *,
        current_model: str = "",
        current_effort: str | None = None,
        stored_override: str | None = None,
        effort_source: str | None = None,
    ) -> None:
        self.kind = kind
        self._current_model = current_model
        self._current_effort = current_effort
        self._stored_override = stored_override
        self._effort_source = effort_source
        if kind == "model":
            index = self.query_one("#agent-options", OptionList).highlighted
            self._sync_model_effort(index)
        elif kind == "effort":
            self._sync_help("←/→ choose · Enter apply · Esc cancel")
        else:
            self._sync_help("Type to filter · Enter choose · Esc cancel")

    def _sync_model_effort(self, index: int | None) -> None:
        row = self._visible_agents[index] if index is not None and index < len(self._visible_agents) else {}
        levels = row.get("supported_efforts", ())
        self._effort_choices = (
            [None, *(str(level) for level in levels if isinstance(level, str))]
            if isinstance(levels, (list, tuple)) else [None]
        )
        self._effort_touched = False
        candidate = self._selection_at(index) if row else ""
        if self._stored_override and self._stored_override in self._effort_choices:
            # The stored session override survives a model selection. Keep it
            # selected when the candidate supports it, without re-applying it.
            self._pending_effort = self._stored_override
        elif (
            candidate == self._current_model
            and self._current_effort in self._effort_choices
        ):
            # The displayed effective value may come from the agent default.
            # It is informational until the user deliberately cycles effort.
            self._pending_effort = self._current_effort
        else:
            self._pending_effort = None
        self._sync_help()

    def _sync_help(self, text: str | None = None) -> None:
        if not self.is_mounted:
            return
        help_widget = self.query_one("#picker-help")
        if text is not None:
            help_widget.update(text)
        elif self.kind == "model":
            row = self._highlighted_model_row()
            raw_levels = row.get("supported_efforts")
            levels = raw_levels if isinstance(raw_levels, (list, tuple)) else ()
            candidate = self._selection_at(
                self.query_one("#agent-options", OptionList).highlighted or 0
            ) if self._visible_agents else ""
            if self._effort_touched:
                value = "Default / clear" if self._pending_effort is None else self._pending_effort
                suffix = " (apply)"
            elif self._stored_override and self._stored_override not in levels:
                support = "unsupported" if isinstance(raw_levels, (list, tuple)) else "support unknown"
                value = f"Dormant {self._stored_override} (kept; {support})"
                suffix = ""
            elif self._pending_effort is not None:
                value = self._pending_effort
                suffix = " (keep)"
            else:
                value = "model default"
                suffix = " (keep)" if self._stored_override else ""
            # Source is useful when the current row's effective effort is
            # inherited rather than explicitly stored.
            if (
                not self._effort_touched
                and candidate == self._current_model
                and self._stored_override is None
                and self._pending_effort is not None
                and self._effort_source
            ):
                suffix = f" ({self._effort_source}; preserve)"
            help_widget.update(
                f"Effort: {value}{suffix}   ←/→ change · Enter apply · Esc cancel"
            )
        elif self.kind == "effort":
            help_widget.update("←/→ choose · Enter apply · Esc cancel")
        else:
            help_widget.update("Type to filter · Enter choose · Esc cancel")

    def cycle_effort(self, direction: int) -> None:
        if self.kind != "model":
            return
        options = self.query_one("#agent-options", OptionList)
        if options.highlighted is None:
            return
        try:
            index = self._effort_choices.index(self._pending_effort)
        except ValueError:
            index = 0
        self._pending_effort = self._effort_choices[(index + direction) % len(self._effort_choices)]
        self._effort_touched = True
        self._sync_help()

    def _highlighted_model_row(self) -> dict:
        options = self.query_one("#agent-options", OptionList)
        index = options.highlighted
        return (
            self._visible_agents[index]
            if index is not None and index < len(self._visible_agents) else {}
        )

    def _options(self, rows: list[dict]) -> list[str]:
        return [
            f"{sanitize(row.get('_label') or _display_name(row.get('name', '?')), 64)}  "
            f"{sanitize(row.get('description', ''), 100)}"
            + (" [" + sanitize(row.get("id", ""), 24) + "]" if row.get("id") else "")
            for row in rows
        ]

    def _selection_at(self, index: int) -> str:
        row = self._visible_agents[index]
        # Model rows carry their canonical provider/model reference in _value;
        # agent rows select by canonical name, never their display label.
        return str(row.get("_value") or row.get("name") or "")

    def _filter_options(self) -> None:
        query = self._filter.casefold().strip()
        self._visible_agents = [
            row for row in self.agents
            if query in str(row.get("_label") or row.get("name", "")).casefold()
            or query in str(row.get("id", "")).casefold()
            or query in str(row.get("_value", "")).casefold()
            or query in str(row.get("description", "")).casefold()
        ]
        options = self.query_one("#agent-options", OptionList)
        options.clear_options()
        options.add_options(self._options(self._visible_agents))
        if options.option_count:
            selected = next(
                (
                    index
                    for index, row in enumerate(self._visible_agents)
                    if not query
                    and str(row.get("_value") or row.get("name") or "") == self.current
                ),
                0,
            )
            options.highlighted = selected
        if self.kind == "model":
            self._sync_model_effort(options.highlighted)

    @on(OptionList.OptionSelected)
    def _select(self, event: OptionList.OptionSelected) -> None:
        if event.option_index >= 0:
            selected_model = self._selection_at(event.option_index)
            preserve_agent_effort = (
                self.kind == "model"
                and selected_model == self._current_model
                and self._effort_source == "agent"
                and self._stored_override is None
                and self._current_effort in self._effort_choices
            )
            commit_effort = self.kind == "model" and (
                self._effort_touched or preserve_agent_effort
            )
            effort = (
                (self._pending_effort if self._effort_touched else self._current_effort)
                if commit_effort else
                (self._selection_at(event.option_index)
                 if self.kind == "effort" else None)
            )
            self.post_message(self.Selected(
                self._selection_at(event.option_index), effort,
                commit_effort=commit_effort,
            ))

    @on(OptionList.OptionHighlighted)
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.kind == "model":
            self._sync_model_effort(event.option_index)

class AgentPicker(ModalScreen[str | None]):
    """Modal wrapper retained for standalone picker flows."""

    def __init__(self, agents: list[dict], *, current: str) -> None:
        super().__init__()
        self.agents = agents
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-picker-dialog"):
            yield AgentPickerPanel(self.agents, current=self.current, id="agent-picker-panel")

    def on_mount(self) -> None:
        panel = self.query_one(AgentPickerPanel)
        panel.display = True
        self.query_one("#agent-options", OptionList).focus()

    @on(AgentPickerPanel.Selected)
    def _picker_selected(self, message: AgentPickerPanel.Selected) -> None:
        self.dismiss(message.value)

    @on(AgentPickerPanel.Cancelled)
    def _picker_cancelled(self, _: AgentPickerPanel.Cancelled) -> None:
        self.dismiss(None)


__all__ = ["AgentPicker", "AgentPickerPanel"]
