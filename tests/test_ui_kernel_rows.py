"""Kernel lifecycle and notebook cell rows, the Kernel sidebar rows (kernel plan §3.11)."""
from __future__ import annotations

import json
from pathlib import Path

from test_codemode_replay import durability_events

from nexus.events import Event
from nexus.ui_support.details import kernel_rows
from nexus.ui_support.timeline import tool_heading, tool_row_text, tool_summary
from nexus.ui_support.tool_details import (
    _cell_sections,
    _kernel_sections,
    cell_result_parts,
    kernel_heading,
    sections_to_text,
    tool_detail_sections,
)
from nexus.ui_support.tool_signal import tool_signal
from nexus.view import fold

FIXTURE = Path(__file__).parent / "fixtures" / "view" / "kernel_cells.events.json"


def _view():
    return fold([Event(**e) for e in json.loads(FIXTURE.read_text())])


def _tools():
    return [tool for turn in _view().turns for tool in turn.tools]


def test_kernel_lifecycle_headings_name_servers_tools_and_purpose():
    start, run1, run2, reset = _tools()
    assert kernel_heading(start) == "⧉ kernel started github (+2 tools) · triage issues"
    assert tool_heading(start) == kernel_heading(start)
    assert kernel_heading(reset) == "⧉ kernel reset start over"
    assert tool_summary(start) == ""
    assert "completed" not in tool_row_text(start)


def test_cell_heading_is_the_notebook_header_with_status_time_calls_and_purpose():
    _, run1, run2, _ = _tools()
    assert kernel_heading(run1) == "In [1] ✓ 0.04 s · 1 call · count issues"
    assert kernel_heading(run2) == "In [2] ✗ ZeroDivisionError 0.01 s · 2 calls · fan out then fail"


def test_cell_sections_and_kernel_sections_are_labelled():
    start, run1, run2, reset = _tools()
    cell = {s.title: s for s in _cell_sections(run1, run1.code_mode)}
    assert set(cell) == {"In [1]", "Out [1]"}
    assert any(r.label == "Time (ms)" and r.value == "40" for r in cell["In [1]"].rows)
    assert cell["Out [1]"].rows[0].value == "42"
    text = sections_to_text(tool_detail_sections(run1))
    assert "Nested call 1" in text and "Out [1]" in text and "1  issues = await" in text
    [kernel] = _kernel_sections(start, start.code_mode)
    assert kernel.title == "Kernel" and any(r.label == "Kernel" and r.value == "k0123456789abcde" for r in kernel.rows)
    assert _kernel_sections(run1, run1.code_mode) == [] and _cell_sections(start, start.code_mode) == []
    assert "kernel reset" in sections_to_text(_kernel_sections(reset, reset.code_mode))


def test_cell_result_parts_split_the_labelled_blocks():
    parts = cell_result_parts("note\n\nIn [3]  ✓ 0.01 s · 0 calls\n\nstdout:\n  hi\n\nOut [3]: 7\n\ncalls:\n  1 x")
    assert parts["notice"] == "note" and parts["stdout"] == "hi" and parts["out"] == "Out [3]: 7"
    assert parts["calls"].startswith("calls:") and parts["head"].startswith("In [3]")


def test_notebook_signal_rows_show_source_out_and_nested_calls():
    _, run1, run2, _ = _tools()
    kinds = [tuple(r) for r in tool_signal(run1).wire()]
    text = "\n".join(t for _, t in kinds)
    assert "1  issues = await mcp__github__list_issues" in text
    assert ("kv", "Out [1]: 42") in kinds
    assert any(t.startswith("✓ 1. mcp__github__list_issues") for _, t in kinds)
    assert "↳ 42 issues" in text
    failed = "\n".join(t for _, t in tool_signal(run2).wire())
    assert "ZeroDivisionError (line 3): division by zero" in failed and "2. read" in failed


def test_restored_and_lost_rows_say_what_is_missing_and_why():
    _, _, restored, lost = [t for turn in fold(durability_events()).turns for t in turn.tools][:4]
    assert "↻ restored after In [8], 1 cell lost" in kernel_heading(restored)
    rows = tool_signal(restored).wire()
    assert any(k == "text" and "kernel restored after In [8] (restart); missing: In [9]" in t for k, t in rows)
    assert "kernel lost: invalid" in kernel_heading(lost)
    rows = tool_signal(lost).wire()
    assert any(k == "error" and "kernel lost: invalid (last cell In [10])" in t for k, t in rows)
    text = sections_to_text(tool_detail_sections(restored))
    assert "Kernel restored" in text and "In [9]" in text


def test_sidebar_kernel_rows_fold_from_the_view():
    assert kernel_rows(fold([])) == []
    rows = dict(kernel_rows(_view()))
    assert rows["Kernel"] == "k0123456789abcde" and rows["Attached"] == "github (+2 tools)"
    assert rows["Cells"] == "0" and rows["Snapshot"] == "none"  # the fixture ends with code_reset
    durable = dict(kernel_rows(fold(durability_events())))
    assert durable["Cells"] == "10" and durable["Liveness"] == "lost: invalid"
    assert durable["Snapshot"] == "none"


def test_verb_group_label_counts_each_kernel_tool_with_its_own_noun():
    from collections import defaultdict
    from types import SimpleNamespace
    from nexus.ui.ratatui.verb_groups import build_run
    tools = _tools()
    by_id = {t.call_id: t for t in tools}
    pending = [(i, 0, {"id": t.call_id}, 0, 0, "tool") for i, t in enumerate(tools)]
    [(_, _, block, *_)] = build_run(pending, by_id, defaultdict(list), turn=SimpleNamespace(id="t", phase="done"),
                                    shell=None, local_ui=True)
    assert block["text"] == "Started 1 kernel, Ran 2 cells, Reset 1 kernel"
    [(_, _, single, *_)] = build_run(pending[:1], by_id, defaultdict(list),
                                     turn=SimpleNamespace(id="t", phase="done"), shell=None, local_ui=True)
    assert single["text"] == "Started 1 kernel"
