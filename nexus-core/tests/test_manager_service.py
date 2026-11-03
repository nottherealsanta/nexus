"""The Manager end to end with the scripted model: calls, holders, events, turns, errors, episodes."""
from __future__ import annotations

import asyncio
import time

import pytest
from manager_seed import seed
from manager_support import Harness


@pytest.fixture
async def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.store.set_preference("model.last", "mock/hello")
    yield harness
    await harness.close()


async def test_a_disabled_manager_refuses_to_start_and_says_how_to_turn_it_on(tmp_path, monkeypatch):
    off = Harness(tmp_path, monkeypatch, enabled=False)
    try:
        stream = off.open_stream()
        response = await off.client.post("/api/manager/start", json={"stream_id": stream.stream_id, "client_id": "tui"})
        assert response.status_code == 409 and response.json()["code"] == "manager_disabled"
        status = (await off.client.get("/api/manager")).json()
        assert status["call"] is None and status["enabled"] is False and status["model"] == "copilot/gpt-6-luna"
        assert status["consent"]["provider"] == "copilot"
    finally:
        await off.close()


async def test_start_returns_a_call_and_status_restores_it_without_secrets(h):
    h.project()
    stream = h.open_stream()
    started = await h.start("tui")
    assert started["call_id"].startswith("call_") and started["holder"] == "tui"
    status = (await h.client.get("/api/manager")).json()
    assert status["call"] == {"call_id": started["call_id"], "holder": "tui", "phase": "idle", "error": None, "card": None}
    assert stream.stream_id not in str(status) and stream.stream_id not in str(h.events)
    assert [e["data"]["active"] for e in h.of("manager.state")] == [True]


async def test_starting_again_from_the_same_stream_is_the_same_call(h):
    h.open_stream()
    first = await h.start()
    assert (await h.start())["call_id"] == first["call_id"]


async def test_starting_on_another_client_moves_the_call_and_the_old_holder_is_locked_out(h):
    old, new = h.open_stream(), h.open_stream([])
    first = await h.start("tui", old)
    second = await h.start("nsh", new)
    assert second["call_id"] != first["call_id"] and second["holder"] == "nsh"
    states = h.of("manager.state")
    assert [s["data"]["call_id"] for s in states][-1] == second["call_id"]
    refused = await h.client.post("/api/manager/say", json={"stream_id": old.stream_id, "text": "hi", "source": "typed"})
    assert refused.status_code == 403 and refused.json()["code"] == "not_holder"
    assert old.stream_id not in refused.text and "tui" not in refused.text


async def test_every_stream_taking_route_is_holder_only_and_echoes_no_identifier(h):
    h.open_stream()
    await h.start()
    stranger = h.open_stream([])
    body = {"stream_id": stranger.stream_id}
    for path, extra in [("stop", {}), ("say", {"text": "x", "source": "typed"}), ("cancel", {}),
                        ("activity", {"listening": True, "speaking": False}),
                        ("card/dismiss", {"card_id": "card_1"}),
                        ("navigate/result", {"request_id": "nav_1", "ok": True})]:
        response = await h.client.post(f"/api/manager/{path}", json={**body, **extra})
        assert response.status_code == 403, path
        assert stranger.stream_id not in response.text
    assert h.manager.call.active  # a stranger's stop did nothing


async def test_start_needs_a_connected_stream(h):
    response = await h.client.post("/api/manager/start", json={"stream_id": "stream_nope", "client_id": "tui"})
    assert response.status_code == 409 and response.json()["code"] == "stream_closed"
    assert "stream_nope" not in response.text


async def test_the_holder_closing_its_stream_ends_the_call_but_not_a_session(h):
    project = h.project()
    session = h.rt.create_session(project, title="work", model="mock/hello", permission=None)
    stream = h.open_stream()
    await h.start()
    stream.close()
    for _ in range(100):
        if not h.manager.call.active:
            break
        await asyncio.sleep(0.02)
    assert not h.manager.call.active
    assert h.rt.store.session(session.id) is not None
    assert (await h.client.get("/api/manager")).json()["call"] is None


async def test_stop_ends_the_call_with_a_state_event_and_never_touches_sessions(h):
    project = h.project()
    seed(h.store, project, title="Keep me", user="hello", answer="hi", now=time.time())
    h.open_stream()
    started = await h.start()
    response = await h.client.post("/api/manager/stop", json={"stream_id": h.stream.stream_id})
    assert response.status_code == 204
    await asyncio.sleep(0.05)
    last = h.of("manager.state")[-1]["data"]
    assert last["call_id"] == started["call_id"] and last["active"] is False
    assert len(h.store.sessions()) == 1


