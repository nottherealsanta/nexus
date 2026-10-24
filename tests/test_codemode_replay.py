"""Replay equals live for the code-mode events; old ``code.*`` sessions reduce and render unchanged.

The removed one-shot ``code`` tool wrote ``code.started`` / ``code.call`` / ``code.store`` /
``code.finished``; ``code_mode.events.json`` pins that history. ``kernel_cells.events.json``
holds the kernel events (kernel plan §3.8, the Phase 2 subset).
"""
from __future__ import annotations

import json
from pathlib import Path

from nexus.events import CODE_EVENTS, Event
from nexus.ui_support.tool_details import sections_to_text, tool_detail_sections
from nexus.view import apply, fold, initial_state

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "view"


def load(name):
    return [Event(**e) for e in json.loads((FIXTURES / f"{name}.events.json").read_text(encoding="utf-8"))]


def tools(view):
    return [tool for turn in view.turns for tool in turn.tools]


def test_old_code_events_are_still_known_and_reduce_unchanged():
    events = load("code_mode")
    kinds = {e.type for e in events if e.type.startswith("code.")}
    assert kinds == {"code.started", "code.call", "code.store", "code.finished"}
    assert kinds <= set(CODE_EVENTS)
    view = fold(events)
    assert view.to_dict() == json.loads((FIXTURES / "code_mode.expected.json").read_text(encoding="utf-8"))
    row = tools(view)[0]
    assert row.name == "code" and row.nested_calls and row.code_mode["status"] == "ok"
    assert view.code_store == {"n": 3, "note": "done"}
    text = sections_to_text(tool_detail_sections(row))
    for expected in ("Code · audit TODOs", "Nested call 5", "Store writes", "1  files = grep("):
        assert expected in text


def test_kernel_and_durability_events_are_in_the_catalogue():
    assert {"code.kernel_started", "code.kernel_attached", "code.cell_started", "code.cell_finished",
            "code.kernel_reset", "code.kernel_checkpointed", "code.kernel_restored",
            "code.kernel_lost"} <= set(CODE_EVENTS)


def durability_events():
    """Cell, checkpoint; then (after a restart) a restored run; then a lost run (kernel plan §3.8)."""
    seq = iter(range(1, 100))

    def ev(type_, data, turn="t1"):
        n = next(seq)
        return Event(type=type_, data=data, seq=n, ts=float(n), session="s1", turn=turn, id=f"e{n:03d}")

    def tool(name, call, turn, *specs):
        out = [ev("tool.requested", {"call_id": call, "tool": name, "input": {"purpose": "p"}}, turn),
               ev("tool.started", {"call_id": call, "tool": name}, turn)]
        out += [ev(kind, data, turn) for kind, data in specs]
        out.append(ev("tool.completed", {"call_id": call, "tool": name, "executed": True, "is_error": False,
                                         "result": {"content": [{"type": "text", "text": "ok"}]}}, turn))
        return out

    kid = "k0123456789abcde"
    out = [ev("turn.started", {"limits": {}})]
    out += tool("code_run", "c1", "t1",
                ("code.cell_started", {"kernel_id": kid, "call_id": "c1", "n": 8, "source": "x = 1"}),
                ("code.cell_finished", {"kernel_id": kid, "call_id": "c1", "n": 8, "status": "ok"}),
                ("code.kernel_checkpointed", {"kernel_id": kid, "call_id": "c1", "after_n": 8, "bytes": 120,
                                                "sha256": "ab" * 32, "status": "written"}))
    out += tool("code_run", "c2", "t1",
                ("code.cell_started", {"kernel_id": kid, "call_id": "c2", "n": 9, "source": "y = 2"}),
                ("code.cell_finished", {"kernel_id": kid, "call_id": "c2", "n": 9, "status": "ok"}),
                ("code.kernel_checkpointed", {"kernel_id": kid, "call_id": "c2", "after_n": 9, "bytes": 9000000,
                                                "sha256": "cd" * 32, "status": "too_large", "kept_after_n": 8,
                                                "max_snapshot_bytes": 8388608}))
    out.append(ev("turn.completed", {"iterations": 1, "stop_reason": "end_turn"}))
    out.append(ev("turn.started", {"limits": {}}, "t2"))
    out += tool("code_run", "c3", "t2",
                ("code.kernel_restored", {"kernel_id": kid, "call_id": "c3", "from_n": 8, "reason": "restart",
                                            "missing_cells": [9], "missing_count": 1}),
                ("code.cell_started", {"kernel_id": kid, "call_id": "c3", "n": 10, "source": "x"}),
                ("code.cell_finished", {"kernel_id": kid, "call_id": "c3", "n": 10, "status": "ok"}))
    out += tool("code_run", "c4", "t2",
                ("code.kernel_lost", {"kernel_id": kid, "call_id": "c4", "reason": "invalid", "last_n": 10}))
    out.append(ev("turn.completed", {"iterations": 1, "stop_reason": "end_turn"}, "t2"))
    return out


