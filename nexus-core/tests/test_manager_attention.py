"""M4 Core: attention (MANAGER_PLAN.md section 8, 10.1). Deterministic lines, no model, no authority, no navigation."""
from __future__ import annotations

import asyncio
import time

import pytest
from manager_seed import seed
from manager_support import Harness, Pending, until

from nexus.manager.attention import APPROVAL
from nexus.manager.turn import TurnState
from nexus.tools.base import ToolError


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
async def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.store.set_preference("model.last", "mock/hello")
    harness.clock = Clock()
    harness.manager.clock = harness.clock
    harness.manager.attention.auto = False  # the tests drive `process` themselves
    harness.pending = Pending(harness)
    yield harness
    await harness.close()


def session(h, title, age=0.1):
    return seed(h.store, h.project(), title=title, user="u", answer="a", now=time.time(), age_days=age)


def proactive(h):
    return [e["data"] for e in h.of("manager.speech") if e["data"].get("proactive")]


async def run(h, **kwargs):
    result = h.manager.attention.process(**kwargs)
    await asyncio.sleep(0.05)
    return result


async def begin(h):
    h.open_stream()
    await h.start()


async def test_an_approval_is_said_once_with_an_unfocused_card_and_no_orb_flag(h):
    s = session(h, "Auth refactor")
    await begin(h)
    _aid, _, _request = h.pending.approval(s)
    h.manager.attention.ingest(h.rt.store.events(s.id, types=["permission.asked"])[-1])
    await run(h)
    assert proactive(h) == []  # coalescing window: a second approval may still arrive
    h.clock.advance(1.5)
    assert await run(h) is True
    await asyncio.sleep(0.05)
    lines = proactive(h)
    assert [l["text"] for l in lines] == ["The auth refactor wants to run a command.", "Allow it?"]
    assert lines[0]["turn_id"].startswith("mp_") and lines[0]["urgent"] is False and [l["seq"] for l in lines] == [0, 1]
    card = h.card()
    assert card["type"] == "approval" and card["focused"] is False and card["detail"] == "alembic upgrade head"
    assert h.of("manager.navigate") == []
    h.clock.advance(60)
    assert await run(h) is False  # said once


async def test_two_approvals_become_one_line_and_one_card(h):
    a, b = session(h, "Auth refactor"), session(h, "Flaky test")
    await begin(h)
    h.pending.approval(a)
    q = h.pending.question(b)
    for e in h.rt.store.events(a.id, types=["permission.asked"]) + h.rt.store.events(b.id, types=["question.asked"]):
        h.manager.attention.ingest(e)
    h.clock.advance(2)
    await run(h)
    await asyncio.sleep(0.05)
    text = " ".join(l["text"] for l in proactive(h))
    assert text.startswith("Two things need you:") and "auth refactor wants to run a command" in text
    assert "flaky test has a question: Which database should the test use?" in text
    assert h.card()["type"] == "approval"  # one card at a time: the first
    assert q in h.manager.presented  # both count as put in front of the user


async def test_coalesced_items_not_named_remain_unpresented_until_the_next_line(h):
    sessions = [session(h, f"Task {name}") for name in ("alpha", "beta", "gamma", "delta")]
    await begin(h)
    approvals = []
    for s in sessions:
        aid, future, _ = h.pending.approval(s)
        approvals.append((aid, future))
        h.manager.attention.ingest(h.rt.store.events(s.id, types=["permission.asked"])[-1])
    h.clock.advance(2)
    assert await run(h)
    assert "and 1 more" in " ".join(line["text"] for line in proactive(h))
    assert all(aid in h.manager.presented for aid, _ in approvals[:3])
    aid, future = approvals[3]
    assert aid not in h.manager.presented
    h.manager.call.turns += 1
    h.manager.turn = TurnState("mt_u", "typed", "yes task delta", index=h.manager.call.turns)
    with pytest.raises(ToolError, match="has not been shown"):
        await h.manager.tools.decide_approval({"handle": h.manager.retriever.handle_for(sessions[3]),
                                               "approval": aid, "allow": True})
    assert not future.done()
    h.manager.turn = None
    h.clock.advance(21)
    assert await run(h)
    assert aid in h.manager.presented
    assert "task delta" in " ".join(line["text"].lower() for line in proactive(h))


