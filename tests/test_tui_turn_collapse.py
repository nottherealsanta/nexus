"""Completed turns collapse to a durable-view summary and can expand again."""

from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client

from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.timeline import ConversationTimeline
from nexus.view import BlockView, ConversationView, MessageView, TurnView


@pytest.mark.asyncio
async def test_completed_turn_unmounts_body_then_restores_it():
    view = ConversationView(session_id="s", turns=[TurnView(
        id="one", phase="completed", elapsed_ms=2400,
        messages=[
            MessageView(role="user", event_seq=1, blocks=[BlockView(text="hi")]),
            MessageView(role="assistant", event_seq=2, blocks=[BlockView(text="hello")], done=True),
        ],
    )])
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(90, 35)) as pilot:
        await pilot.pause()
        timeline = app.query_one(ConversationTimeline)
        await timeline.set_view(view)
        await pilot.pause()
        turn = timeline._turns["one"]
        assert turn.query(".timeline-assistant")
        await pilot.click(".timeline-user", offset=(1, 1))
        await pilot.pause()
        assert turn.collapsed
        assert not turn.query(".timeline-assistant")
        assert "hello" in turn.query_one(".turn-collapsed").render().plain
        await pilot.click(".timeline-user", offset=(1, 1))
        await pilot.pause()
        assert not turn.collapsed
        assert turn.query(".timeline-assistant")
