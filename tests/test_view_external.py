"""Reducer support for external-worker events (``external.*``; Claude Code's own tools)."""
from __future__ import annotations

import json

from nexus.events import EVENT_GROUPS, EVENT_TYPES, Event
from nexus.view import fold

PROV = "Claude Code (own tools)"


def _ev(seq, type_, data=None, *, turn="t1", session="s1", ts=None):
    return Event(type=type_, data=dict(data or {}), seq=seq, ts=float(seq) if ts is None else ts,
                 session=session, turn=turn, id=f"x{seq:03d}")


def _log():
    return [
        _ev(1, "turn.started", {"limits": {}}),
        _ev(2, "external.started", {"provider": "claude-code", "model": "claude-haiku-4-5",
                                    "authority": "Claude Code ran its own tools", "permission_mode": "dontAsk"}),
        _ev(3, "text", {"text": "Looking.", "provider": "claude-code", "model": "claude-haiku-4-5"}),
        _ev(4, "model.stopped", {"stop_reason": "end_turn"}),
        _ev(5, "external.tool", {"phase": "requested", "call_id": "c1", "tool": "Read", "input": {"file_path": "a"},
                                 "provenance": PROV}),
        _ev(6, "external.tool", {"phase": "completed", "call_id": "c1", "tool": "Read", "is_error": False,
                                 "content": [{"type": "text", "text": "print(1)"}],
                                 "clip_note": "clipped: showing the first 8000 of 9000 characters", "provenance": PROV}),
        _ev(7, "text", {"text": "Done.", "provider": "claude-code", "model": "claude-haiku-4-5"}),
        _ev(8, "model.stopped", {"stop_reason": "end_turn"}),
        _ev(9, "external.finished", {"status": "completed", "usage": "unknown", "transcript_path": "/t.jsonl"}),
        _ev(10, "turn.completed", {"stop_reason": "end_turn", "iterations": 0, "usage": {}}),
    ]


def test_events_are_registered():
    assert EVENT_GROUPS["external"] == ("external.started", "external.tool", "external.finished")
    assert {"external.started", "external.tool", "external.finished"} <= EVENT_TYPES


def test_external_tool_rows_carry_provenance_and_are_not_nexus_executions():
    view = fold(_log())
    assert not view.diagnostics
    (tool,) = view.tools
    assert tool.name == "Read" and tool.provenance == PROV and tool.executed is False
    assert tool.status == "completed" and tool.input == {"file_path": "a"}
    assert tool.result == [{"type": "text", "text": "print(1)"}]
    assert "clipped" in tool.display  # the clip is announced on the row
    assert [m.text for m in view.messages] == ["Looking.", "Done."]
    assert view.messages[0].provider == "claude-code" and view.messages[0].model == "claude-haiku-4-5"
    assert view.external["authority"].startswith("Claude Code ran") and view.external["finished"] is True
    assert view.external["usage"] == "unknown"
    assert view.turns[0].phase == "completed"


def test_replay_is_idempotent_and_order_tolerant():
    events = _log()
    view = fold(events)
    assert fold(events + events).to_dict() == view.to_dict()
    unsequenced = [Event(type=e.type, data=e.data, seq=0, ts=e.ts, session=e.session, turn=e.turn) for e in events]
    swapped = unsequenced[:4] + [unsequenced[5], unsequenced[4]] + unsequenced[6:]
    tool = fold(swapped).tools[0]
    assert tool.status == "completed" and tool.input == {"file_path": "a"} and tool.provenance == PROV
    snapshot = json.loads(json.dumps(view.to_dict()))
    assert snapshot["turns"][0]["tools"][0]["provenance"] == PROV


def test_error_result_marks_the_row_failed():
    events = [e for e in _log() if e.seq != 6] + [
        _ev(11, "external.tool", {"phase": "completed", "call_id": "c1", "tool": "Read", "is_error": True,
                                  "content": [{"type": "text", "text": "denied"}], "provenance": PROV})]
    tool = fold(events).tools[0]
    assert tool.status == "failed" and tool.is_error


def test_ordinary_views_keep_their_wire_shape():
    view = fold([_ev(1, "turn.started"), _ev(2, "tool.requested", {"call_id": "n", "tool": "read"})])
    data = view.to_dict()
    assert "external" not in data and "provenance" not in data["turns"][0]["tools"][0]


def test_agent_completed_marks_usage_unknown():
    meta = {"id": "s1/sub/1", "parent": "s1", "depth": 1, "type": "claude", "task": "task-1", "session": "s1/sub/1"}
    view = fold([
        _ev(1, "agent.spawned", {"agent": meta, **meta}),
        _ev(2, "external.started", {"agent": meta, "provider": "claude-code"}, session="s1/sub/1"),
        _ev(3, "external.tool", {"agent": meta, "phase": "requested", "call_id": "c", "tool": "Grep", "provenance": PROV},
            session="s1/sub/1"),
        _ev(4, "agent.completed", {"agent": meta, **meta, "status": "completed", "ok": True, "is_error": False,
                                   "usage": {}, "usage_unknown": True}),
    ])
    agent = view.agents["s1/sub/1"]
    assert agent.usage_unknown and agent.to_dict()["usage_unknown"] is True
    assert agent.body.tools[0].provenance == PROV and agent.body.external["provider"] == "claude-code"
    assert "usage_unknown" not in fold([_ev(1, "agent.spawned", {"agent": meta, **meta})]).agents["s1/sub/1"].to_dict()