async def test_at_most_one_line_per_interval_and_the_interval_is_configurable(h):
    a, b = session(h, "Auth refactor"), session(h, "Flaky test")
    await begin(h)
    h.pending.approval(a)
    h.manager.attention.ingest(h.rt.store.events(a.id, types=["permission.asked"])[-1])
    h.clock.advance(2)
    await run(h)
    h.pending.approval(b)
    h.manager.attention.ingest(h.rt.store.events(b.id, types=["permission.asked"])[-1])
    h.clock.advance(5)
    assert await run(h) is False  # 5 s after the last line, interval is 20
    h.clock.advance(16)
    assert await run(h) is True
    assert len({l["turn_id"] for l in proactive(h)}) == 2


async def test_nothing_is_said_while_the_user_is_listening_and_it_goes_when_they_stop(h):
    s = session(h, "Auth refactor")
    await begin(h)
    await h.client.post("/api/manager/activity", json={"stream_id": h.stream.stream_id, "listening": True, "speaking": False})
    h.pending.approval(s)
    h.manager.attention.ingest(h.rt.store.events(s.id, types=["permission.asked"])[-1])
    h.clock.advance(5)
    assert await run(h) is False and proactive(h) == []
    await h.client.post("/api/manager/activity", json={"stream_id": h.stream.stream_id, "listening": False, "speaking": False})
    await asyncio.sleep(0.05)
    assert proactive(h)  # activity going quiet released the held line


async def test_while_the_client_plays_speech_only_priority_one_goes(h):
    s = session(h, "Auth refactor")
    await begin(h)
    h.manager.call.speaking = True
    s_fail = session(h, "Broken build")
    h.manager.attention.ingest({"type": "session.error", "session_id": s_fail.id, "data": {"kind": "provider", "message": "x"}})
    h.clock.advance(10)
    assert await run(h) is False
    h.pending.approval(s)
    h.manager.attention.ingest(h.rt.store.events(s.id, types=["permission.asked"])[-1])
    h.clock.advance(2)
    assert await run(h) is True
    assert "auth refactor" in proactive(h)[0]["text"]


async def test_mid_reply_only_priority_one_and_only_at_a_sentence_break(h):
    s, other = session(h, "Auth refactor"), session(h, "Broken build")
    await begin(h)
    h.manager.turn = TurnState("mt_1", "typed", "hello", index=1)  # the Manager is replying
    h.pending.approval(s)
    h.manager.attention.ingest(h.rt.store.events(s.id, types=["permission.asked"])[-1])
    h.manager.attention.ingest({"type": "session.error", "session_id": other.id, "data": {"kind": "provider"}})
    h.clock.advance(10)
    assert await run(h) is False  # never in the middle of a sentence
    assert await run(h, sentence_break=True) is True
    await asyncio.sleep(0.05)
    lines = proactive(h)
    assert all(l["urgent"] is True for l in lines) and "auth refactor" in lines[0]["text"]
    h.clock.advance(60)
    assert await run(h, sentence_break=True) is False  # the failure waits for the reply to end
    h.manager.turn = None
    assert await run(h) is True
    assert "broken build stopped with an error" in proactive(h)[-1]["text"].lower()


