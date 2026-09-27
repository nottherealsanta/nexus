"""Regression coverage for overlapping TUI session bootstraps."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nexus.events import Event
from nexus.ui.tui.controller import TuiController


class DeferredBootstrapHost:
    def __init__(self) -> None:
        self.a_stream_entered = asyncio.Event()
        self.release_a_stream = asyncio.Event()
        self.closed_streams: list[str] = []

    async def handshake(self) -> None:
        return None

    async def open_session(self, session: str) -> str:
        return session

    async def state(self, session: str, _from_seq: int = 0):
        return {}, 9 if session == "A" else 2

    async def current_agent(self, session: str):
        return SimpleNamespace(name=f"agent-{session}", source="host")

    def stream(self, session: str, _from_seq: int = 0, *, follow: bool = True):
        assert follow is False

        async def events():
            try:
                if session == "A":
                    self.a_stream_entered.set()
                    await self.release_a_stream.wait()
                    yield Event(
                        "turn.started", seq=9, session="A", turn="turn-A"
                    )
                else:
                    yield Event(
                        "turn.started", seq=2, session="B", turn="turn-B"
                    )
            finally:
                self.closed_streams.append(session)

        return events()


@pytest.mark.asyncio
async def test_late_bootstrap_replay_cannot_replace_switched_session_state():
    host = DeferredBootstrapHost()
    controller = TuiController(host, "A")  # type: ignore[arg-type]

    bootstrap_a = asyncio.create_task(controller.bootstrap())
    await host.a_stream_entered.wait()

    await controller.switch_session("B")
    await controller.bootstrap()
    assert controller.session == "B"
    assert controller.view.session_id == "B"
    assert controller.view.turns[0].id == "turn-B"
    assert controller.cursor == 2
    assert controller.agent_name == "agent-B"

    host.release_a_stream.set()
    await bootstrap_a

    assert controller.session == "B"
    assert controller.view.session_id == "B"
    assert [turn.id for turn in controller.view.turns] == ["turn-B"]
    assert controller.cursor == 2
    assert controller.agent_name == "agent-B"
    assert sorted(host.closed_streams) == ["A", "B"]


@pytest.mark.asyncio
async def test_single_bootstrap_commits_replayed_view_cursor_and_metadata():
    host = DeferredBootstrapHost()
    controller = TuiController(host, "B")  # type: ignore[arg-type]

    result = await controller.bootstrap()

    assert result == ("", "B")
    assert controller.session == "B"
    assert controller.view.session_id == "B"
    assert [turn.id for turn in controller.view.turns] == ["turn-B"]
    assert controller.cursor == controller.view.last_seq == 2
    assert controller.agent_name == "agent-B"
    assert host.closed_streams == ["B"]
