"""Modal inspection for a single reducer-backed tool call (PLAN section 14.7)."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Static

from ...ui_support.tool_details import DetailSection


class ToolDetailsScreen(ModalScreen[None]):
    """Show a bounded call/result payload without expanding the transcript row."""

    DEFAULT_CSS = """
    ToolDetailsScreen {
        align: center middle;
        background: $background 70%;
    }
    #tool-details-dialog {
        width: 80%;
        max-width: 84;
        height: 70%;
        padding: 1 2;
        background: $panel;
        border: tall $border;
    }
    #tool-details-title {
        height: auto;
        margin-bottom: 1;
        color: $accent;
        text-style: bold;
    }
    #tool-details-body {
        height: 1fr;
        padding: 1;
        overflow-y: auto;
        background: $background;
        color: $text-muted;
    }
    """

    def __init__(self, title: str, sections: Sequence[DetailSection] | str) -> None:
        super().__init__()
        self.tool_title = title
        self.sections = sections

    def _render_body(self) -> Text:
        """Labelled sections: muted labels, bold accent titles, indented blocks."""
        if isinstance(self.sections, str):
            return Text(self.sections)
        out = Text()
        for index, section in enumerate(self.sections):
            if index:
                out.append("\n")
            out.append(section.title.upper() + "\n", style="bold")
            for row in section.rows:
                pad = "  " * (row.indent + 1)
                if row.header:
                    out.append(f"{pad}{row.label}\n", style="bold")
                elif row.block:
                    out.append(f"{pad}{row.label}\n", style="dim")
                    for line in row.value.split("\n"):
                        style = ""
                        if section.kind == "diff":
                            style = "green" if line.startswith("+") and not line.startswith("+++") else (
                                "red" if line.startswith("-") and not line.startswith("---") else (
                                    "magenta" if line.startswith("@@") else ""))
                        out.append(f"{pad}  {line}\n", style=style)
                else:
                    out.append(f"{pad}{row.label}: ", style="dim")
                    out.append(f"{row.value}\n")
        return out

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="tool-details-dialog"):
            yield Static(self.tool_title, id="tool-details-title", markup=False)
            yield Static(self._render_body(), id="tool-details-body", markup=False)

    def on_mount(self) -> None:
        # Escape or a click outside closes the dialog; the scroll takes the keys.
        self.call_after_refresh(lambda: self.query_one("#tool-details-dialog", VerticalScroll).focus())

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


__all__ = ["ToolDetailsScreen"]
