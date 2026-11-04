"""The Manager's read-only context modal (docs/manager.md): what Core returns for both clients."""
from __future__ import annotations

import time

from manager_support import Harness

from nexus.llmhub.types import Message, Text


async def test_with_no_episode_it_says_so_instead_of_failing(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    try:
        h.open_stream()
        response = await h.client.get("/api/manager/context")
        assert response.status_code == 200
        body = response.json()
        assert body["episode"] is None and body["episodes"] == []
        assert "no episode yet" in body["document"]
        assert body["title"] == "Manager context"
    finally:
        await h.close()


async def test_it_shows_the_episode_recap_state_and_the_recent_log(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    try:
        h.open_stream()
        await h.start()
        await h.say("what's going on?")
        body = (await h.client.get("/api/manager/context")).json()
        episode = h.manager.current_episode()
        assert episode is not None
        assert body["episode"]["id"] == episode.id
        assert body["episodes"] == [{"id": episode.id, "created": episode.created, "updated": episode.updated,
                                     "current": True}]
        doc = body["document"]
        for want in ("Episodes", "Recap", "State snapshot and deltas", "State snapshot", "Recent log",
                     "what's going on?", "Tool overview", "result:", episode.id):
            assert want in doc, f"missing {want!r}\n{doc}"
        # The raw turn input is not shown as the user's words in the log: only the utterance is.
        assert "<user_said" not in doc and "<nexus_state>" not in doc
    finally:
        await h.close()


async def test_long_logs_and_blocks_announce_what_was_cut(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    try:
        h.open_stream()
        await h.start()
        await h.say("what's going on?")
        episode = h.manager.current_episode()
        assert episode is not None
        for i in range(60):
            h.store.append_message(episode.id, Message("assistant", (Text(f"long answer {i} " + "x" * 1400),)))
        body = (await h.client.get("/api/manager/context")).json()
        doc = body["document"]
        assert "earlier not shown" in doc
        assert "Clipped:" in doc
        assert body["clipped"]["log"] > 0
        assert body["clipped"]["entries"] > 0
    finally:
        await h.close()


async def test_a_new_episode_shows_the_recap_and_the_earlier_episode(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, idle_episode_minutes=1)
    try:
        async def narrator(material: str) -> str:
            return "The user asked what is going on."

        h.manager.narrator = narrator
        h.open_stream()
        await h.start()
        await h.say("note")
        h.manager.clock = lambda: time.time() + 120  # the episode is now older than the idle window
        await h.say("note")
        body = (await h.client.get("/api/manager/context")).json()
        assert len(body["episodes"]) == 2
        assert body["episode"]["continued_from"] == body["episodes"][1]["id"]
        doc = body["document"]
        assert "Recap of the previous conversation" in doc
        assert "The user asked what is going on." in doc
        assert "earlier: 1" in doc
    finally:
        await h.close()