async def test_a_typed_line_is_captioned_and_the_answer_arrives_as_cards_and_speech(h):
    project = h.project()
    seed(h.store, project, title="Auth refactor", user="refactor", answer="done", now=time.time(), age_days=0.1)
    h.open_stream()
    await h.start()
    posted = await h.say("what's going on?")
    caption = h.of("manager.caption")[0]["data"]
    assert caption["role"] == "user" and caption["text"] == "what's going on?" and caption["turn_id"] == posted["turn_id"]
    card = h.card()
    assert card["type"] == "board" and card["focused"] is True and card["id"].startswith("card_")
    assert [r["handle"] for r in card["columns"]["finished"]] == ["auth-refactor"]
    assert h.spoken() == ["I put it on the screen."]
    speech = h.of("manager.speech")[0]["data"]
    assert speech["turn_id"] == posted["turn_id"] and speech["seq"] == 0 and speech["urgent"] is False
    phases = [e["data"]["phase"] for e in h.of("manager.state")]
    assert phases[:3] == ["idle", "thinking", "replying"] and phases[-1] == "idle"
    assert (await h.client.get("/api/manager")).json()["call"]["card"]["id"] == card["id"]


async def test_spoken_source_is_not_captioned_back_but_is_the_users_words(h):
    h.open_stream()
    await h.start()
    await h.say("what's going on?", source="speech")
    assert h.of("manager.caption") == []
    assert h.card()["type"] == "board"


async def test_bad_input_is_rejected(h):
    h.open_stream()
    await h.start()
    for body in ({"text": "", "source": "typed"}, {"text": "x", "source": "voice"}, {"text": "x" * 5000, "source": "typed"},
                 {"text": "x", "source": "wake"}):
        response = await h.client.post("/api/manager/say", json={"stream_id": h.stream.stream_id, **body})
        assert response.status_code == 400, body


async def test_speech_streams_sentence_by_sentence(h, monkeypatch):
    from nexus import runtime as runtime_module
    from nexus.llmhub.types import Done, PartDone, Text, TextDelta

    class Chatty:
        name = "scripted"

        async def stream(self, req):
            text = "First sentence. Second one here! And a third?"
            for i in range(0, len(text), 7):
                yield TextDelta(text[i:i + 7])
                await asyncio.sleep(0)
            yield PartDone(Text(text))
            yield Done("end_turn")

    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (Chatty(), "m"))
    h.open_stream()
    await h.start()
    await h.say("hello there")
    assert h.spoken() == ["First sentence.", "Second one here!", "And a third?"]
    assert [e["data"]["seq"] for e in h.of("manager.speech")] == [0, 1, 2]


async def test_an_unavailable_model_fails_visibly_and_never_falls_back(tmp_path, monkeypatch):
    bad = Harness(tmp_path, monkeypatch, model="copilot/gpt-6-luna")
    try:
        bad.open_stream()
        await bad.start()
        await bad.say("hello")
        state = bad.of("manager.state")[-1]["data"]
        assert state["error"]["code"] == "model_unavailable" and "copilot" in state["error"]["message"]
        assert state["error"]["fix"]
        captions = [e["data"] for e in bad.of("manager.caption") if e["data"]["role"] == "manager"]
        assert captions and captions[0]["final"] and "not logged in" in captions[0]["text"]
        assert bad.spoken() == []
        # The error is on the call until a turn succeeds, and GET restores it.
        assert (await bad.client.get("/api/manager")).json()["call"]["error"]["code"] == "model_unavailable"
        # No fallback: the episode's model is exactly what the config says.
        assert bad.manager.current_episode().model == "copilot/gpt-6-luna"
    finally:
        await bad.close()


async def test_a_later_good_turn_clears_the_error(tmp_path, monkeypatch):
    bad = Harness(tmp_path, monkeypatch, model="copilot/gpt-6-luna")
    try:
        bad.open_stream()
        await bad.start()
        await bad.say("hello")
        from manager_support import enable_manager

        enable_manager(bad.home, "mock/manager_demo")
        await bad.say("what's going on?")
        assert bad.of("manager.state")[-1]["data"]["error"] is None
        assert bad.manager.current_episode().model == "mock/manager_demo"
    finally:
        await bad.close()


async def test_cancel_before_any_side_effect_interrupts_the_turn(h, monkeypatch):
    from nexus import runtime as runtime_module
    from nexus.llmhub.types import Done, PartDone, Text, TextDelta

    gate = asyncio.Event()

    class Slow:
        name = "scripted"

        async def stream(self, req):
            yield TextDelta("Thinking about it. ")
            await gate.wait()
            yield TextDelta("Never said.")
            yield PartDone(Text("Thinking about it. Never said."))
            yield Done("end_turn")

    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (Slow(), "m"))
    h.open_stream()
    await h.start()
    await h.say("go on", wait=False)
    for _ in range(100):
        if h.spoken():
            break
        await asyncio.sleep(0.02)
    response = await h.client.post("/api/manager/cancel", json={"stream_id": h.stream.stream_id})
    assert response.json()["cancelled"] is True and response.json()["muted"] is True
    await h.settle()
    assert h.spoken() == ["Thinking about it."]
    episode = h.manager.current_episode()
    assert h.store.turns_for_session(episode.id)[-1]["status"] == "interrupted"


async def test_cancel_after_an_act_mutes_speech_but_lets_the_turn_finish(h):
    project = h.project()
    seed(h.store, project, title="Flaky test", user="x", answer="y", now=time.time())
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    h.manager.turn = None
    from nexus.manager.turn import TurnState

    turn = TurnState("mt_x", "typed", "tell it hi", index=9)
    turn.acted = True
    h.manager.turn = turn
    result = await h.manager.cancel(h.stream.stream_id)
    assert result["cancelled"] is False and result["muted"] is True and "keeps going" in result["reason"]
    assert turn.muted is True
    h.manager.turn = None


