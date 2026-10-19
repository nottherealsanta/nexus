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
