"""Small Textual-only presentation widgets for the Nexus shell."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Markdown, Static, TextArea

from ..cli.render import escape_controls, sanitize
from .messages import AgentPickerRequested, CancelRequested, InputSubmitted

class Transcript(Markdown):
    """Markdown transcript with safe handling for streamed untrusted content."""

    can_focus = True

    def __init__(self, content: str = "", **kwargs) -> None:
        super().__init__(content, open_links=False, **kwargs)
        self._stream = None
        self._pending = ""
        self._finished = False

    async def on_mount(self) -> None:
        if self._pending and not self._finished:
            self._stream = Markdown.get_stream(self)
            await self._stream.write(self._pending)
            self._pending = ""
        elif not self._finished:
            self._stream = Markdown.get_stream(self)

    async def append_text(self, text: str) -> None:
        if not text or self._finished:
            return
        # Controls are neutralized, markup is parsed as Markdown (never Rich
        # markup), and links are not opened by terminal interaction.
        safe = escape_controls(text)[:8192]
        if self.is_mounted:
            if self._stream is None:
                self._stream = Markdown.get_stream(self)
            await self._stream.write(safe)
        else:
            self._pending += safe

    async def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None
        elif self._pending:
            await self.update(self._pending)
            self._pending = ""

    def begin_next(self) -> None:
        """Start a new stream segment after a completed turn."""
        self._finished = False
        self._stream = None

    async def on_unmount(self) -> None:
        await self.finish()


class TranscriptPane(Vertical):
    """Scrollable transcript root with stable extension points for detail panes."""

    def compose(self) -> ComposeResult:
        from textual.containers import VerticalScroll

        with VerticalScroll(id="transcript-scroll"):
            yield Transcript("", id="transcript")

    def set_markdown(self, content: str) -> None:
        self.query_one("#transcript", Transcript).update(content)


class ChatInput(Vertical):
    """Keyboard-first multiline prompt editor and submit/cancel controls."""

    class Submitted(Message):
        """Input submit button or Enter key was activated."""

    def compose(self) -> ComposeResult:
        yield TextArea(id="chat-editor", soft_wrap=True, tab_behavior="indent")
        with Horizontal(id="input-actions"):
            yield Button("Send", id="send", variant="primary")
            yield Button("Cancel", id="cancel")
            yield Static("Ctrl+Enter send · Shift+Tab agent · Ctrl+P commands · Ctrl+C cancel", id="input-help")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()
        self.query_one("#send", Button).disabled = True

    @on(Button.Pressed, "#send")
    def _send_pressed(self, _: Button.Pressed) -> None:
        self._submit()

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self, _: Button.Pressed) -> None:
        self.post_message(CancelRequested())

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "chat-editor":
            self.query_one("#send", Button).disabled = not bool(event.text_area.text.strip())

    def on_key(self, event) -> None:
        if event.key == "ctrl+enter":
            event.stop()
            event.prevent_default()
            self._submit()

    def _submit(self) -> None:
        editor = self.query_one("#chat-editor", TextArea)
        text = editor.text
        if text.strip():
            self.post_message(InputSubmitted(text))
            editor.clear()
            editor.focus()

    async def action_insert_newline(self) -> None:
        self.query_one("#chat-editor", TextArea).insert("\n")


class RootAgentBar(Static):
    """Visible active root-agent identity/source and picker affordance."""

    can_focus = True

    def set_agent(self, name: str, source: str, status: str = "idle") -> None:
        self.update(
            f"{sanitize(name, 64)} · {sanitize(source, 32)} · {sanitize(status, 32)} · Ctrl+G"
        )

    def on_click(self) -> None:
        self.post_message(AgentPickerRequested())

    def on_key(self, event) -> None:
        if event.key in {"enter", "space"}:
            event.stop()
            self.post_message(AgentPickerRequested())


class ConnectionStatus(Static):
    """Connection/turn feedback plus compact session status."""

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.update(text)
        self.set_class(error, "error")


__all__ = ["ChatInput", "ConnectionStatus", "RootAgentBar", "Transcript", "TranscriptPane"]
