"""Compact layout and interaction checks for the Textual chat shell."""

from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client
from textual.widgets import Static, TextArea

from nexus.ui.tui.agent_picker import AgentPickerPanel
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.timeline import ConversationTimeline, UserMessage
from nexus.ui.tui.widgets import (
    ChatEditor,
    ChatInput,
    CompletionPopup,
    RootAgentBar,
)
from nexus.view import BlockView, ConversationView, MessageView, TurnView


@pytest.mark.asyncio
async def test_composer_metadata_and_conversation_layout_at_narrow_width():
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(42, 18)) as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        composer = app.query_one(ChatInput)
        metadata = app.query_one("#runtime-info")
        bottom = app.query_one("#bottom-info")
        popup = app.query_one(CompletionPopup)
        conversation = app.query_one(ConversationTimeline)
        context_preview = app.query_one("#context-preview")

        assert app.focused is editor
        assert not context_preview.display
        assert app.query_one("#context-header") in conversation.children
        assert editor.region.y > conversation.region.y
        assert metadata.region.y == editor.region.y + editor.region.height
        assert bottom.region.y == metadata.region.y + metadata.region.height
        assert bottom.region.y + bottom.region.height == composer.region.y + composer.region.height
        assert metadata.region.width <= 42
        assert bottom.region.width <= 42
        assert editor.region.width <= 42
        assert editor.region.height >= 3
        assert composer.styles.border_top[0] == ""
        assert composer.styles.border_right[0] == ""
        assert composer.styles.border_bottom[0] == ""
        assert composer.styles.border_left[0] == ""
        # The composer sits on the page; its editor and agent line form the box.
        assert composer.styles.background.hex.lower() == "#141414"
        assert composer.styles.padding.bottom == 0
        assert not popup.display
        assert metadata.styles.background.hex.lower() == "#141414"
        assert not app.query("#cwd-path")
        assert app.query_one("#context-usage").region.right == bottom.region.right
        assert editor.styles.border_left[0] == ""
        assert editor.styles.border_top[0] == ""
        assert editor.styles.border_right[0] == ""
        assert editor.styles.border_bottom[0] == ""
        assert editor.region.y == composer.region.y
        assert app.query_one("#root-agent", RootAgentBar)
        assert str(app.query_one("#context-usage", Static).render()) == "0 (0%)"
        assert app.query_one("#context-usage", Static).display
        assert not app.query("#app-title")
        assert "Ctrl+" not in str(metadata.render())
        assert not app.query("#input-help")
        assert app.query_one("#connection-status").region.height <= 1
        assert editor.region.right <= 42
        assert "default" not in str(app.query_one("#root-agent", RootAgentBar).summary())
        assert "seq" not in str(app.query_one("#connection-status").render())

        app._show_inline_picker(
            "agent",
            [{"name": "general", "description": "General", "contexts": ["root"]}],
        )
        await pilot.pause()
        picker = app.query_one("#inline-picker", AgentPickerPanel)
        assert picker.display
        assert picker.region.y < editor.region.y
        assert picker.region.y + picker.region.height <= editor.region.y
        assert picker.region.y + picker.region.height == composer.region.y
        assert picker.region.x == composer.region.x
        assert picker.region.right == composer.region.right
        assert picker.region.height <= 9
        assert picker.styles.border_top[0] == picker.styles.border_bottom[0] == ""
        assert picker.styles.outline_left[0] == ""
        assert picker.styles.padding.top == picker.styles.padding.bottom == 0
        options = picker.query_one("#agent-options")
        assert options.region.x == picker.region.x
        assert options.region.right == picker.region.right
        assert options.highlighted == 0
        assert options.region.height == 1
        assert app.focused is options
        app._close_inline_picker()
        await pilot.pause()
        assert app.focused is editor

        user = UserMessage(MessageView(role="user", blocks=[]), classes="timeline-user")
        await conversation.mount(user)
        await pilot.pause()
        assert user.styles.background.hex.lower() == "#141414"  # $nx-panel prompt block
        assert app.query_one("#context-header") in conversation.children


@pytest.mark.asyncio
async def test_focused_editor_keeps_session_shortcuts_and_reconnect_refreshes_metadata():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(50, 20)) as pilot:
        await pilot.pause()
        assert app.focused is app.query_one(TextArea)

        await pilot.press("ctrl+n")
        await pilot.pause()
        assert app.controller.session != "s"

        await pilot.press("ctrl+o")
        await pilot.pause()
        assert "SessionList" in transport.trace
        await pilot.press("escape")  # close the Sessions dialog
        await pilot.pause()

        await pilot.press("ctrl+f")
        await pilot.pause()
        assert app.controller.session == "forked"
        assert "SessionFork" in transport.trace

        app.controller.provider = "fresh-provider"
        app.controller.model = "fresh-model"
        await app.action_reconnect()
        await pilot.pause()
        visible = str(app.query_one("#root-agent", RootAgentBar).summary())
        assert "General" in visible
        assert "fresh-provider/fresh-model" not in visible


@pytest.mark.asyncio
async def test_adjacent_conversation_turns_have_compact_vertical_spacing():
    view = ConversationView(
        session_id="s",
        turns=[
            TurnView(
                id=f"t{index}",
                messages=[
                    MessageView(
                        role="user",
                        blocks=[BlockView(text=f"prompt {index}")],
                        event_seq=index * 2 + 1,
                    ),
                    MessageView(
                        role="assistant",
                        blocks=[BlockView(text=f"answer {index}")],
                        event_seq=index * 2 + 2,
                        done=True,
                    ),
                ],
            )
            for index in range(2)
        ],
    )
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(72, 24)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationTimeline)
        await conversation.set_view(view)
        await pilot.pause()
        assistants = sorted(app.query(".timeline-assistant"), key=lambda widget: widget.region.y)
        users = sorted(app.query(".timeline-user"), key=lambda widget: widget.region.y)
        assert len(assistants) == len(users) == 2
        assert users[1].region.y - (assistants[0].region.y + assistants[0].region.height) <= 2