async def test_activity_and_card_dismiss_are_accepted_from_the_holder(h):
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    card = h.card()
    ok = await h.client.post("/api/manager/activity", json={"stream_id": h.stream.stream_id, "listening": True, "speaking": False})
    assert ok.status_code == 204 and h.manager.call.listening is True
    stale = await h.client.post("/api/manager/card/dismiss", json={"stream_id": h.stream.stream_id, "card_id": "card_old"})
    assert stale.status_code == 204 and h.manager.stage.card is not None  # a stale id changes nothing
    done = await h.client.post("/api/manager/card/dismiss", json={"stream_id": h.stream.stream_id, "card_id": card["id"]})
    await asyncio.sleep(0.05)
    assert done.status_code == 204 and h.card() is None and h.manager.stage.card is None
    bad = await h.client.post("/api/manager/activity", json={"stream_id": h.stream.stream_id, "listening": "yes", "speaking": False})
    assert bad.status_code == 400


async def test_the_next_delta_tells_the_manager_the_user_closed_the_card(h):
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    await h.client.post("/api/manager/card/dismiss", json={"stream_id": h.stream.stream_id, "card_id": h.card()["id"]})
    await h.say("okay")
    episode = h.manager.current_episode()
    last_user = [m for m in h.store.messages(episode.id) if m.message.role == "user"][-1]
    assert "The user closed the board card you showed." in last_user.message.parts[0].text


async def test_first_turn_has_a_snapshot_and_later_turns_only_a_delta(h):
    project = h.project()
    first = seed(h.store, project, title="Docs pass", user="x", answer="y", now=time.time(), age_days=0.2)
    h.open_stream()
    await h.start()
    await h.say("hello")
    await h.say("hello again")
    h.rt.errors.clear()
    h.store.update_session(first.id, title="Docs pass renamed")
    episode = h.manager.current_episode()
    users = [m.message.parts[0].text for m in h.store.messages(episode.id) if m.message.role == "user"]
    assert "State of Nexus now" in users[0] and "docs-pass" in users[0]
    assert "State of Nexus now" not in users[1] and "<nexus_state>" not in users[1]
    assert users[1].rstrip().endswith("</user_said>")


async def test_an_idle_episode_rolls_over_with_a_recap_and_continued_from(h):
    h.project()
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    first = h.manager.current_episode()
    h.manager.clock = lambda: time.time() + 31 * 60
    h.manager.narrator = lambda material: _async("They looked at the board.")
    await h.say("what's going on?")
    second = h.manager.current_episode()
    assert second.id != first.id and second.continued_from == first.id and second.kind == "manager"
    opening = h.store.messages(second.id)[0].message.parts[0].text
    assert "<manager_recap>" in opening and "They looked at the board." in opening
    assert "User: what's going on?" in opening and "State of Nexus now" in opening
    assert [e.id for e in h.manager.episodes()] == [second.id, first.id]


async def test_a_failed_narrative_still_rolls_over_with_the_deterministic_recap(h):
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    h.manager.clock = lambda: time.time() + 31 * 60

    async def broken(material):
        raise RuntimeError("model down")

    h.manager.narrator = broken
    await h.say("what's going on?")
    opening = h.store.messages(h.manager.current_episode().id)[0].message.parts[0].text
    assert "Last 1 exchanges, verbatim" in opening


async def test_an_oversize_episode_rolls_over(tmp_path, monkeypatch):
    big = Harness(tmp_path, monkeypatch, max_episode_tokens=2000)
    try:
        big.open_stream()
        await big.start()
        await big.say("what's going on?")
        first = big.manager.current_episode()
        monkeypatch.setattr(big.manager, "_episode_tokens", lambda episode: 5000)
        big.manager.narrator = lambda material: _async("")
        await big.say("what's going on?")
        assert big.manager.current_episode().continued_from == first.id
    finally:
        await big.close()


async def test_a_new_call_does_not_start_a_new_episode(h):
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    first = h.manager.current_episode()
    await h.client.post("/api/manager/stop", json={"stream_id": h.stream.stream_id})
    second_stream = h.open_stream([])
    await h.start("tui", second_stream)
    await h.say("what's going on?", stream=second_stream)
    assert h.manager.current_episode().id == first.id


async def test_a_wake_turn_cannot_act_and_says_so(h):
    project = h.project()
    seed(h.store, project, title="Docs pass", user="x", answer="y", now=time.time())
    h.open_stream()
    await h.start()
    await h.say("what's going on?")
    h.manager.wake("start fix the dictation cue in nexus")
    await h.settle()
    assert h.manager.tools  # tools exist, and the turn that ran had no authority
    audit = h.store.audit()
    assert audit and audit[0]["tool"] == "start_session" and audit[0]["ok"] is False
    assert len(h.store.sessions()) == 1
    assert any("notification" in (r["result"] or "") for r in audit)


async def _async(value):
    return value
