"""Markdown rendering and streaming contracts for the reducer-backed TUI."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Markdown

from nexus.ui_support.timeline import tool_output
from nexus.ui.tui.timeline import AssistantMessage, ThoughtLine, TurnWidget
from nexus.view import (
    AgentView,
    BlockView,
    ConversationView,
    MessageView,
    ToolCallView,
    TurnView,
)


class _MessageApp(App[None]):
    def __init__(self, message: MessageView) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        yield AssistantMessage(self.message, id="assistant")


class _TurnApp(App[None]):
    def __init__(self, turn: TurnView) -> None:
        super().__init__()
        self.turn = turn

    def compose(self) -> ComposeResult:
        yield TurnWidget(self.turn, {}, id="turn")


def _rich_markdown() -> str:
    return (
        "# Heading\n\n"
        "A **bold** word, *italic*, `inline code`, and [safe link](https://example.test).\n\n"
        "1. first\n2. second\n\n"
        "```python\nprint('hello')\n```"
    )


async def test_root_assistant_renders_full_markdown_and_disables_auto_links():
    text = _rich_markdown()
    message = MessageView(id="m1", role="assistant", blocks=[BlockView(text=text)])
    async with _MessageApp(message).run_test() as pilot:
        await pilot.pause()
        widget = pilot.app.query_one("#assistant", Markdown)
        assert widget._markdown == text
        assert widget._open_links is False
        assert widget.query("MarkdownHeader")
        assert widget.query("MarkdownBullet") or widget.query("MarkdownOrderedList")
        assert widget.query("MarkdownFence")


async def test_root_message_keeps_rich_markup_literal_and_escapes_terminal_controls():
    source = "[bold]untrusted[/bold]\x1b[31m"
    message = MessageView(id="unsafe", blocks=[BlockView(text=source)])
    async with _MessageApp(message).run_test() as pilot:
        await pilot.pause()
        widget = pilot.app.query_one("#assistant", Markdown)
        assert widget._markdown == "[bold]untrusted[/bold]\\x1b[31m"
        assert "[bold]untrusted[/bold]" in widget._markdown


async def test_partial_markdown_updates_and_replacements_keep_complete_content():
    first = "# Partial heading\n\n- entry\n\n```python\nprint("
    message = MessageView(id="stream", blocks=[BlockView(text=first)])
    async with _MessageApp(message).run_test() as pilot:
        await pilot.pause()
        widget = pilot.app.query_one("#assistant", AssistantMessage)

        appended = first + "'partial')"
        await widget.set_message(MessageView(id="stream", blocks=[BlockView(text=appended)]))
        await pilot.pause()
        assert widget._rendered == appended
        assert widget._markdown == appended

        replaced = "## Corrected\n\n- kept after provider rewrite\n"
        await widget.set_message(MessageView(id="stream", blocks=[BlockView(text=replaced)]))
        await pilot.pause()
        assert widget._rendered == replaced
        assert widget._markdown == replaced
        assert widget._stream is not None

        await widget.set_message(
            MessageView(id="stream", blocks=[BlockView(text=replaced)], done=True)
        )
        assert widget._stream is None
        assert widget._markdown == replaced


async def test_stream_completion_without_text_change_stops_markdown_stream():
    message = MessageView(id="done", blocks=[BlockView(text="# Complete")])
    async with _MessageApp(message).run_test() as pilot:
        await pilot.pause()
        widget = pilot.app.query_one("#assistant", AssistantMessage)
        assert widget._stream is not None
        await widget.set_message(
            MessageView(id="done", blocks=[BlockView(text="# Complete")], done=True)
        )
        assert widget._stream is None


async def test_root_thought_only_message_is_markdown_and_signature_is_private():
    thought = MessageView(
        id="thought",
        role="assistant",
        blocks=[
            BlockView(kind="thinking", text="**Checking** the request", signature="provider-secret-signature")
        ],
    )
    turn = TurnView(id="turn", messages=[thought])
    async with _TurnApp(turn).run_test() as pilot:
        await pilot.pause()
        # Thinking collapses to one "Thought:" line; the signature stays private.
        assert not pilot.app.query(AssistantMessage)
        widget = pilot.app.query_one(ThoughtLine)
        assert str(widget.render()) == "Thought: Checking the request"
        widget.expanded = True
        widget.set_message(thought)
        assert "**Checking** the request" in str(widget.render())
        assert "provider-secret-signature" not in str(widget.render())


async def test_nested_inspector_renders_message_markdown_with_safe_links():
    from nexus.ui.tui.agent_transcript import AgentTranscriptScreen

    text = _rich_markdown() + "\n\n[bold] is ordinary message text"
    agent = AgentView(
        id="child",
        body=ConversationView(
            turns=[
                TurnView(
                    id="child-turn",
                    messages=[MessageView(role="assistant", blocks=[BlockView(text=text)])],
                )
            ]
        ),
    )
    async with App().run_test() as pilot:
        screen = AgentTranscriptScreen(agent)
        await pilot.app.push_screen(screen)
        await pilot.pause(0.2)
        (widget,) = screen.query("#agent-timeline Markdown")
        assert widget._open_links is False
        assert "[bold] is ordinary message text" in widget._markdown
        assert widget.query("MarkdownHeader")
        assert widget.query("MarkdownFence")


def test_nested_transcript_keeps_tool_output_literal():
    tool = ToolCallView(
        name="Bash",
        status="completed",
        display="output begins ``` then ends ```` and [bold] stays literal",
    )
    assert tool_output(tool) == "output begins ``` then ends ```` and [bold] stays literal"