async def test_proactive_levels(h):
    from manager_support import enable_manager

    a = session(h, "Auth refactor")
    await begin(h)
    finished = [session(h, f"Done {i}") for i in range(3)]
    for f in finished:
        turn = h.store.turns_for_session(f.id)[-1]
        h.manager.attention.ingest({"type": "session.status", "session_id": f.id,
                                    "data": {"status": "idle", "turn_id": turn["id"]}})
    h.clock.advance(30)
    h.manager.last_activity = h.clock() - 100
    assert await run(h) is False  # `attention` level: finishes are not spoken
    enable_manager(h.home, "mock/manager_demo", proactive="all")
    assert await run(h) is True
    assert proactive(h)[-1]["text"] == "Three sessions finished."
    enable_manager(h.home, "mock/manager_demo", proactive="off")
    h.pending.approval(a)
    h.manager.attention.ingest(h.rt.store.events(a.id, types=["permission.asked"])[-1])
    h.clock.advance(60)
    assert await run(h) is False


async def test_a_finish_waits_for_a_lull(h):
    from manager_support import enable_manager

    await begin(h)
    enable_manager(h.home, "mock/manager_demo", proactive="all")
    f = session(h, "Docs pass")
    turn = h.store.turns_for_session(f.id)[-1]
    h.manager.attention.ingest({"type": "session.status", "session_id": f.id, "data": {"status": "idle", "turn_id": turn["id"]}})
    h.manager.last_activity = h.clock()
    h.clock.advance(9)
    assert await run(h) is True  # 9 s of quiet is past the 8 s lull and the 8 s window
    assert proactive(h)[-1]["text"] == "The docs pass finished."


async def test_an_item_answered_before_it_is_said_is_dropped(h):
    s = session(h, "Auth refactor")
    await begin(h)
    aid, _future, _ = h.pending.approval(s)
    h.manager.attention.ingest(h.rt.store.events(s.id, types=["permission.asked"])[-1])
    h.rt.approvals.pop(aid)  # answered in the session itself
    h.clock.advance(5)
    assert await run(h) is False and proactive(h) == []


async def test_a_user_interruption_and_the_managers_own_failures_are_not_announced(h):
    s = session(h, "Auth refactor")
    await begin(h)
    h.manager.attention.ingest({"type": "session.error", "session_id": s.id, "data": {"kind": "interrupted"}})
    episode = h.manager._new_episode("mock/manager_demo")
    h.manager.attention.ingest({"type": "session.error", "session_id": episode.id, "data": {"kind": "provider"}})
    h.clock.advance(10)
    assert h.manager.attention.items == {} and await run(h) is False


async def test_a_barge_in_holds_lines_for_one_interval(h):
    s = session(h, "Auth refactor")
    await begin(h)
    h.pending.approval(s)
    h.manager.attention.ingest(h.rt.store.events(s.id, types=["permission.asked"])[-1])
    h.clock.advance(2)
    await h.client.post("/api/manager/cancel", json={"stream_id": h.stream.stream_id})
    assert await run(h) is False
    h.clock.advance(21)
    assert await run(h) is True


async def test_a_focused_card_is_never_replaced_by_a_proactive_one(h):
    s = session(h, "Auth refactor")
    await begin(h)
    await h.say("note")
    focused = h.card()
    assert focused["focused"] is True
    h.pending.approval(s)
    h.manager.attention.ingest(h.rt.store.events(s.id, types=["permission.asked"])[-1])
    h.clock.advance(5)
    await run(h)
    await asyncio.sleep(0.05)
    assert proactive(h) and h.card()["id"] == focused["id"]


async def test_an_unprompted_line_carries_no_authority_and_the_next_user_yes_can_use_it(h):
    s = session(h, "Auth refactor")
    await begin(h)
    aid, future, _ = h.pending.approval(s)
    h.manager.attention.ingest(h.rt.store.events(s.id, types=["permission.asked"])[-1])
    h.clock.advance(2)
    await run(h)
    handle = h.manager.retriever.handle_for(s)
    tools = h.manager.tools
    # A wake (no user words) cannot act on it, even though the item was presented.
    h.manager.call.turns += 1
    h.manager.turn = TurnState("mt_w", "wake", "approve it", index=h.manager.call.turns)
    with pytest.raises(ToolError, match="notification"):
        await tools.decide_approval({"handle": handle, "approval": aid, "allow": True})
    # A user turn answering in the same count as the presentation would be refused; the next one is fine.
    known = h.manager.presented[aid]
    h.manager.turn = TurnState("mt_u", "typed", "yes", index=known[1])
    with pytest.raises(ToolError, match="has not answered since"):
        await tools.decide_approval({"handle": handle, "approval": aid, "allow": True})
    h.manager.turn = TurnState("mt_u2", "speech", "yes", index=known[1] + 1)
    assert "Allowed once" in await tools.decide_approval({"handle": handle, "approval": aid, "allow": True})
    assert future.result() is True


