"""The activity bar spends timer work only on active animation."""

from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client

from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.widgets import ActivityProgress


@pytest.mark.asyncio
async def test_activity_timer_stops_when_idle():
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        bar = app.query_one(ActivityProgress)
        assert bar._timer is None
        bar.set_state(used=75, budget=100)
        assert bar._timer is None
        assert "━" in bar.render().plain
        bar.set_state(running=True, color="#f00")
        assert bar._timer is not None
        bar.set_state(running=False)
        assert bar._timer is None


@pytest.mark.asyncio
async def test_terminal_event_is_handled_with_running_cleared():
    """A failed turn must stop the activity animation on its final sync."""
    from nexus.events import Event
    from nexus.ui.tui.controller import TuiController

    controller = TuiController(_client(FakeTransport()), "s")
    seen: list[tuple[str, bool]] = []

    async def events():
        yield Event(type="turn.started", data={}, seq=1, ts=1.0, session="s", turn="t")
        yield Event(type="turn.failed", data={"error": "HTTP 429"}, seq=2, ts=2.0, session="s", turn="t")

    def post(event):
        seen.append((event.type, controller.running))

    controller.running = True
    await controller._consume("s", events(), post)
    assert seen == [("turn.started", True), ("turn.failed", False)]
    assert controller.running is False


@pytest.mark.asyncio
async def test_bar_stops_once_the_view_is_idle_even_if_the_stream_lingers():
    """The view, not the live stream, decides whether the bar animates."""
    from nexus.events import Event
    from nexus.view import apply_many, initial_state

    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        bar = app.query_one(ActivityProgress)
        app.controller.running = True  # a stream that has not wound down
        app.controller.view = apply_many(initial_state("s"), [
            Event(type="turn.started", data={}, seq=1, ts=1.0, session="s", turn="t"),
        ])
        app._sync_activity()
        assert bar._running and bar._timer is not None
        app.controller.view = apply_many(app.controller.view, [
            Event(type="turn.failed", data={"error": "HTTP 429"}, seq=2, ts=2.0, session="s", turn="t"),
        ])
        app._sync_activity()
        assert not bar._running and bar._timer is None
        # Any stream end also resyncs through the controller hook.
        app.controller.view = apply_many(app.controller.view, [
            Event(type="turn.started", data={}, seq=3, ts=3.0, session="s", turn="u"),
        ])
        app._sync_activity()
        assert bar._running
        app.controller.running = False
        app.controller.on_stream_end()
        assert not bar._running and bar._timer is None
