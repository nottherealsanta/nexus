"""A code-mode program with ok/error/denied nested rows in a real PTY (plan 03 section 7, test 11).

Build rust/tui first. The `code.*` events come from the reducer fixture, so no Monty is needed:
the reduced tool goes through the same `tool_signal` projection the bridge sends.
"""
import json
import time
from pathlib import Path

import pytest
from test_ratatui_pty import BINARY

from nexus.events import Event
from nexus.ui_support.tool_signal import tool_signal
from nexus.view import fold

FIXTURE = Path(__file__).parent / "fixtures" / "view" / "code_mode.events.json"


def _tool(upto=None):
    events = [Event(**e) for e in json.loads(FIXTURE.read_text())]
    view = fold([e for e in events if upto is None or e.seq <= upto])
    return view.turns[0].tools[0]


def _block(tool):
    return {"id": "g", "kind": "tool_group", "text": "code · 1 call", "local_ui": True,
            "operation": {"kind": "block_toggle", "id": "g"}, "members": [
                {"id": tool.call_id, "kind": "tool", "heading": "code · audit TODOs", "local_ui": True,
                 "operation": {"kind": "block_toggle", "id": "c1:detail"},
                 "output_operation": {"kind": "block_toggle", "id": "c1:output"},
                 "fold_lines": 2, "local_detail": "details", "signal": tool_signal(tool).wire()}]}


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_program_with_ok_error_and_denied_nested_rows_renders_live():
    from ratatui_latency_probe import NativeProbe
    from ratatui_screen import Screen
    probe = NativeProbe(BINARY)
    try:
        # Mid-run: only the first calls have arrived (event seq 7: c1.1 and c1.2 running).
        probe.send({"schema": 3, "reset": True, "revision": 1, "generation": 1,
                    "blocks": [_block(_tool(7))], "sessions_sidebar": False, "details_sidebar": False})
        deadline = time.monotonic() + 4
        while b"1 call" not in probe.output and probe.process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        probe.keys(b"\t\r")  # open the group and the call, as in the disclosure test
        time.sleep(.2)
        probe.keys(b"\t\r")
        time.sleep(.3)
        screen = Screen(40, 140)
        screen.feed(bytes(probe.output))
        assert "… 1. grep" in screen.text(), screen.text()
        assert "audit TODOs" in screen.text()
        # The finished program: glyph rows, denial rule and the model row.
        probe.send({"schema": 3, "revision": 2, "generation": 1, "blocks_from": 0, "blocks": [_block(_tool())]})
        time.sleep(.4)
        screen = Screen(40, 140)
        screen.feed(bytes(probe.output))
        text = screen.text()
        for want in ("audit TODOs", "✓ 1. grep", "✗ 2. read", "⊘ 3. write", "rule Write(/etc/**)",
                     "1  files = grep(", "claude-x 120→9 tok"):
            assert want in text, f"{want!r} missing:\n{text}"
        assert probe.process.poll() is None
    finally:
        probe.close()


# -- kernel notebook rows (kernel plan section 3.11) ----------------------------------------------

KERNEL_FIXTURE = Path(__file__).parent / "fixtures" / "view" / "kernel_cells.events.json"


def _group_label(tool, group):
    """The header text the real verb-group projection gives a group holding only this tool."""
    from collections import defaultdict
    from types import SimpleNamespace
    from nexus.ui.ratatui.verb_groups import build_run
    pending = [(0, 0, {"id": tool.call_id}, 0, 0, "tool")]
    block = build_run(pending, {tool.call_id: tool}, defaultdict(list), turn=SimpleNamespace(id=group, phase="done"),
                      shell=None, local_ui=True)[0][2]
    return block["text"]


def _kernel_block(tool, group="kg"):
    from nexus.ui.ratatui.codemode import member_fields
    member = {"id": tool.call_id, "kind": "tool", "status": "completed", "title": tool.name, "text": "",
              "local_ui": True, "operation": {"kind": "block_toggle", "id": tool.call_id + ":detail"},
              "output_operation": None, "fold_lines": 2, "local_detail": "details",
              "signal": tool_signal(tool).wire()}
    member.update(member_fields(tool, member))
    return {"id": group, "kind": "tool_group", "text": _group_label(tool, group), "local_ui": True,
            "operation": {"kind": "block_toggle", "id": group}, "members": [member]}


def _open_and_dump(blocks, wait_for):
    from ratatui_latency_probe import NativeProbe
    from ratatui_screen import Screen
    probe = NativeProbe(BINARY)
    try:
        probe.send({"schema": 3, "reset": True, "revision": 1, "generation": 1, "blocks": blocks,
                    "sessions_sidebar": False, "details_sidebar": False})
        deadline = time.monotonic() + 4
        while wait_for.encode() not in probe.output and probe.process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        time.sleep(.4)
        for _ in range(2):  # open the group, then the first member
            probe.keys(b"\t\r")
            time.sleep(.4)
        screen = Screen(40, 140)
        screen.feed(bytes(probe.output))
        assert probe.process.poll() is None
        return screen.text()
    finally:
        probe.close()


def _kernel_tools():
    events = [Event(**e) for e in json.loads(KERNEL_FIXTURE.read_text())]
    return [t for turn in fold(events).turns for t in turn.tools]


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_notebook_cell_row_shows_in_header_source_out_and_nested_call():
    _, cell, failed, _ = _kernel_tools()
    text = _open_and_dump([_kernel_block(cell)], "In [1]")
    for want in ("In [1]", "✓ 0.04 s · 1 call · count issues", "1  issues = await mcp__github__list_issues",
                 "Out [1]: 42", "✓ 1. mcp__github__list_issues", "↳ 42 issues"):
        assert want in text, f"{want!r} missing:\n{text}"
    text = _open_and_dump([_kernel_block(failed, "kf")], "In [2]")
    for want in ("In [2]", "ZeroDivisionError", "(line 3): division by zero", "2. read"):
        assert want in text, f"{want!r} missing:\n{text}"


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_kernel_started_row_and_restore_notice_render():
    from test_codemode_replay import durability_events
    start = _kernel_tools()[0]
    text = _open_and_dump([_kernel_block(start, "ks")], "kernel started")
    for want in ("Started 1 kernel", "⧉ kernel started", "github (+2 tools)", "triage issues", "kernel: k0123456789abcde",
                 "not attached: server github: 1 of 2 tools"):
        assert want in text, f"{want!r} missing:\n{text}"
    restored = [t for turn in fold(durability_events()).turns for t in turn.tools][2]
    text = _open_and_dump([_kernel_block(restored, "kr")], "In [10]")
    assert "↻ restored after In [8], 1 cell lost" in text, text
    assert "kernel restored after In [8] (restart); missing: In [9]" in text, text
