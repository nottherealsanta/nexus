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

GOLDEN_LOGS = ("text_stream", "multi_turn_tools", "subagent", "reconnect")


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
