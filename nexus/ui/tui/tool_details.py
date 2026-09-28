"""Modal inspection for a single reducer-backed tool call (PLAN section 14.7)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .messages import AgentOpenRequested


class ToolDetailsScreen(ModalScreen[None]):
    """Show a bounded call/result payload without expanding the transcript row."""

    DEFAULT_CSS = """
    Screen ToolDetailsScreen {
        align: center middle;
        background: $background 70%;
    }
    #tool-details-dialog {
        width: 90%;
        max-width: 110;
        height: 80%;
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
    #tool-details-close {
        width: 16;
        margin-top: 1;
        dock: right;
    }
    """

    def __init__(self, title: str, body: str, *, agent_id: str | None = None) -> None:
        super().__init__()
        self.tool_title = title
        self.body = body
        self.agent_id = agent_id

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="tool-details-dialog"):
            yield Static(self.tool_title, id="tool-details-title", markup=False)
            yield Static(self.body, id="tool-details-body", markup=False)
            if self.agent_id:
                yield Button("Open child agent", id="tool-details-agent")
            yield Button("Close", id="tool-details-close", variant="primary")

    def on_mount(self) -> None:
        self.call_after_refresh(lambda: self.query_one("#tool-details-close", Button).focus())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tool-details-agent" and self.agent_id:
            self.post_message(AgentOpenRequested(self.agent_id))
            self.dismiss(None)
            return
        if event.button.id == "tool-details-close":
            self.dismiss(None)

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


__all__ = ["ToolDetailsScreen"]
