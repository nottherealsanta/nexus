"""Modal inspection for a single reducer-backed tool call (PLAN section 14.7)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Static


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

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.tool_title = title
        self.body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="tool-details-dialog"):
            yield Static(self.tool_title, id="tool-details-title", markup=False)
            yield Static(self.body, id="tool-details-body", markup=False)

    def on_mount(self) -> None:
        # Escape or a click outside closes the dialog; the scroll takes the keys.
        self.call_after_refresh(lambda: self.query_one("#tool-details-dialog", VerticalScroll).focus())

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


__all__ = ["ToolDetailsScreen"]
