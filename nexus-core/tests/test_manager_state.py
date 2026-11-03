"""The unified session state (MANAGER_PLAN.md section 7): one answer to "what does this session need?"."""
from __future__ import annotations

import asyncio
import time

import pytest
from manager_seed import project as make_project
from manager_seed import seed
from manager_support import Harness

from nexus.manager.state import (
    ATTENTION_STATES,
    PRECEDENCE,
    STATES,
    UNKNOWN,
    SessionFacts,
    SessionState,
    session_state,
    session_states,
    state_map,
)
from nexus.store import Store

NONE = {"pending_approvals": 0, "pending_questions": 0, "escalations": 0, "failed": False, "busy": False}


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / "s.db")
    proj = make_project(store, tmp_path, "p")
    return store, proj


def make(world, archived=False):
    store, proj = world
    return seed(store, proj, title="t", user="u", now=time.time(), archived=archived)


def test_each_fact_wins_in_the_documented_order(world):
    session = make(world)
    everything = {"pending_approvals": 1, "pending_questions": 1, "escalations": 1, "failed": True, "busy": True, "unread": True}
    expected = ["needs_approval", "needs_answer", "blocked", "failed", "working", "finished_unread"]
    for name, state in zip(("pending_approvals", "pending_questions", "escalations", "failed", "busy", "unread"), expected, strict=True):
        assert session_state(session, SessionFacts(**everything)).state == state
        everything[name] = 0 if name not in ("failed", "busy", "unread") else False
    assert session_state(session, SessionFacts(**everything)).state == "idle"
    assert PRECEDENCE == ("needs_approval", "needs_answer", "blocked", "failed", "working", "archived",
                          "finished_unread", "idle")


def test_archive_outranks_unread_but_not_activity(world):
    archived = make(world, archived=True)
    assert session_state(archived, SessionFacts(**NONE, unread=True)).state == "archived"
    assert session_state(archived, SessionFacts(**{**NONE, "busy": True}, unread=True)).state == "working"


def test_a_missing_fact_that_could_outrank_the_answer_is_unknown_never_idle(world):
    session = make(world)
    assert session_state(session).state == UNKNOWN
    assert session_state(session, SessionFacts(**{**NONE, "busy": None}, unread=False)).state == UNKNOWN
    assert session_state(session, SessionFacts(**NONE)).state == UNKNOWN  # unread has no source in Core
    reading = session_state(session, SessionFacts(**NONE, unread=False))
    assert reading.state == "idle" and reading.exact and reading.unavailable == ()
    # A fact above the winner decides first, so a gap below it does not matter.
    assert session_state(session, SessionFacts(pending_approvals=1)).state == "needs_approval"
    assert session_state(make(world, archived=True), SessionFacts(**NONE)).state == "archived"


def test_facts_are_validated():
    for bad in ({"pending_approvals": -1}, {"pending_questions": True}, {"escalations": 1.5}, {"failed": 1}, {"unread": "no"}):
        with pytest.raises(ValueError):
            SessionFacts(**bad)
    with pytest.raises(ValueError):
        SessionState("s", "dreaming", False)


def test_a_reading_describes_itself_for_a_tool_result(world):
    reading = session_state(make(world), SessionFacts(pending_approvals=2))
    data = reading.as_dict()
    assert data["state"] == "needs_approval" and data["needs_attention"] is True and data["exact"] is True
    assert data["facts"]["pending_approvals"] == 2 and "pending_questions" in data["unavailable"]
    assert reading.key == "needs_approval" and ATTENTION_STATES == {"needs_approval", "needs_answer", "blocked"}
    assert UNKNOWN in STATES and "unknown" not in PRECEDENCE


def test_states_for_a_store_use_archive_state_from_the_row_and_map_by_id(world):
    store, _proj = world
    live, gone = make(world), make(world, archived=True)
    states = session_states(store, {live.id: SessionFacts(**NONE, unread=False), gone.id: SessionFacts(**NONE)})
    assert {s: r.state for s, r in states.items()} == {live.id: "idle", gone.id: "archived"}
    assert state_map(states.values()) == {live.id: "idle", gone.id: "archived"}
    assert set(session_states(store, archived="only")) == {gone.id}
    assert set(session_states(store, archived="exclude")) == {live.id}
    assert session_states(store)[live.id].state == UNKNOWN  # no facts supplied: unknown, not idle
    with pytest.raises(ValueError):
        session_states(store, archived="sometimes")


# -- the Runtime-backed facts the Manager uses ------------------------------------------------------

@pytest.fixture
async def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    await harness.close()


async def test_the_manager_rolls_a_childs_wait_up_into_its_root(h):
    project = h.project()
    root = seed(h.store, project, title="Parent", user="u", answer="a", now=time.time())
    child = seed(h.store, project, title="Child", user="u", answer="a", now=time.time(), parent=root)
    future = asyncio.get_running_loop().create_future()
    h.rt.approvals["per_x"] = (child.id, future, {"id": "per_x", "session_id": child.id})
    sessions = h.store.sessions()
    states = h.manager.states(sessions)
    assert states[root.id] == "needs_approval" and states[child.id] == "needs_approval"
    h.rt.approvals.clear()
    h.rt.turns[child.id] = asyncio.ensure_future(asyncio.sleep(30))
    states = h.manager.states(sessions)
    assert states[root.id] == "working" and states[child.id] == "working"
    h.rt.turns.pop(child.id).cancel()
    assert h.manager.states(sessions)[root.id] == "idle"


async def test_a_failed_last_turn_is_failed_but_an_interruption_is_not(h):
    project = h.project()
    failed = seed(h.store, project, title="Failed", user="u", answer="a", now=time.time())
    turn = h.store.start_turn(failed.id)
    h.store.finish_turn(turn["id"], "failed", error={"kind": "provider", "message": "boom"})
    stopped = seed(h.store, project, title="Stopped", user="u", answer="a", now=time.time())
    turn = h.store.start_turn(stopped.id)
    h.store.finish_turn(turn["id"], "interrupted", error={"kind": "interrupted", "message": "x"})
    states = h.manager.states(h.store.sessions())
    assert states[failed.id] == "failed" and states[stopped.id] == "idle"


async def test_an_escalation_and_a_question_are_attention(h):
    project = h.project()
    blocked = seed(h.store, project, title="Blocked", user="u", answer="a", now=time.time())
    h.rt.escalations[blocked.id] = ["I need a decision"]
    asked = seed(h.store, project, title="Asked", user="u", answer="a", now=time.time())
    from nexus.questions import PendingQuestion, normalize_request

    q = normalize_request({"questions": [{"id": "q", "question": "Which?"}]})
    h.rt.questions["qst_1"] = PendingQuestion("qst_1", asked.id, None, q, time.time(), asyncio.get_running_loop().create_future())
    states = h.manager.states(h.store.sessions())
    assert states[blocked.id] == "blocked" and states[asked.id] == "needs_answer"
