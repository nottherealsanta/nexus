"""A failed ``code`` program leaves no spinning phantom row (call ids survive redaction)."""

from nexus.core.loop import _safe_code_event
from nexus.events import Event
from nexus.ui_support.timeline import tool_status
from nexus.view import fold

CALL = "call-ce4ea2c4-d054-4f0f-9e5c-43f55c57f804-23"  # 44 characters, like a real call id
DIGEST = "a" * 64


def _ev(seq, kind, data):
    return Event(id=f"e{seq}", seq=seq, session="s1", ts=float(seq), turn="t1", type=kind, data=data)


def test_code_event_keeps_ids_and_counters_but_still_redacts_secrets():
    safe = _safe_code_event({
        "call_id": CALL, "nested_id": f"{CALL}.4", "program_digest": DIGEST, "sub_tokens": 12,
        "input_tokens": 3, "purpose": "x" * 8 + " sk-" + "q" * 40,
        "args": {"token": "hunter2", "path": "nexus"}})
    assert safe["call_id"] == CALL and safe["nested_id"] == f"{CALL}.4"
    assert safe["program_digest"] == DIGEST
    assert safe["sub_tokens"] == 12 and safe["input_tokens"] == 3
    assert "sk-" not in safe["purpose"] or "q" * 40 not in safe["purpose"]
    assert safe["args"]["token"] == "***" and safe["args"]["path"] == "nexus"


def test_failed_program_finalizes_every_nested_step_and_the_row():
    events = [
        _ev(1, "turn.started", {"limits": {}}),
        _ev(2, "tool.requested", {"call_id": CALL, "tool": "code"}),
        _ev(3, "tool.started", {"call_id": CALL, "tool": "code"}),
        *[_ev(4 + i, "code.call", {"call_id": CALL, "index": i, "nested_id": f"{CALL}.{i}",
                                   "tool": "grep", "status": "running"}) for i in (1, 2, 3)],
        _ev(8, "code.call", {"call_id": CALL, "index": 3, "nested_id": f"{CALL}.3", "tool": "grep",
                             "status": "error"}),
        _ev(9, "code.finished", {"call_id": CALL, "status": "error",
                                 "diagnostic": {"kind": "ToolError", "message": "Not a directory", "line": 4}}),
        _ev(10, "tool.completed", {"call_id": CALL, "tool": "code", "is_error": True, "duration_ms": 261}),
    ]
    tools = fold(events).turns[0].tools
    assert [t.call_id for t in tools] == [CALL]
    assert tool_status(tools[0]) == "failed"
    assert [c["status"] for c in tools[0].nested_calls] == ["cancelled", "cancelled", "error"]


def test_replayed_redacted_ids_do_not_leave_a_running_phantom():
    # Sessions recorded before the fix persisted the code events' call_id as "***".
    events = [
        _ev(1, "turn.started", {"limits": {}}),
        _ev(2, "tool.requested", {"call_id": CALL, "tool": "code"}),
        _ev(3, "tool.started", {"call_id": CALL, "tool": "code"}),
        _ev(4, "code.call", {"call_id": "***", "index": 1, "nested_id": "***.1", "tool": "grep", "status": "running"}),
        _ev(5, "code.finished", {"call_id": "***", "status": "error"}),
        _ev(6, "tool.completed", {"call_id": CALL, "tool": "code", "is_error": True}),
    ]
    assert {tool_status(t) for t in fold(events).turns[0].tools} == {"failed"}