def test_durability_events_replay_equals_live_and_fold_into_their_rows():
    events = durability_events()
    live = initial_state()
    for event in events:
        live = apply(live, event)
    assert live.to_dict() == fold(events).to_dict()
    assert fold([*events, *events]).to_dict() == fold(events).to_dict()
    first, second, restored, lost = tools(fold(events))
    assert first.code_mode["checkpoint"]["status"] == "written" and first.code_mode["n"] == 8
    assert second.code_mode["checkpoint"]["kept_after_n"] == 8 and second.code_mode["n"] == 9
    assert restored.code_mode["restored"]["missing_cells"] == [9] and restored.code_mode["n"] == 10
    assert lost.code_mode["lost"]["reason"] == "invalid"
    assert all(t.status == "completed" for t in (first, second, restored, lost))


def test_durability_rows_render_as_labelled_sections():
    first, second, restored, lost = tools(fold(durability_events()))
    assert "Checkpoint" in sections_to_text(tool_detail_sections(first))
    text = sections_to_text(tool_detail_sections(second))
    assert "not written (over max_snapshot_bytes)" in text and "kept (after In [8])" in text
    text = sections_to_text(tool_detail_sections(restored))
    assert "Kernel restored" in text and "Restored from after: In [8]" in text and "In [9]" in text
    text = sections_to_text(tool_detail_sections(lost))
    assert "Kernel lost" in text and "Reason: invalid" in text and "reset empty" in text


def test_kernel_events_replay_equals_live_step_by_step():
    events = load("kernel_cells")
    live = initial_state()
    for event in events:
        live = apply(live, event)
    assert live.to_dict() == fold(events).to_dict()
    # A snapshot plus the tail equals the whole log.
    middle = len(events) // 2
    head = fold(events[:middle])
    resumed = head
    for event in events[middle:]:
        resumed = apply(resumed, event)
    assert resumed.to_dict() == fold(events).to_dict()


def test_kernel_cells_fold_into_their_tool_rows():
    start, first, second, reset = tools(fold(load("kernel_cells")))
    assert (start.name, first.name, second.name, reset.name) == ("code_start", "code_run", "code_run", "code_reset")
    assert start.code_mode["attached"]["servers"] == ["github"] and start.code_mode["agent_path"] == "root"
    assert first.code_mode["n"] == 1 and first.code_mode["status"] == "ok" and first.code_mode["out_summary"] == "42"
    assert [c["status"] for c in first.nested_calls] == ["ok"]
    assert all(c["parent_call_id"] == "k1" for c in first.nested_calls)
    # The failed cell: one call finished, the one still running was cut off, never a spinner.
    assert second.is_error and second.code_mode["diagnostic"]["kind"] == "ZeroDivisionError"
    assert [c["status"] for c in second.nested_calls] == ["ok", "cancelled"]
    assert reset.code_mode["reason"] == "reset" and reset.code_mode["stopped"] is False
    assert all(t.status == "completed" for t in (start, first, second, reset))


def test_kernel_rows_render_as_labelled_sections_not_json():
    start, first, second, reset = tools(fold(load("kernel_cells")))
    text = sections_to_text(tool_detail_sections(start))
    assert "Code_start · triage issues" in text and "Kernel: k0123456789abcde" in text
    assert "Servers: github" in text and "Attached tools: 3:" in text
    assert "Not attached (1)" in text and "refused by [code_mode] expose" in text
    cell = sections_to_text(tool_detail_sections(first))
    for expected in ("Code_run · count issues", "Cell (In [n]): 1", "1  issues = await", "Nested call 1", "Out: 42"):
        assert expected in cell, cell
    failed = sections_to_text(tool_detail_sections(second))
    assert "Kind: ZeroDivisionError" in failed and "– 2. read" in failed
    assert "Stopped: no (variables cleared)" in sections_to_text(tool_detail_sections(reset))


def test_replaying_the_log_twice_changes_nothing():
    events = load("kernel_cells")
    once = fold(events)
    twice = fold([*events, *events])
    assert once.to_dict() == twice.to_dict()
