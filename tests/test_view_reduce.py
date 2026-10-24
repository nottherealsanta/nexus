"""Phase 8a1: the pure view reducer, golden replay, and JSON snapshots.

The reducer is the one place every surface renders from (PLAN section 14.7), so
these tests pin the properties the design depends on:

* a recorded multi-turn / subagent / approval / reconnect log folds to a stable
  golden view (``tests/fixtures/view/*.expected.json``);
* streamed deltas plus the finalized event never produce the text twice;
* replay is deterministic and idempotent -- a snapshot plus a tail equals the
  whole log, and folding twice changes nothing;
* unknown events are tolerated and retained rather than dropped or raised on;
* a view snapshot is JSON-safe;
* ``view/`` imports only ``nexus.events`` and the standard library, is
  synchronous, and never mutates the state it is handed.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from nexus.events import Event
from nexus.view import ConversationView, apply, apply_many, fold, initial_state

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "view"
REPO_ROOT = Path(__file__).resolve().parents[1]
VIEW_ROOT = REPO_ROOT / "nexus" / "view"

GOLDEN_LOGS = ("text_stream", "multi_turn_tools", "subagent", "reconnect", "code_mode", "kernel_cells")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_events(name: str) -> list[Event]:
    raw = json.loads((FIXTURES / f"{name}.events.json").read_text(encoding="utf-8"))
    return [Event(**item) for item in raw]


def _load_expected(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.expected.json").read_text(encoding="utf-8"))


def _ev(seq, type_, data=None, *, turn=None, session="s1", ts=None, id=None):
    return Event(
        type=type_,
        data=dict(data or {}),
        seq=seq,
        ts=float(seq) if ts is None else float(ts),
        session=session,
        turn=turn,
        id=id or f"x{seq:03d}",
    )


# ---------------------------------------------------------------------------
# Golden logs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", GOLDEN_LOGS)
def test_golden_log_reduces_to_expected_view(name: str):
    view = fold(_load_events(name))
    assert view.to_dict() == _load_expected(name)


@pytest.mark.parametrize("name", GOLDEN_LOGS)
def test_golden_view_snapshot_is_json_safe(name: str):
    view = fold(_load_events(name))
    encoded = json.dumps(view.to_dict(), sort_keys=True)
    assert json.loads(encoded) == view.to_dict()


# ---------------------------------------------------------------------------
# Replay properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", GOLDEN_LOGS)
def test_fold_is_deterministic(name: str):
    events = _load_events(name)
    assert fold(events).to_dict() == fold(events).to_dict()


@pytest.mark.parametrize("name", GOLDEN_LOGS)
def test_reapplying_a_folded_log_is_idempotent(name: str):
    events = _load_events(name)
    once = fold(events)
    twice = fold(events, once)
    assert twice.to_dict() == once.to_dict()


def test_snapshot_plus_tail_equals_full_log():
    events = _load_events("reconnect")
    split = json.loads((FIXTURES / "reconnect.split.json").read_text())["prefix"]
    full = fold(events)
    prefix = fold(events[:split])
    resumed = fold(events[split:], prefix)
    assert resumed.to_dict() == full.to_dict()


def test_apply_many_matches_fold():
    events = _load_events("multi_turn_tools")
    assert apply_many(initial_state(), events).to_dict() == fold(events).to_dict()


def test_apply_does_not_mutate_prior_state():
    events = _load_events("text_stream")
    state = fold(events[:5])
    before = state.to_dict()
    apply(state, events[5])
    assert state.to_dict() == before


def test_unsequenced_events_apply_in_order():
    view = fold(
        [
            _ev(0, "text", {"text": "a"}, turn="t", id="u1"),
            _ev(0, "text", {"text": "b"}, turn="t", id="u2"),
        ]
    )
    assert view.messages[0].text == "ab"


def test_queued_input_before_turn_started_attaches_the_prompt():
    # The session emits input.consumed before run_turn emits turn.started.
    view = fold(
        [
            _ev(1, "input.queued", {"queued_id": "q1", "content": [{"type": "text", "text": "hi"}], "queue_depth": 1}),
            _ev(2, "input.consumed", {"queued_id": "q1", "turn": "t1"}),
            _ev(3, "turn.started", {"limits": {}}, turn="t1"),
            _ev(4, "model.started", {}, turn="t1"),
            _ev(5, "text", {"text": "ok"}, turn="t1"),
        ]
    )
    assert view.input_queue == []
    turn = view.turns[0]
    assert turn.id == "t1"
    assert [m.role for m in turn.messages] == ["user", "assistant"]
    assert turn.messages[0].text == "hi"
    assert turn.messages[-1].text == "ok"


def test_input_mode_changed_updates_mode_and_order_and_replays_identically():
    def queued(seq, queued_id, mode="queue"):
        return _ev(seq, "input.queued", {"queued_id": queued_id, "mode": mode, "queue_depth": seq,
                                         "content": [{"type": "text", "text": queued_id}]})

    events = [
        queued(1, "s1", "steer"), queued(2, "q1"), queued(3, "q2"),
        _ev(4, "input.mode_changed", {"queued_id": "q2", "mode": "steer", "order": ["s1", "q2", "q1"]}),
        _ev(5, "input.mode_changed", {"queued_id": "q1", "mode": "interrupt", "order": ["q1", "s1", "q2"]}),
        _ev(6, "input.mode_changed", {"queued_id": "gone", "mode": "steer", "order": ["gone"]}),
    ]
    view = fold(events)
    assert [(i.queued_id, i.mode) for i in view.input_queue] == [("q1", "interrupt"), ("s1", "steer"), ("q2", "steer")]
    # The mode change on an unknown id is ignored, and the queue ignores a missing order.
    assert fold(events[:5]).input_queue == view.input_queue


def test_input_consumed_before_queued_still_attaches_the_prompt():
    # A live follower attaches mid-turn, so it can observe input.consumed (the
    # turn is started from the cursor) *before* the input.queued event with the
    # actual text. The user message must still carry the prompt either way.
    view = fold(
        [
            _ev(1, "input.consumed", {"queued_id": "q1", "turn": "t1"}),
            _ev(2, "turn.started", {"limits": {}}, turn="t1"),
            _ev(3, "input.queued", {"queued_id": "q1", "content": [{"type": "text", "text": "hi"}], "queue_depth": 1}),
            _ev(4, "model.started", {}, turn="t1"),
            _ev(5, "text", {"text": "ok"}, turn="t1"),
        ]
    )
    turn = view.turns[0]
    assert [m.role for m in turn.messages] == ["user", "assistant"]
    assert turn.messages[0].text == "hi"
    assert view.input_queue == []


def test_completed_turn_summary_fields_use_turn_events_and_frozen_metadata():
    events = [
        _ev(1, "input.consumed", {"queued_id": "q1", "turn": "t1"}, turn=None, ts=100),
        _ev(
            2,
            "turn.started",
            {"agent": {"name": "general", "source": "config"}},
            turn="t1",
            ts=101,
        ),
        _ev(
            3,
            "model.started",
            {"provider": "p", "model": "m", "reasoning_effort": "high"},
            turn="t1",
            ts=102,
        ),
        _ev(4, "text", {"text": "done"}, turn="t1", ts=104),
        _ev(5, "model.stopped", {"stop_reason": "end_turn"}, turn="t1", ts=105),
        _ev(6, "turn.completed", {}, turn="t1", ts=106),
        # Later session selections must not rewrite a completed turn's facts.
        _ev(7, "agent.selected", {"name": "later"}, ts=107),
        _ev(8, "reasoning_effort.selected", {"effort": "low"}, ts=108),
    ]
    view = fold(events)
    turn = view.turns[0]

    assert turn.user_ts == 100
    assert turn.assistant_ts == 104
    assert turn.elapsed_ms == 4000
    assert turn.agent == {"name": "general", "source": "config"}
    assert turn.reasoning_effort == "high"
    assert turn.to_dict()["elapsed_ms"] == 4000

    split = fold(events[:4])
    assert fold(events[4:], split).to_dict() == view.to_dict()


@pytest.mark.parametrize(
    ("user_ts", "assistant_ts", "expected"),
    [
        (None, 4.0, None),
        (2.0, None, None),
        (float("nan"), 4.0, None),
        (5.0, 4.0, None),
        (2.0, 4.125, 2125),
    ],
)
def test_completed_turn_summary_timestamp_validation(user_ts, assistant_ts, expected):
    events = []
    events.append(
        _ev(
            1,
            "input.consumed",
            {"queued_id": "q", "turn": "t"},
            ts=2.0 if user_ts is None else user_ts,
        )
    )
    events.extend(
        [
            _ev(2, "turn.started", {}, turn="t", ts=3),
            _ev(3, "model.started", {}, turn="t", ts=3.5),
                _ev(4, "text", {"text": "ok"}, turn="t", ts=6 if assistant_ts is None else assistant_ts),
            _ev(5, "model.stopped", {}, turn="t", ts=6),
        ]
    )
    if user_ts is None:
        events[0] = _ev(1, "input.consumed", {"queued_id": "q", "turn": "t"}, ts=float("nan"))
    if assistant_ts is None:
        events[3] = _ev(4, "text", {"text": "ok"}, turn="t", ts=float("nan"))
        events[4] = _ev(5, "model.stopped", {}, turn="t", ts=float("nan"))
    turn = fold(events).turns[0]
    assert turn.elapsed_ms == expected


def test_effort_is_unknown_when_not_emitted_and_frozen_when_present():
    view = fold(
        [
            _ev(1, "turn.started", {}, turn="t"),
            _ev(2, "model.started", {}, turn="t"),
            _ev(3, "reasoning_effort.selected", {"effort": "high"}),
        ]
    )
    assert view.turns[0].reasoning_effort is None

    view = fold(
        [
            _ev(1, "turn.started", {}, turn="t"),
            _ev(2, "model.started", {"reasoning_effort": "medium"}, turn="t"),
            _ev(3, "model.started", {"reasoning_effort": None}, turn="t"),
        ]
    )
    assert view.turns[0].reasoning_effort == "medium"


# ---------------------------------------------------------------------------
# Deltas, thinking, tools, permissions, agents
# ---------------------------------------------------------------------------


def test_streamed_deltas_and_final_text_do_not_duplicate():
    view = fold(
        [
            _ev(1, "turn.started", {}, turn="t"),
            _ev(2, "model.started", {"iteration": 0}, turn="t"),
            _ev(3, "text.delta", {"text": "Hel"}, turn="t"),
            _ev(4, "text.delta", {"text": "lo"}, turn="t"),
            _ev(5, "text", {"text": "Hello"}, turn="t"),
        ]
    )
    message = view.messages[0]
    assert message.text == "Hello"
    assert [b.text for b in message.blocks] == ["Hello"]
    assert message.blocks[0].streamed is True
    assert message.blocks[0].finalized is True


def test_visible_text_orders_after_prior_tool_not_model_placeholder():
    view = fold(
        [
            _ev(1, "turn.started", {}, turn="t"),
            _ev(2, "model.started", {}, turn="t"),
            _ev(3, "tool.requested", {"call_id": "c", "tool": "Read"}, turn="t"),
            _ev(4, "text.delta", {"text": "answer"}, turn="t"),
        ]
    )
    message = view.messages[0]
    tool = view.tools[0]
    assert tool.event_seq < message.event_seq


def test_final_text_without_deltas_is_appended_once():
    view = fold(
        [
            _ev(1, "turn.started", {}, turn="t"),
            _ev(2, "model.started", {"iteration": 0}, turn="t"),
            _ev(3, "text", {"text": "Only final"}, turn="t"),
        ]
    )
    message = view.messages[0]
    assert [b.text for b in message.blocks] == ["Only final"]
    assert message.blocks[0].streamed is False


def test_thinking_final_carries_signature_and_stays_ordered():
    view = fold(
        [
            _ev(1, "turn.started", {}, turn="t"),
            _ev(2, "model.started", {}, turn="t"),
            _ev(3, "thinking.delta", {"text": "pon"}, turn="t"),
            _ev(4, "thinking.delta", {"text": "der"}, turn="t"),
            _ev(5, "thinking", {"text": "ponder", "signature": "sig"}, turn="t"),
            _ev(6, "text", {"text": "answer"}, turn="t"),
        ]
    )
    message = view.messages[0]
    assert [b.kind for b in message.blocks] == ["thinking", "text"]
    assert message.blocks[0].signature == "sig"
    assert message.thinking == "ponder"
    assert message.text == "answer"


def test_thinking_run_records_its_duration_from_event_timestamps():
    view = fold(
        [
            _ev(1, "turn.started", {}, turn="t", ts=10.0),
            _ev(2, "model.started", {}, turn="t", ts=10.1),
            _ev(3, "thinking.delta", {"text": "pon"}, turn="t", ts=10.2),
            _ev(4, "thinking.delta", {"text": "der"}, turn="t", ts=10.5),
            _ev(5, "thinking.end", {"signature": "sig"}, turn="t", ts=10.871),
            _ev(6, "thinking", {"text": "ponder", "signature": "sig"}, turn="t", ts=11.0),
        ]
    )
    assert view.messages[0].thinking_ms == 671


def test_tool_lifecycle_status_transitions():
    view = fold(
        [
            _ev(1, "turn.started", {}, turn="t"),
            _ev(2, "tool.requested", {"call_id": "c1", "tool": "Read"}, turn="t"),
            _ev(3, "tool.started", {"call_id": "c1", "tool": "Read", "bundle": "fs"}, turn="t"),
            _ev(4, "tool.progress", {"call_id": "c1", "text": "half"}, turn="t"),
            _ev(5, "tool.completed", {"call_id": "c1", "tool": "Read", "is_error": False, "duration_ms": 3, "executed": True}, turn="t"),
            _ev(6, "tool.requested", {"call_id": "c2", "tool": "Bash"}, turn="t"),
            _ev(7, "tool.failed", {"call_id": "c2", "tool": "Bash", "code": "unknown_tool", "executed": False}, turn="t"),
        ]
    )
    tools = {t.call_id: t for t in view.tools}
    assert tools["c1"].status == "completed"
    assert tools["c1"].progress == ["half"]
    assert tools["c1"].bundle == "fs"
    assert tools["c2"].status == "failed"
    assert tools["c2"].executed is False
    assert tools["c2"].code == "unknown_tool"


def test_tool_transcript_events_are_order_tolerant_and_old_events_stay_renderable():
    view = fold(
        [
            _ev(1, "tool.result", {"tool_use_id": "late", "is_error": False, "content": [{"type": "text", "text": "ok"}]}, turn="t"),
            _ev(2, "tool.input", {"call_id": "late", "input": {"path": "a.txt"}}, turn="t"),
            _ev(3, "tool.requested", {"call_id": "late", "tool": "Read"}, turn="t"),
            # Legacy tool lifecycle data had neither input/result nor a diff.
            _ev(4, "tool.requested", {"call_id": "old", "tool": "Bash"}, turn="t"),
            _ev(5, "tool.completed", {"call_id": "old", "tool": "Bash", "executed": True}, turn="t"),
        ]
    )
    tools = {tool.call_id: tool for tool in view.tools}
    assert tools["late"].name == "Read"
    assert tools["late"].input == {"path": "a.txt"}
    assert tools["late"].result == [{"type": "text", "text": "ok"}]
    assert tools["late"].status == "completed"
    assert tools["old"].status == "completed"
    assert tools["old"].diff is None


@pytest.mark.parametrize("spawn_first", [False, True])
def test_task_calls_link_parallel_and_nested_agents_in_any_event_order(spawn_first):
    root = {
        "id": "s/sub/1", "parent": "s", "parent_call_id": "root-task",
        "depth": 1, "task": "root-child", "session": "s/sub/1",
    }
    sibling = {
        "id": "s/sub/2", "parent": "s", "parent_call_id": "root-task",
        "depth": 1, "task": "root-sibling", "session": "s/sub/2",
    }
    nested = {
        "id": "s/sub/1/sub/1", "parent": "s/sub/1", "parent_agent_id": "s/sub/1",
        "parent_call_id": "inner-task", "depth": 2, "task": "nested",
        "session": "s/sub/1/sub/1",
    }
    requested = _ev(2, "tool.requested", {"call_id": "root-task", "tool": "Task"}, turn="t")
    events = [
        _ev(1, "turn.started", {}, turn="t"),
        *(
                [_ev(2, "agent.spawned", {"agent": root, **root}, turn="t"), _ev(3, "agent.spawned", {"agent": sibling, **sibling}, turn="t"), _ev(4, "tool.requested", {"call_id": "root-task", "tool": "Task"}, turn="t")]
            if spawn_first
            else [requested, _ev(3, "agent.spawned", {"agent": root, **root}, turn="t"), _ev(4, "agent.spawned", {"agent": sibling, **sibling}, turn="t")]
        ),
        _ev(5, "tool.requested", {"agent": root, "call_id": "inner-task", "tool": "Task"}, session=root["session"]),
        _ev(6, "agent.spawned", {"agent": nested, **nested}, session=root["session"]),
    ]
    view = fold(events)
    root_task = next(tool for tool in view.tools if tool.call_id == "root-task")
    assert root_task.child_agent_ids == [root["id"], sibling["id"]]
    inner_task = next(tool for tool in view.agents[root["id"]].body.tools if tool.call_id == "inner-task")
    assert inner_task.child_agent_ids == [nested["id"]]


def test_permission_pending_then_resolved():
    view = fold(
        [
            _ev(1, "turn.started", {}, turn="t"),
            _ev(2, "permission.requested", {"id": "p1", "call_id": "c1", "tool": "Bash", "key": "cmd:ls", "default_rule": "Bash(cmd:ls)"}, turn="t"),
        ]
    )
    assert view.phase == "awaiting_permission"
    assert [p.id for p in view.pending_permissions] == ["p1"]
    view = fold(
        [
            _ev(3, "permission.resolved", {"id": "p1", "call_id": "c1", "tool": "Bash", "key": "cmd:ls", "decision": "allow_once", "scope": "session", "ts": 3.0}, turn="t"),
        ],
        view,
    )
    permission = view.permissions[0]
    assert permission.status == "resolved"
    assert permission.decision == "allow_once"
    assert view.pending_permissions == []


def test_permission_targets_are_projected_without_rule_metadata():
    view = fold(
        [
            _ev(
                1,
                "permission.requested",
                {
                    "id": "p1",
                    "tool": "Write",
                    "targets": [
                        {
                            "role": "destination",
                            "path": "notes/today.md",
                            "reason": "This file is outside the writable root.",
                            "suggested_rule": "Write(notes/today.md)",
                            "private_metadata": "must not be projected",
                        }
                    ],
                },
            )
        ]
    )

    permission = view.permissions[0]
    assert permission.to_dict()["targets"] == [
        {
            "role": "destination",
            "path": "notes/today.md",
            "reason": "This file is outside the writable root.",
        }
    ]
    assert "suggested_rule" not in str(permission.to_dict())
    assert "private_metadata" not in str(permission.to_dict())


def test_permission_target_projection_is_complete_or_unavailable():
    from nexus.view.reduce import (
        _MAX_PERMISSION_TARGET_REQUEST_CHARS,
        _MAX_PERMISSION_TARGETS,
    )

    oversized_role = "r" * 65
    valid_target = {"role": "source", "path": "src/file.py", "reason": "Read required."}
    targets = [
        valid_target,
        {"role": "write", "path": "dst", "reason": 12},
        {"role": oversized_role, "path": "dst", "reason": "too long role"},
        {"role": "write", "path": "p" * 4097, "reason": "too long path"},
        {"role": "write", "path": "dst", "reason": "x" * 513},
        None,
    ]
    targets.extend(
        {"role": "source", "path": f"src/{index}.py", "reason": "Read required."}
        for index in range(_MAX_PERMISSION_TARGETS + 2)
    )
    view = fold([_ev(1, "permission.requested", {"id": "p1", "targets": targets})])

    projected = view.permissions[0].to_dict()
    assert projected["targets"] == []

    overflow = fold(
        [
            _ev(
                1,
                "permission.requested",
                {
                    "id": "p2",
                    "targets": [
                        {"role": "source", "path": f"src/{index}.py", "reason": "Read required."}
                        for index in range(_MAX_PERMISSION_TARGETS + 2)
                    ],
                },
            )
        ]
    )
    assert overflow.permissions[0].to_dict()["targets"] == []

    boundary = fold(
        [
            _ev(
                1,
                "permission.requested",
                {
                    "id": "p3",
                    "targets": [
                        {"role": "source", "path": f"src/{index}.py", "reason": "Read required."}
                        for index in range(_MAX_PERMISSION_TARGETS)
                    ],
                },
            )
        ]
    )
    assert len(boundary.permissions[0].to_dict()["targets"]) == _MAX_PERMISSION_TARGETS

    aggregate_overflow = fold(
        [
            _ev(
                1,
                "permission.requested",
                {
                    "id": "p4",
                    "targets": [
                        {"role": "source", "path": f"p/{index}/{'x' * 4096}", "reason": "Read required."}
                        for index in range(16)
                    ],
                },
            )
        ]
    )
    assert _MAX_PERMISSION_TARGET_REQUEST_CHARS == 65_536
    assert aggregate_overflow.permissions[0].to_dict()["targets"] == []


def test_legacy_permission_event_keeps_scalar_readability_without_targets():
    view = fold(
        [_ev(1, "permission.requested", {"id": "old", "tool": "Bash", "key": "cmd:ls"})]
    )

    permission = view.permissions[0]
    assert permission.tool == "Bash"
    assert permission.key == "cmd:ls"
    assert "targets" not in permission.to_dict()


def test_terminal_turn_expires_a_leftover_pending_permission():
    view = fold(
        [
            _ev(1, "turn.started", {}, turn="t"),
            _ev(2, "permission.requested", {"id": "p1", "call_id": "c1", "tool": "Bash"}, turn="t"),
            _ev(3, "turn.cancelled", {"reason": "viewer left", "iterations": 0}, turn="t"),
        ]
    )
    assert view.permissions[0].status == "cancelled"
    assert view.phase == "idle"


def test_shutdown_cancel_is_not_an_error_but_other_terminals_keep_theirs():
    from nexus.events import SHUTDOWN_CANCEL_REASON

    def run(kind, data):
        return fold([
            _ev(1, "turn.started", {}, turn="t"),
            _ev(2, "tool.requested", {"call_id": "c1", "name": "Bash", "input": {}}, turn="t"),
            _ev(3, kind, {**data, "iterations": 1}, turn="t"),
        ]).turns[0]

    closing = run("turn.cancelled", {"reason": SHUTDOWN_CANCEL_REASON})
    assert closing.phase == "cancelled"
    assert closing.error is None
    assert SHUTDOWN_CANCEL_REASON not in (closing.tools[0].error or "")
    assert run("turn.cancelled", {"reason": "viewer left"}).error == "viewer left"
    # A real failure whose text mentions the shutdown reason is never hidden.
    assert run("turn.failed", {"error": SHUTDOWN_CANCEL_REASON}).error == SHUTDOWN_CANCEL_REASON


def test_shutdown_cancel_of_child_agent_is_not_an_error_but_real_errors_stay():
    from nexus.events import SHUTDOWN_CANCEL_REASON

    def run(status, error, nested=False):
        meta = {"id": "s1/sub/1", "parent": "s1", "depth": 1, "type": "general", "task": "t", "index": 0}
        events = [_ev(1, "agent.spawned", {"agent": meta})]
        if nested:
            child = {"id": "s1/sub/1/sub/1", "parent": "s1/sub/1", "parent_agent_id": "s1/sub/1",
                     "depth": 2, "type": "general", "task": "t", "index": 0}
            events.append(_ev(2, "agent.spawned", {"agent": child}))
        target = "s1/sub/1/sub/1" if nested else "s1/sub/1"
        events.append(_ev(3, "agent.completed", {"agent": {**(child if nested else meta)}, "status": status, "error": error}))
        agent = fold(events).agents["s1/sub/1"]
        return agent.body.agents[target] if nested else agent

    assert run("cancelled", SHUTDOWN_CANCEL_REASON).error is None
    assert run("cancelled", SHUTDOWN_CANCEL_REASON, nested=True).error is None
    assert run("cancelled", "viewer left").error == "viewer left"
    assert run("failed", SHUTDOWN_CANCEL_REASON).error == SHUTDOWN_CANCEL_REASON


def test_agent_tree_nests_child_conversation():
    meta = {
        "id": "s1/sub/1",
        "parent": "s1",
        "depth": 1,
        "type": "general",
        "task": "task-1",
        "index": 0,
        "session": "s1/sub/1",
    }
    view = fold(
        [
            _ev(1, "agent.spawned", {"agent": meta, **meta, "description": "go", "model": "small", "tools": ["Read"]}, turn="t"),
            _ev(2, "text.delta", {"agent": meta, "text": "work"}, session="s1/sub/1"),
            _ev(3, "text.delta", {"agent": meta, "text": "ing"}, session="s1/sub/1"),
            _ev(4, "text", {"agent": meta, "text": "working"}, session="s1/sub/1"),
            _ev(5, "agent.completed", {"agent": meta, **meta, "status": "completed", "ok": True, "is_error": False, "usage": {"input_tokens": 1, "output_tokens": 2}, "iterations": 1, "stop_reason": "end_turn", "dropped_tools": [], "clamped": False, "error": None}, turn="t"),
        ]
    )
    assert [a.id for a in view.root_agents] == ["s1/sub/1"]
    agent = view.agents["s1/sub/1"]
    assert agent.status == "completed"
    assert agent.ok is True
    assert agent.usage.total_tokens == 3
    assert agent.body.messages[0].text == "working"
    assert agent.body.turns[0].phase == "completed"
    assert view.phase == "idle"


_MODEL_ROOT = {"id": "s1/sub/1", "parent": "s1", "depth": 1, "task": "t1", "session": "s1/sub/1"}
_MODEL_NESTED = {
    "id": "s1/sub/1/sub/1", "parent": "s1/sub/1", "parent_agent_id": "s1/sub/1",
    "depth": 2, "task": "t2", "session": "s1/sub/1/sub/1",
}


def _model_started(seq, meta, provider, model):
    return _ev(seq, "model.started", {"agent": meta, "provider": provider, "model": model}, session=meta["session"])


def test_agent_resolved_model_tracks_actual_model_not_spawn_reference():
    view = fold([
        _ev(1, "agent.spawned", {"agent": _MODEL_ROOT, **_MODEL_ROOT, "tier": "low", "model": "low"}, turn="t"),
    ])
    agent = view.agents["s1/sub/1"]
    assert (agent.model, agent.tier, agent.resolved_model) == ("low", "low", None)
    assert "resolved_model" not in view.to_dict()["agents"][0]  # absent until a model starts

    view = apply_many(view, [
        _model_started(2, _MODEL_ROOT, "anthropic", "claude-haiku"),
        _model_started(3, _MODEL_ROOT, "openai", "gpt-fallback"),
    ])
    agent = view.agents["s1/sub/1"]
    assert agent.model == "low"  # spawn reference is untouched
    assert agent.resolved_model == "openai/gpt-fallback"  # latest start wins
    assert view.to_dict()["agents"][0]["resolved_model"] == "openai/gpt-fallback"


def test_agent_resolved_model_does_not_double_a_qualified_model():
    view = fold([
        _ev(1, "agent.spawned", {"agent": _MODEL_ROOT, **_MODEL_ROOT}, turn="t"),
        _model_started(2, _MODEL_ROOT, "openrouter", "openrouter/anthropic/claude"),
    ])
    assert view.agents["s1/sub/1"].resolved_model == "openrouter/anthropic/claude"
    view = apply(view, _model_started(3, _MODEL_ROOT, "openrouter", "anthropic/claude"))
    assert view.agents["s1/sub/1"].resolved_model == "openrouter/anthropic/claude"
    view = apply(view, _model_started(4, _MODEL_ROOT, None, "bare-model"))
    assert view.agents["s1/sub/1"].resolved_model == "bare-model"


def test_nested_agents_keep_their_own_resolved_model_and_replay_matches():
    events = [
        _ev(1, "agent.spawned", {"agent": _MODEL_ROOT, **_MODEL_ROOT}, turn="t"),
        _model_started(2, _MODEL_ROOT, "anthropic", "parent-model"),
        _ev(3, "agent.spawned", {"agent": _MODEL_NESTED, **_MODEL_NESTED}, session=_MODEL_ROOT["session"]),
        _model_started(4, _MODEL_NESTED, "openai", "child-model"),
        _model_started(5, _MODEL_ROOT, "anthropic", "parent-model-2"),
    ]
    view = fold(events)
    root = view.agents["s1/sub/1"]
    assert root.resolved_model == "anthropic/parent-model-2"
    assert root.body.agents["s1/sub/1/sub/1"].resolved_model == "openai/child-model"
    # Replaying the whole log (stale seqs) neither regresses nor changes it.
    assert apply_many(view, events).to_dict() == view.to_dict()
    assert fold(events).to_dict() == view.to_dict()


def test_external_started_sets_resolved_model_and_completed_backfills():
    external = {"agent": _MODEL_ROOT, "provider": "claude-code", "model": "sonnet", "session": "w1"}
    view = fold([
        _ev(1, "agent.spawned", {"agent": _MODEL_ROOT, **_MODEL_ROOT, "tier": "medium"}, turn="t"),
        _ev(2, "external.started", external, session=_MODEL_ROOT["session"]),
    ])
    assert view.agents["s1/sub/1"].resolved_model == "claude-code/sonnet"

    # No relayed start: the completion's final-model disclosure backfills it.
    bare = fold([
        _ev(1, "agent.spawned", {"agent": _MODEL_ROOT, **_MODEL_ROOT, "tier": "medium"}, turn="t"),
        _ev(2, "agent.completed", {"agent": _MODEL_ROOT, **_MODEL_ROOT, "status": "completed", "resolved_model": "openai/gpt-x"}, turn="t"),
    ])
    assert bare.agents["s1/sub/1"].resolved_model == "openai/gpt-x"
    # ...but never overrides a start already seen.
    both = apply_many(view, [
        _ev(3, "agent.completed", {"agent": _MODEL_ROOT, **_MODEL_ROOT, "status": "completed", "resolved_model": "other/x"}, turn="t"),
    ])
    assert both.agents["s1/sub/1"].resolved_model == "claude-code/sonnet"


def test_agent_tree_is_parent_linked():
    root = {"id": "s1/sub/1", "parent": "s1", "depth": 1, "task": "t1", "session": "s1/sub/1"}
    child = {"id": "s1/sub/1/sub/1", "parent": "s1/sub/1", "depth": 2, "task": "t2", "session": "s1/sub/1/sub/1"}
    view = fold(
        [
            _ev(1, "agent.spawned", {"agent": root, **root}, turn="t"),
            _ev(2, "agent.spawned", {"agent": child, **child}, turn="t"),
        ]
    )
    assert [a.id for a in view.root_agents] == ["s1/sub/1"]
    assert [a.id for a in view.children_of("s1/sub/1")] == ["s1/sub/1/sub/1"]


def test_unknown_event_is_tolerated_and_retained():
    marker = object()
    view = fold([_ev(1, "brand.new.type", {"value": marker}, ts=9.0)])
    assert len(view.diagnostics) == 1
    diagnostic = view.diagnostics[0]
    assert diagnostic.type == "brand.new.type"
    assert diagnostic.seq == 1
    # A payload the reducer cannot interpret is stringified, never dropped.
    assert diagnostic.data["value"].startswith("<object object at")
    # And the snapshot still serializes.
    assert json.loads(json.dumps(view.to_dict()))["diagnostics"][0]["type"] == "brand.new.type"


def test_unknown_event_after_a_turn_does_not_disturb_it():
    events = _load_events("text_stream")
    view = fold([*events, _ev(99, "some.future_thing", {"k": 1}, turn="t1")])
    assert view.turns[0].phase == "completed"
    assert view.messages[0].text == "Hello"
    assert view.diagnostics[-1].type == "some.future_thing"


# ---------------------------------------------------------------------------
# Purity / layering guardrails
# ---------------------------------------------------------------------------


def _module_parts(path: Path) -> list[str]:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def _resolve_from(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    parts = _module_parts(path)
    package = parts if path.name == "__init__.py" else parts[:-1]
    base = package[: len(package) - (node.level - 1)]
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base)


def _view_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(_resolve_from(path, node))
    return modules


@pytest.mark.parametrize("path", sorted(VIEW_ROOT.glob("*.py")), ids=lambda p: p.name)
def test_view_layer_imports_only_events_and_stdlib(path: Path):
    violations = sorted(
        module
        for module in _view_imports(path)
        if module.startswith("nexus")
        and module != "nexus.events"
        and not module.startswith("nexus.view")
    )
    assert not violations, f"{path.name} imports {violations}"


def test_reducer_is_synchronous():
    tree = ast.parse((VIEW_ROOT / "reduce.py").read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree))
    assert not any(isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for node in ast.walk(tree))


def test_apply_rejects_non_events():
    with pytest.raises(TypeError):
        apply(ConversationView(), "not an event")  # type: ignore[arg-type]


def test_context_usage_prefers_the_provider_measurement_and_carries_it_forward():
    from nexus.ui_support.context import context_measure, context_usage
    from nexus.ui_support.timeline import turn_footer_text

    assembled = {"context": {"used_tokens": 4000, "input_budget": 90000, "context_window": 100000}}
    state = apply_many(initial_state("s1"), [
        _ev(1, "turn.started", turn="t1"),
        _ev(2, "context.assembled", {"iteration": 1, **assembled}, turn="t1"),
    ])
    assert context_measure(state) == (4000, 100000, False)
    # Anthropic-style usage: the loop reports the whole prompt, cached tokens included.
    state = apply(state, _ev(3, "model.usage", {"input": 20, "output": 100, "cache_read": 2900, "prompt": 3000}, turn="t1"))
    assert context_measure(state) == (3100, 100000, True)
    assert context_usage(state) == "3k (3%)"
    # The next iteration's estimate grew by 700; the measurement grows by the same amount.
    state = apply(state, _ev(4, "context.assembled", {"iteration": 2, "context": {**assembled["context"], "used_tokens": 4700}}, turn="t1"))
    assert context_measure(state) == (3700, 100000, True)
    # A log without ``prompt`` falls back to ``input``.
    state = apply(state, _ev(5, "model.usage", {"input": 3800, "output": 50}, turn="t1"))
    assert context_measure(state)[0] == 3850
    assert turn_footer_text(state.turns[0]).endswith("turn ↑6.7K ↓150 · 43% cached")
    # Reasoning shows as a bare ``r`` count, with no hidden/shown distinction.
    with_reasoning = apply(state, _ev(6, "model.usage", {"input": 0, "output": 0, "reasoning": 800}, turn="t1"))
    assert turn_footer_text(with_reasoning.turns[0]).endswith("43% cached · 800 r")
    assert fold([
        _ev(1, "turn.started", turn="t1"),
        _ev(2, "context.assembled", {"iteration": 1, **assembled}, turn="t1"),
        _ev(3, "model.usage", {"input": 20, "output": 100, "cache_read": 2900, "prompt": 3000}, turn="t1"),
    ]).context["context"]["measured_tokens"] == 3100


def test_turn_footer_names_a_limit_stop():
    from nexus.ui_support.timeline import turn_footer_text

    def footer(data):
        return turn_footer_text(apply_many(initial_state("s1"), [
            _ev(1, "turn.started", turn="t1"),
            _ev(2, "turn.completed", data, turn="t1"),
        ]).turns[0])

    # The turn clock leads the footer; the limit stop comes right after it.
    assert footer({"stop_reason": "budget", "limit": "max_seconds"}).split(" · ")[1] == "stopped by turn limit"
    assert footer({"stop_reason": "max_iterations"}).split(" · ")[1] == "stopped by iteration limit"
    assert "stopped" not in footer({"stop_reason": "end_turn"})


def test_context_usage_without_a_window_uses_the_input_budget():
    from nexus.ui_support.context import context_measure

    state = apply_many(initial_state("s1"), [
        _ev(1, "turn.started", turn="t1"),
        _ev(2, "context.assembled", {"iteration": 1, "context": {"used_tokens": 500, "input_budget": 8000}}, turn="t1"),
        _ev(3, "model.usage", {"input": 0, "output": 5}, turn="t1"),
    ])
    assert context_measure(state) == (500, 8000, False)


def test_failed_turn_finishes_unresolved_task_for_live_and_replay():
    events = [_ev(1, "turn.started", turn="t"),
              _ev(2, "tool.requested", {"call_id": "task", "tool": "subagent"}, turn="t"),
              _ev(3, "turn.failed", {"error": "SubagentError: invalid model"}, turn="t")]
    before = fold(events[:2])
    live = apply(before, events[-1])
    tool = live.turns[0].tools[0]
    assert tool.status == "failed" and tool.is_error
    assert tool.error == "SubagentError: invalid model"
    assert before.turns[0].tools[0].status == "requested"
    assert live.to_dict() == fold(events).to_dict()


def test_overload_retry_keeps_interrupted_text_separate_and_replays():
    events = [_ev(1, "turn.started", turn="t"),
              _ev(2, "model.started", {"iteration": 1, "model": "m"}, turn="t"),
              _ev(3, "text.delta", {"text": "partial"}, turn="t"),
              _ev(4, "model.retrying", {"reason": "provider_overloaded", "attempt": 1, "delay_seconds": 2}, turn="t"),
              _ev(5, "model.started", {"iteration": 1, "model": "m", "attempt": 1}, turn="t"),
              _ev(6, "text.delta", {"text": "recovered"}, turn="t")]
    view = fold(events)
    assert [m.text for m in view.turns[0].messages] == ["partial", "recovered"]
    assert view.turns[0].messages[0].done
    assert view.turns[0].retries[0].delay_seconds == 2
    assert fold(events[4:], fold(events[:4])).to_dict() == view.to_dict()


@pytest.mark.parametrize('kind', ['turn.completed', 'turn.failed', 'turn.cancelled'])
def test_terminal_timestamp_survives_replay(kind):
    events = [_ev(1, 'turn.started', {}, turn='t', ts=100),
              _ev(2, kind, {}, turn='t', ts=123)]
    view = fold(events)
    assert view.turns[0].completed_ts == 123
    assert fold(events[1:], fold(events[:1])).to_dict() == view.to_dict()


# ---------------------------------------------------------------------------
# Code mode (plan 03 section 3.12)
# ---------------------------------------------------------------------------


def test_code_calls_fold_by_nested_id_in_start_order_and_replay_equals_live():
    events = _load_events("code_mode")
    live = initial_state()
    for event in events:
        live = apply(live, event)
    assert live.to_dict() == fold(events).to_dict()
    tool = live.turns[0].tools[0]
    assert [row["nested_id"] for row in tool.nested_calls] == [f"c1.{n}" for n in range(1, 6)]
    assert [row["status"] for row in tool.nested_calls] == ["ok", "error", "denied", "ok", "ok"]
    assert tool.nested_calls[2]["permission"]["rule"] == "Write(/etc/**)"
    assert tool.nested_calls[4]["output_tokens"] == 9
    assert tool.code_mode["purpose"] == "audit TODOs" and tool.code_mode["status"] == "ok"
    assert live.code_store == {"n": 3, "note": "done"}


def test_running_nested_rows_are_cancelled_when_the_program_finishes():
    events = _load_events("code_mode")
    until_running = [e for e in events if e.seq <= 23]  # c2.1 still running
    mid = fold(until_running).turns[1].tools[0]
    assert mid.nested_calls[0]["status"] == "running"
    done = fold(events).turns[1].tools[0]
    assert done.nested_calls[0]["status"] == "cancelled" and done.code_mode["status"] == "cancelled"