async def test_the_managers_context_records_what_was_said_and_shown_unprompted(h):
    s = session(h, "Auth refactor")
    await begin(h)
    h.pending.approval(s)
    h.manager.attention.ingest(h.rt.store.events(s.id, types=["permission.asked"])[-1])
    h.clock.advance(2)
    await run(h)
    audit = h.store.audit(tool="proactive")
    assert audit and audit[0]["target"] == "auth-refactor" and "wants to run a command" in audit[0]["result"]
    await h.say("hello")
    episode = h.manager.current_episode()
    first = h.store.messages(episode.id)[0].message.parts[0].text
    assert 'Unprompted, Nexus said aloud: "The auth refactor wants to run a command. Allow it?"' in first
    assert "unfocused approval card for the auth refactor" in first
    await h.say("hello again")
    second = [m for m in h.store.messages(episode.id) if m.message.role == "user"][-1].message.parts[0].text
    assert "Unprompted" not in second  # told once


async def test_joining_a_call_opens_with_what_is_pending_and_silence_when_nothing_is(tmp_path, monkeypatch):
    quiet = Harness(tmp_path / "a", monkeypatch)
    (tmp_path / "a").mkdir(exist_ok=True)
    try:
        quiet.open_stream()
        await quiet.start()
        await asyncio.sleep(0.2)
        assert [e for e in quiet.of("manager.speech")] == []
    finally:
        await quiet.close()
    busy = Harness(tmp_path / "b", monkeypatch)
    try:
        s = seed(busy.store, busy.project(), title="Auth refactor", user="u", answer="a", now=time.time())
        Pending(busy).approval(s)
        busy.open_stream()
        await busy.start()
        await until(lambda: busy.of("manager.speech"), what="the opening line")
        assert busy.of("manager.speech")[0]["data"]["text"] == "The auth refactor wants to run a command."
        assert busy.of("manager.speech")[0]["data"]["proactive"] is True
        assert busy.card()["focused"] is False
        assert busy.manager.attention.items and next(iter(busy.manager.attention.items.values())).kind == APPROVAL
    finally:
        await busy.close()


async def test_the_background_loop_reacts_to_bus_events_end_to_end(tmp_path, monkeypatch):
    live = Harness(tmp_path, monkeypatch, proactive_interval_seconds=1)
    try:
        live.manager.attention.windows = {1: 0.05, 2: 0.05, 3: 0.05}
        s = seed(live.store, live.project(), title="Auth refactor", user="u", answer="a", now=time.time())
        live.open_stream()
        await live.start()
        Pending(live).approval(s)
        await until(lambda: live.of("manager.speech"), what="a line from the loop")
        assert "auth refactor" in live.of("manager.speech")[0]["data"]["text"]
        assert live.of("manager.navigate") == []
    finally:
        await live.close()


async def test_session_text_in_a_spoken_question_is_redacted(h):
    s = session(h, "Auth refactor")
    await begin(h)
    qid = h.pending.question(s, "Use key sk-abcdefghijklmnopqrstuvwxyz0123456789 for the test?")
    h.manager.attention.ingest(h.rt.store.events(s.id, types=["question.asked"])[-1])
    h.clock.advance(2)
    await run(h)
    assert qid in h.manager.presented
    spoken = [e["data"]["text"] for e in h.of("manager.speech")]
    assert spoken and all("sk-abc" not in text for text in spoken) and "[redacted]" in " ".join(spoken)
