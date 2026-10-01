"""Textual geometry checks for the chat shell's compact spacing."""

from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client

from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.timeline import AssistantMessage, ConversationTimeline, UserMessage
from nexus.ui.tui.widgets import ChatEditor, ChatInput, RootAgentBar
from nexus.view import BlockView, ConversationView, MessageView, ToolCallView, TurnView


@pytest.mark.asyncio
async def test_message_and_composer_have_vertical_breathing_room_without_extra_header():
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(64, 22)) as pilot:
        await pilot.pause()

        assert not app.query("#app-title")
        assert not app.query("#shell-header")

        conversation = app.query_one(ConversationTimeline)
        message = UserMessage(
            MessageView(role="user", blocks=[BlockView(text="A user prompt")]),
            classes="timeline-user",
        )
        await conversation.mount(message)
        await pilot.pause()

        assert message.styles.padding.top == 1
        assert message.styles.padding.bottom == 1
        assert message.region.height == 3
        assert message.styles.border_left[0] == ""

        composer = app.query_one(ChatInput)
        editor = app.query_one(ChatEditor)
        metadata = app.query_one("#runtime-info")
        bottom = app.query_one("#bottom-info")
        assert composer.styles.padding.top == 0
        assert composer.styles.padding.bottom == 0
        # The composer has one row of air around the text and two info rows.
        assert editor.styles.padding.top == 1
        assert editor.styles.padding.bottom == 1
        assert editor.region.y == composer.region.y
        assert metadata.region.height == 1
        assert metadata.region.y == editor.region.y + editor.region.height
        assert bottom.region.height == 1
        assert bottom.region.y == metadata.region.y + metadata.region.height
        assert bottom.region.y + bottom.region.height == composer.region.y + composer.region.height
        assert not app.query("#cwd-path")
        assert app.query_one("#context-usage").render().plain == "0 (0%)"
        metadata_text = app.query_one("#root-agent").summary().plain.casefold()
        assert "cwd" not in metadata_text
        assert "context" not in metadata_text
        assert composer.styles.border_top[0] == ""
        assert composer.styles.border_right[0] == ""
        assert composer.styles.border_bottom[0] == ""
        assert composer.styles.border_left[0] == ""
        assert editor.region.x == composer.region.x
        assert editor.region.right == composer.region.right
        assert len(app.query(RootAgentBar)) == 1


@pytest.mark.asyncio
async def test_prompt_reply_and_turn_summary_spacing():
    view = ConversationView(
        session_id="s",
        turns=[
            TurnView(
                id="turn",
                phase="completed",
                elapsed_ms=1200,
                messages=[
                    MessageView(
                        role="user",
                        blocks=[BlockView(text="A user prompt")],
                        event_seq=1,
                    ),
                    MessageView(
                        role="assistant",
                        blocks=[BlockView(text="An agent  \nreply")],
                        event_seq=2,
                        done=True,
                    ),
                ],
            )
        ],
    )
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        timeline = app.query_one(ConversationTimeline)
        await timeline.set_view(view)
        await pilot.pause()

        turn = timeline._turns["turn"]
        user = turn.query_one(UserMessage)
        assistant = turn.query_one(AssistantMessage)
        summary = turn.query_one(".timeline-summary")
        markdown = assistant.children[0]
        assert assistant.region.y - user.region.bottom == 1
        assert markdown.region.height >= 2
        # Markdown's final paragraph margin supplies exactly one blank row.
        assert summary.region.y - markdown.region.bottom == 1


@pytest.mark.asyncio
async def test_tool_rows_keep_compact_spacing_before_multiline_reply():
    view = ConversationView(
        session_id="s",
        turns=[
            TurnView(
                id="turn",
                phase="completed",
                messages=[
                    MessageView(
                        role="user",
                        blocks=[BlockView(text="Run the tool")],
                        event_seq=1,
                    ),
                    MessageView(
                        role="assistant",
                        blocks=[BlockView(text="The result is ready.  \nSecond line.")],
                        event_seq=4,
                        done=True,
                    ),
                ],
                tools=[
                    ToolCallView(
                        call_id="tool",
                        event_seq=3,
                        name="Read",
                        status="completed",
                        display="Read: 2 lines",
                    )
                ],
            )
        ],
    )
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        timeline = app.query_one(ConversationTimeline)
        await timeline.set_view(view)
        await pilot.pause()

        turn = timeline._turns["turn"]
        user = turn.query_one(UserMessage)
        tool = turn.query_one(".tool-card")
        assistant = turn.query_one(AssistantMessage)
        summary = turn.query_one(".timeline-summary")
        markdown = assistant.children[0]
        assert tool.region.y - user.region.bottom == 1
        assert assistant.region.y - tool.region.bottom == 1
        assert markdown.region.height >= 2
        assert summary.region.y - markdown.region.bottom == 1


