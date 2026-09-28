"""Shared list presentation for inline completions and pickers."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets._option_list import Option

from .prompts import PromptChoice


@dataclass(frozen=True)
class ListItem:
    """One selectable row, with independently styled primary and secondary text."""

    primary: str
    secondary: str = ""
    value: str | None = None
    primary_style: str = "bold"
    current: bool = False


class ListPanel(OptionList):
    """Reusable selectable list with the common Nexus list palette."""

    DEFAULT_CSS = """
    ListPanel {
        width: 100%; height: auto; max-height: 8;
        padding: 0 0 0 1; margin: 0;
        background: $nx-list; color: $nx-quiet;
        border-top: tall $nx-element-hi; border-bottom: tall $nx-element-hi;
        border-left: none; border-right: none;
        scrollbar-size-vertical: 1;
        scrollbar-color: $nx-accent 50%; scrollbar-background: $nx-list;
    }
    ListPanel > .option-list--option { height: 1; padding: 0 0 0 1; }
    ListPanel > .option-list--option-highlighted,
    ListPanel > .option-list--option:hover { background: $nx-element-hi; color: $nx-text; }
    """

    def set_items(self, items: list[ListItem], *, selected: int = 0) -> None:
        """Replace rows and keep the requested selection visible."""
        self.clear_options()
        accent = str(self.app.theme_variables.get("nx-accent", "orange")) if self.is_attached else "orange"
        self.add_options([Option(self.render_item(item, accent=accent), id=None) for item in items])
        self.highlighted = selected if items else None
        if items:
            self.scroll_to_highlight()

    def on_key(self, event) -> None:
        if event.key == "tab" and self.highlighted is not None:
            event.stop()
            event.prevent_default()
            index = self.highlighted
            self.post_message(self.OptionSelected(self, self.get_option_at_index(index), index))

    @staticmethod
    def render_item(item: ListItem, *, accent: str = "orange") -> Text:
        text = Text()
        text.append(item.primary, style=(f"bold {accent}" if item.current else item.primary_style))
        if item.secondary:
            text.append("  ")
            text.append(item.secondary, style="dim")
        if item.current:
            text.append(" ◀", style="dim")
        return text


class ListPrompt(ModalScreen[str | None]):
    """A prompt answered from a list, docked above the composer like a picker.

    Enter picks the highlighted row; a row's ``key`` (or its 1-9 position)
    picks it directly; Esc answers ``cancel``. A disabled row answers
    ``cancel`` too (fail closed), or rings when there is none. With
    ``free_text`` an input takes a typed answer instead.
    """

    DEFAULT_CSS = """
    ListPrompt { align: center bottom; background: $background 40%; }
    ListPrompt > #prompt-dialog {
        width: 100%; max-width: 120; height: auto; max-height: 80%;
        margin: 0 2 6 2; padding: 0; background: $nx-list;
    }
    #prompt-title { width: 100%; padding: 0 1; color: $nx-accent; text-style: bold; background: $nx-element; }
    #prompt-body-scroll { height: auto; max-height: 14; padding: 0 1; scrollbar-size-vertical: 1; }
    #prompt-body { width: 1fr; height: auto; color: $nx-text; }
    ListPrompt #prompt-options, ListPrompt #prompt-options:focus { border: none; max-height: 10; }
    #prompt-text { margin: 0 1; border: tall $nx-element-hi; background: $nx-list; }
    #prompt-help { width: 100%; height: 1; padding: 0 1; color: $nx-muted; background: $nx-element; }
    """

    def __init__(
        self,
        title: str,
        body: str,
        choices: tuple[PromptChoice, ...],
        *,
        cancel: str | None = None,
        free_text: bool = False,
        cancel_hint: str = "dismiss",
    ) -> None:
        super().__init__()
        self.title_text = title
        self.body = body
        self.choices = choices
        self.cancel = cancel
        self.free_text = free_text
        self.cancel_hint = cancel_hint

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-dialog"):
            yield Static(self.title_text, id="prompt-title", markup=False)
            if self.body:
                with VerticalScroll(id="prompt-body-scroll"):
                    yield Static(self.body, id="prompt-body", markup=False)
            if self.choices:
                yield ListPanel(id="prompt-options")
            if self.free_text:
                yield Input(placeholder="Type your answer", id="prompt-text")
            keys = "/".join(choice.key for choice in self.choices if choice.key)
            parts = ["Enter answer" if self.free_text else "↑/↓ choose · Enter select"]
            if keys:
                parts.append(f"{keys} pick")
            parts.append(f"Esc {self.cancel_hint}")
            yield Static(" · ".join(parts), id="prompt-help", markup=False)

    def on_mount(self) -> None:
        if self.choices:
            options = self.query_one("#prompt-options", ListPanel)
            options.set_items([
                ListItem(
                    choice.label,
                    " · ".join(part for part in (
                        f"[{choice.key}]" if choice.key else "",
                        "unavailable" if choice.disabled else choice.hint,
                    ) if part),
                    value=choice.value,
                    primary_style="dim" if choice.disabled else "bold",
                )
                for choice in self.choices
            ], selected=next((i for i, c in enumerate(self.choices) if not c.disabled), 0))
            options.focus()
        if self.free_text:
            self.query_one("#prompt-text", Input).focus()

    def _pick(self, choice: PromptChoice) -> None:
        if choice.disabled and self.cancel is None:
            self.app.bell()
        elif choice.disabled:
            self.dismiss(self.cancel)
        else:
            self.dismiss(choice.value)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(self.cancel)
            return
        if isinstance(self.focused, Input):
            return
        key = event.key.casefold()
        for index, choice in enumerate(self.choices, 1):
            if key == choice.key or key == str(index):
                event.stop()
                self._pick(choice)
                return

    @on(OptionList.OptionSelected, "#prompt-options")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if 0 <= event.option_index < len(self.choices):
            self._pick(self.choices[event.option_index])

    @on(Input.Submitted, "#prompt-text")
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.value.strip():
            self.dismiss(event.value.strip())


__all__ = ["ListItem", "ListPanel", "ListPrompt"]
