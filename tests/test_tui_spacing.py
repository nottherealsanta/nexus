"""Textual geometry checks for the chat shell's compact spacing."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_ui_tui import FakeTransport, _client

from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.timeline import ConversationTimeline, UserMessage
from nexus.ui.tui.widgets import ChatEditor, ChatInput, RootAgentBar
from nexus.view import BlockView, MessageView


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
        assert editor.styles.padding.top == 0
        assert editor.styles.padding.bottom == 0
        assert editor.region.y == composer.region.y
        assert metadata.region.height == 1
        assert metadata.region.y == editor.region.y + editor.region.height
        assert bottom.region.height == 1
        assert bottom.region.y == metadata.region.y + metadata.region.height
        assert bottom.region.y + bottom.region.height == composer.region.y + composer.region.height
        assert app.query_one("#cwd-path").render().plain == str(Path.cwd())
        assert app.query_one("#context-usage").render().plain == "Preview"
        metadata_text = app.query_one("#root-agent").render().plain.casefold()
        assert "cwd" not in metadata_text
        assert "context" not in metadata_text
        assert composer.styles.border_top[0] == ""
        assert composer.styles.border_right[0] == ""
        assert composer.styles.border_bottom[0] == ""
        assert composer.styles.border_left[0] == ""
        assert editor.region.x == composer.region.x
        assert editor.region.right == composer.region.right
        assert len(app.query(RootAgentBar)) == 1
