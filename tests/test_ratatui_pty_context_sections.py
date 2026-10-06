"""Tools list, skill cards and the compact detail page in a real controlling PTY (CONTEXT_SECTIONS_PLAN §6).

Build rust/tui first. This developer integration test needs controlling-TTY access.
"""
import time

import pytest
from test_ratatui_pty_sessions import BINARY, Terminal

TOGGLE = {"kind": "context_toggle", "category": "tools", "name": "edit", "enabled": False}


def screen_after(t, **fields):
    t.send(generation=fields.pop("generation", 1), **fields)
    time.sleep(.4)
    t.redraw()
    return t.text()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_tools_list_cards_and_detail_page_render_and_operate():
    t = Terminal(36, 110)
    try:
        rows = [{"label": name, "operation": {"kind": "tool_show", "name": name}, "trailing": tokens, "toggle_enabled": on,
                 "toggle_operation": {**TOGGLE, "name": name}} for name, tokens, on in
                [("bash", "~1.4K", True), ("edit", "~620", True), ("mcp__mock_tracker__create_issue", "~340", False)]]
        screen = screen_after(t, panel_title="Tools · 2 of 3 on · ~2.0K tokens", panel_layout="list", items=rows)
        for want in ("Tools · 2 of 3 on", "bash", "~1.4K", "mcp__mock_tracker__create_issue", "[ OFF ]"):
            assert want in screen, f"{want!r} missing:\n{screen}"
        t.key(b"\x1b[B")
        t.key(b" ")
        assert t.action()["operation"] == {**TOGGLE, "name": "edit"}
        t.key(b"\r")
        assert t.action()["operation"] == {"kind": "tool_show", "name": "edit"}

        cards = [{"label": "code-review  project", "operation": {"kind": "skill"}, "toggle_enabled": True,
                  "toggle_operation": {"kind": "context_toggle"},
                  "lines": ["description  Review a diff.", "allowed-tools  read, grep", "~38 in context · ~1.9K full skill"]},
                 {"label": "Show literal index", "operation": {"kind": "index"}}]
        screen = screen_after(t, panel_title="Skills · 1 of 1 on", panel_layout="context", items=cards, generation=2)
        for want in ("code-review  project", "description  Review a diff.", "allowed-tools  read, grep", "~38 in context", "Show literal index"):
            assert want in screen, f"{want!r} missing:\n{screen}"

        page = ["*Space switches this tool on or off.*", "", "## Parameters", "| Name | Type |", "| --- | --- |", "| `path` | string |"]
        screen = screen_after(t, panel_title="Tool · edit · on · ~620 tokens", panel_layout="detail", panel_format="markdown",
                              items=[], panel_lines=page, panel_toggle=TOGGLE, generation=3)
        for want in ("Tool · edit", "Parameters", "path", "string"):
            assert want in screen, f"{want!r} missing:\n{screen}"
        t.key(b" ")
        assert t.action()["operation"] == TOGGLE
    finally:
        t.close()
