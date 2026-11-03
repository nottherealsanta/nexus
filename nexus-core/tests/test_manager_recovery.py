"""A daemon restart in the middle of a Manager turn recovers like any session's, and the episode stays hidden."""
from __future__ import annotations

from manager_support import Harness

from nexus.llmhub.types import Message, Text
from nexus.server import make_runtime


async def test_a_crash_mid_turn_is_recovered_and_the_episode_stays_hidden(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    try:
        episode = h.manager._new_episode("mock/manager_demo")
        h.store.append_message(episode.id, Message("user", (Text("<user_said source=\"typed\">\nhello\n</user_said>"),)))
        turn = h.store.start_turn(episode.id)  # the daemon dies here: the turn is still `running`
        restarted = make_runtime(h.store)
        assert restarted.recovered or h.store.turn(turn["id"])["status"] != "running"
        assert h.store.turn(turn["id"])["status"] in ("interrupted", "failed", "completed")
        assert h.store.sessions() == [] and restarted.manager.current_episode().id == episode.id
        assert restarted.manager.handles.snapshot() == {}
        await restarted.shutdown()
    finally:
        await h.close()