@pytest.mark.parametrize(
    ("ending", "body", "minimum_height"),
    [
        ("plain paragraph", "A plain assistant reply.", 1),
        (
            "soft-wrapped paragraph",
            "A deliberately long final paragraph wraps naturally across several terminal rows without an explicit newline, allowing its final rendered line to be measured.",
            2,
        ),
        ("list", "- first item\n- final item", 2),
        ("fenced code", "```python\nprint(1)\nprint(2)\n```", 4),
    ],
)
@pytest.mark.asyncio
async def test_assistant_markdown_endings_leave_one_row_before_footer(
    ending: str, body: str, minimum_height: int
):
    view = ConversationView(
        session_id="s",
        turns=[
            TurnView(
                id="turn",
                phase="completed",
                messages=[
                    MessageView(
                        role="user",
                        blocks=[BlockView(text="A user prompt")],
                        event_seq=1,
                    ),
                    MessageView(
                        role="assistant",
                        blocks=[BlockView(text=body)],
                        event_seq=2,
                        done=True,
                    ),
                ],
            )
        ],
    )
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        timeline = app.query_one(ConversationTimeline)
        await timeline.set_view(view)
        await pilot.pause()

        turn = timeline._turns["turn"]
        assistant = turn.query_one(AssistantMessage)
        summary = turn.query_one(".timeline-summary")
        final_block = assistant.children[-1]
        assert final_block.region.height >= minimum_height, ending
        assert final_block.styles.margin.bottom == 1, ending
        assert summary.region.y - final_block.region.bottom == 1, ending


@pytest.mark.asyncio
async def test_final_edit_diff_leaves_one_row_before_turn_footer():
    view = ConversationView(
        session_id="s",
        turns=[
            TurnView(
                id="turn",
                phase="completed",
                messages=[
                    MessageView(
                        role="user",
                        blocks=[BlockView(text="Update the file")],
                        event_seq=1,
                    )
                ],
                tools=[
                    ToolCallView(
                        call_id="edit",
                        event_seq=2,
                        name="Edit",
                        status="completed",
                        display="updated",
                        diff={
                            "path": "example.py",
                            "added_lines": 1,
                            "removed_lines": 1,
                            "hunk": "--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-old\n+new",
                        },
                    )
                ],
            )
        ],
    )
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        timeline = app.query_one(ConversationTimeline)
        await timeline.set_view(view)
        await pilot.pause()

        turn = timeline._turns["turn"]
        tool = turn.query_one(".tool-card")
        diff = tool.query_one(".tool-diff")
        summary = turn.query_one(".timeline-summary")
        assert summary.region.y - diff.region.bottom == 1
        assert summary.region.y == tool.region.bottom


@pytest.mark.asyncio
async def test_tool_diff_to_assistant_keeps_exactly_one_blank_row():
    view = ConversationView(
        session_id="s",
        turns=[
            TurnView(
                id="turn",
                phase="completed",
                messages=[
                    MessageView(
                        role="user",
                        blocks=[BlockView(text="Update and explain")],
                        event_seq=1,
                    ),
                    MessageView(
                        role="assistant",
                        blocks=[BlockView(text="The file was updated.")],
                        event_seq=3,
                        done=True,
                    ),
                ],
                tools=[
                    ToolCallView(
                        call_id="edit",
                        event_seq=2,
                        name="Edit",
                        status="completed",
                        display="updated",
                        diff={
                            "path": "example.py",
                            "added_lines": 1,
                            "removed_lines": 1,
                            "hunk": "--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-old\n+new",
                        },
                    )
                ],
            )
        ],
    )
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        timeline = app.query_one(ConversationTimeline)
        await timeline.set_view(view)
        await pilot.pause()

        turn = timeline._turns["turn"]
        tool = turn.query_one(".tool-card")
        diff = tool.query_one(".tool-diff")
        assistant = turn.query_one(AssistantMessage)
        markdown = assistant.children[0]
        assert assistant.region.y - diff.region.bottom == 1
        assert assistant.region.y == tool.region.bottom
        assert markdown.region.height >= 1


@pytest.mark.asyncio
async def test_activity_rows_share_reply_left_edge():
    from nexus.ui.tui.timeline import TaskActivityWidget, ThoughtLine, ToolActivityWidget

    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(80, 30)) as pilot:
        timeline = app.query_one(ConversationTimeline)
        tool = ToolActivityWidget(ToolCallView(call_id="read", name="Read"), classes="tool-card")
        task = TaskActivityWidget(ToolCallView(call_id="task", name="Task"), {}, classes="tool-card task-card")
        thought = ThoughtLine(MessageView(role="assistant", blocks=[BlockView(kind="thinking", text="Thinking")]), classes="timeline-thought")
        reply = AssistantMessage(MessageView(role="assistant", blocks=[BlockView(text="Reply")]), classes="timeline-assistant")
        await timeline.mount(tool, task, thought, reply)
        await pilot.pause()
        # The reply is indented two cells under its agent label; activity rows
        # share the label's edge.
        left = reply.content_region.x - 2
        assert tool.query_one("#tool-header").content_region.x == left
        assert task.query_one("#tool-header").content_region.x == left
        assert task.query_one("#task-metrics").content_region.x == left
        assert thought.content_region.x == left
