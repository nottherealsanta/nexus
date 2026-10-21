"""Persistent children: ``agent.resumed``/``agent.stopped`` fold into ``AgentView``.

plans/done/NONBLOCKING_SUBAGENTS_PLAN.md section 3.4: ``status`` keeps meaning
"a run is in flight" (``spawned``); ``runs``, ``continuable``, ``stopped`` and
``last_run_ts`` say what a finished child can do next.
"""

from __future__ import annotations

from nexus.events import AGENT_EVENTS, EVENT_TYPES, Event
from nexus.view import fold

META = {"id": "s1/sub/1", "parent": None, "depth": 1, "type": "task", "task": "t1", "index": 0}


def _ev(seq, type_, data=None, *, turn=None):
    return Event(type=type_, data=dict(data or {}), seq=seq, ts=float(seq), session="s1", turn=turn, id=f"x{seq:03d}")


def _spawned(seq=1, **extra):
    return _ev(seq, "agent.spawned", {"agent": META, **META, **extra})


def _completed(seq, **extra):
    return _ev(seq, "agent.completed", {"agent": META, **META, "status": "completed", "ok": True, **extra})


def _resumed(seq, run, message="please continue", **extra):
    return _ev(seq, "agent.resumed", {"agent": META, **META, "run": run, "from": "root", "message": message, **extra})


def _stopped(seq, **extra):
    return _ev(seq, "agent.stopped", {"agent": META, **META, "by": "root", "reason": "done", **extra})


def _agent(events):
    return fold(events).agents[META["id"]]


def test_new_events_are_catalogued():
    assert {"agent.resumed", "agent.stopped"} <= set(AGENT_EVENTS)
    assert {"agent.resumed", "agent.stopped"} <= EVENT_TYPES


def test_spawn_completed_resumed_completed_folds_runs_and_status():
    spawned = _agent([_spawned(background=True, continuable=True)])
    assert (spawned.status, spawned.runs, spawned.background, spawned.continuable) == ("spawned", 1, True, True)
    assert spawned.last_run_ts == 1.0

    done = _agent([_spawned(continuable=True), _completed(2, run=1, continuable=True)])
    assert (done.status, done.runs, done.continuable, done.stopped) == ("completed", 1, True, False)
    assert done.completed_ts == 2.0 and done.last_run_ts == 2.0

    resumed = _agent([_spawned(continuable=True), _completed(2, run=1, continuable=True), _resumed(3, 2)])
    assert (resumed.status, resumed.runs) == ("spawned", 2)
    assert resumed.ok is None and resumed.completed_ts is None and resumed.last_run_ts == 3.0

    again = _agent([
        _spawned(continuable=True), _completed(2, run=1, continuable=True), _resumed(3, 2),
        _completed(4, run=2, continuable=True, status="failed", ok=False),
    ])
    assert (again.status, again.runs, again.ok) == ("failed", 2, False)


def test_follow_up_row_lands_on_the_child_page_with_root_followup_origin():
    agent = _agent([_spawned(), _completed(2, run=1), _resumed(3, 2, "also check the tests")])
    turn = agent.body.turns[-1]
    assert turn.phase == "active" and len(agent.body.turns) == 2
    [row] = [m for m in turn.messages if m.role == "user"]
    assert row.text == "also check the tests"
    assert row.origin == {"kind": "root_followup", "agent": META["id"], "run": 2}
    # The new run's turn closes with its own completion.
    closed = _agent([_spawned(), _completed(2, run=1), _resumed(3, 2), _completed(4, run=2)])
    assert [t.phase for t in closed.body.turns] == ["completed", "completed"]


def test_follow_up_text_is_bounded():
    agent = _agent([_spawned(), _resumed(2, 2, "x" * 9000)])
    [row] = agent.body.turns[-1].messages
    assert len(row.text) == 4000


def test_stopped_is_terminal_and_keeps_the_log():
    agent = _agent([_spawned(continuable=True), _completed(2, run=1, continuable=True), _stopped(3)])
    assert agent.stopped is True and agent.stopped_by == "root" and agent.stopped_reason == "done"
    assert agent.status == "completed" and agent.runs == 1


def test_replay_equals_live_and_unknown_fields_are_tolerated():
    events = [
        _spawned(continuable=True, future_field={"x": 1}),
        _completed(2, run=1, continuable=True),
        _resumed(3, 2, extra_thing=True),
        _completed(4, run=2, continuable=True),
        _stopped(5, surprise=[1, 2]),
    ]
    live = fold([])  # fold is a pure reduction: feeding events one at a time must match one batch
    from nexus.view import apply
    for event in events:
        live = apply(live, event)
    assert live.to_dict() == fold(events).to_dict()


def test_events_for_an_unknown_agent_are_diagnostic_not_fatal():
    state = fold([_resumed(1, 2), _stopped(2)])
    assert state.agents == {}


def test_turnless_agent_events_do_not_create_phantom_turns():
    state = fold([_spawned(), _completed(2, run=1), _resumed(3, 2), _stopped(4)])
    assert state.turns == []


def test_report_origin_is_normalized_and_bounded():
    queued = _ev(1, "input.queued", {
        "queued_id": "q1", "mode": "steer", "content": [{"type": "text", "text": "hi"}],
        "origin": {
            "kind": "subagent_report", "agent": "a" * 500, "run": 3, "status": "completed",
            "agents": ["s1/sub/1", 7, "", "s1/sub/2"] + ["x"] * 100, "junk": "dropped",
        },
    })
    [item] = fold([queued]).input_queue
    origin = item.origin
    assert origin["kind"] == "subagent_report" and origin["run"] == 3 and origin["status"] == "completed"
    assert len(origin["agent"]) == 200
    assert origin["agents"][:2] == ["s1/sub/1", "s1/sub/2"] and len(origin["agents"]) == 30  # 32 inspected, junk skipped
    assert "junk" not in origin


def test_origin_run_must_be_a_bounded_int():
    queued = _ev(1, "input.queued", {
        "queued_id": "q1", "content": [], "origin": {"kind": "root_followup", "run": True, "agent": "a"},
    })
    [item] = fold([queued]).input_queue
    assert item.origin["kind"] == "root_followup" and "run" not in item.origin
