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


def test_refused_follow_up_keeps_the_last_completed_result_and_records_the_refusal():
    events = [
        _spawned(continuable=True),
        _completed(2, run=1, continuable=True, stop_reason="end_turn"),
        _resumed(3, 2, "also check the tests"),
        _completed(
            4, run=2, continuable=True, status="failed", ok=False, is_error=True,
            unchanged=True, error="ContextFullError: no room",
        ),
    ]
    agent = _agent(events)
    assert (agent.status, agent.ok, agent.is_error, agent.error) == ("completed", True, False, None)
    assert (agent.runs, agent.continuable, agent.stop_reason) == (2, True, "end_turn")
    assert agent.last_run_ts == 4.0 and agent.completed_ts is not None
    assert agent.diagnostics == ["follow-up run 2 refused: ContextFullError: no room"]
    # The audit trail keeps what the root sent, closed as refused (no child run began).
    refused = agent.body.turns[-1]
    assert refused.phase == "completed" and refused.stop_reason == "refused"
    assert refused.error == "ContextFullError: no room"
    assert [m.text for m in refused.messages] == ["also check the tests"]
    # A later follow-up works normally and replay equals the live fold.
    again = _agent([*events, _resumed(5, 3), _completed(6, run=3, continuable=True)])
    assert (again.status, again.runs, again.error, again.diagnostics) == (
        "completed", 3, None, agent.diagnostics,
    )
    assert fold(events) == fold(events)


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


def test_continue_call_links_child_and_run_two_follows_its_followup_row():
    meta1 = {**META, "parent_call_id": "c1"}
    meta2 = {**META, "parent_call_id": "c2"}
    T = "root-turn"

    def child(seq, text, meta):
        return _ev(seq, "text.delta", {"agent": meta, "text": text}, turn=T)

    events = [
        _ev(1, "turn.started", {}, turn=T),
        _ev(2, "tool.requested", {"call_id": "c1", "tool": "subagent", "arguments": {}}, turn=T),
        _ev(3, "agent.spawned", {"agent": meta1, **meta1}, turn=T),
        child(4, "run one", meta1),
        _ev(5, "agent.completed", {"agent": meta1, **meta1, "status": "completed", "ok": True, "continuable": True}, turn=T),
        _ev(6, "tool.completed", {"call_id": "c1", "tool": "subagent", "ok": True}, turn=T),
        _ev(7, "tool.requested", {"call_id": "c2", "tool": "subagent_continue", "arguments": {}}, turn=T),
        _ev(8, "agent.resumed", {"agent": meta2, **meta2, "run": 2, "from": "root", "message": "again"}, turn=T),
        child(9, "run two", meta2),
        _ev(10, "agent.completed", {"agent": meta2, **meta2, "status": "completed", "ok": True, "continuable": True}, turn=T),
    ]
    state = fold(events)
    tools = {t.call_id: t for turn in state.turns for t in turn.tools}
    assert META["id"] in tools["c1"].child_agent_ids
    assert tools["c2"].child_agent_ids == [META["id"]]
    agent = state.agents[META["id"]]
    assert agent.parent_call_id == "c1"
    turns = agent.body.turns
    assert [t.id for t in turns] == ["t1", "t1:run2"]
    assert [m.role for m in turns[0].messages] == ["assistant"]
    assert turns[0].messages[0].blocks[0].text == "run one"
    assert [m.role for m in turns[1].messages] == ["user", "assistant"]
    assert (turns[1].messages[0].origin or {}).get("kind") == "root_followup"
    assert turns[1].messages[1].blocks[0].text == "run two"
