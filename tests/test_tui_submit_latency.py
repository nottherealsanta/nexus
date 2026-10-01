"""A submitted prompt shows quickly: presence events skip the timeline and
unchanged turns are not reconciled again."""

from __future__ import annotations

import asyncio

import pytest
from test_ui_tui import FakeTransport, _client

from nexus.events import Event
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.timeline import ConversationTimeline, TurnWidget
from nexus.view import apply_many, initial_state


def _turn_events(turn: str, seq: int, prompt: str) -> list[Event]:
    def event(kind: str, offset: int, data: dict | None = None) -> Event:
        return Event(type=kind, data=data or {}, seq=seq + offset, session="s", turn=turn)

    return [
        event("input.queued", 0, {"queued_id": turn, "content": [{"text": prompt}]}),
        event("turn.started", 1),
        event("input.consumed", 2, {"queued_id": turn, "turn": turn}),
        event("text", 3, {"text": f"reply to {prompt}"}),
        event("turn.completed", 4),
    ]


@pytest.mark.asyncio
async def test_unchanged_turns_are_not_reconciled_again(monkeypatch):
    events = [e for i in range(5) for e in _turn_events(f"t{i}", i * 10 + 1, f"prompt {i}")]
    view = apply_many(initial_state("s"), events)
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        timeline = app.query_one(ConversationTimeline)
        await timeline.set_view(view)
        await pilot.pause()
        calls: list[str] = []
        original = TurnWidget._reconcile_turn

        async def counted(self, turn, *args, **kwargs):
            calls.append(turn.id)
            return await original(self, turn, *args, **kwargs)

        monkeypatch.setattr(TurnWidget, "_reconcile_turn", counted)
        await timeline.set_view(view)
        assert calls == []
        # A new turn reconciles only itself; the shared earlier turns are skipped.
        grown = apply_many(view, _turn_events("t5", 61, "prompt 5")[:3])
        await timeline.set_view(grown)
        assert calls == ["t5"]


@pytest.mark.asyncio
async def test_presence_events_do_not_resync_the_timeline():
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        syncs = 0
        original = app._sync_timeline

        async def counted() -> None:
            nonlocal syncs
            syncs += 1
            await original()

        app._sync_timeline = counted
        for seq, kind in enumerate(("presence.joined", "presence.changed"), 100):
            handled = app._post_event(Event(type=kind, data={"viewers": 2}, seq=seq, session="s"))
            await asyncio.wait_for(handled, 5)
        assert syncs == 0
        handled = app._post_event(Event(type="turn.started", data={}, seq=200, session="s", turn="live"))
        await asyncio.wait_for(handled, 5)
        assert syncs == 1
