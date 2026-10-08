"""Tools list, skill cards and the compact detail page in a real controlling PTY (CONTEXT_SECTIONS_PLAN §6).

Build rust/tui first. This developer integration test needs controlling-TTY access.
"""
import re
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


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_mcp_list_is_thin_with_refresh_restart_and_toggle():
    t = Terminal(36, 160)
    try:
        restart = {"kind": "mcp_restart", "name": "tracker"}
        items = [{"label": "↻ Refresh all · re-read mcp.json, reconnect servers", "operation": {"kind": "mcp_refresh"}},
                 {"label": "tracker", "operation": {"kind": "server"}, "trailing": "~24 / ~1.4K",
                  "toggle_enabled": True, "toggle_operation": {"kind": "context_toggle"},
                  "action_label": "Restart", "action_operation": restart},
                 {"label": "Edit MCP servers…", "operation": {"kind": "settings"}}]
        screen = screen_after(t, panel_title="MCP · 1 of 1 on · ~24 indexed / ~1.4K full", panel_layout="list", items=items)
        for want in ("↻ Refresh all", "tracker", "~24 / ~1.4K", "[ Restart ]", "[ ON ]", "~24 indexed / ~1.4K full"):
            assert want in screen, f"{want!r} missing:\n{screen}"
        row = re.search(r"tracker +~24 / ~1\.4K +\[ Restart \] +\[ ON \]", screen)
        assert row and len(row.group()) <= 72, f"the MCP list should stay thin:\n{screen}"
        t.key(b"\r")
        assert t.action()["operation"] == {"kind": "mcp_refresh"}
        t.key(b"\x1b[B")
        t.key(b"\x12")  # Ctrl+R on the selected row restarts that server
        assert t.action()["operation"] == restart

        page = [{"label": "connected · stdio · project", "operation": {"kind": "server"}, "group": "Server",
                 "action_label": "Restart", "action_operation": {**restart, "page": True},
                 "toggle_enabled": True, "toggle_operation": {"kind": "context_toggle"}},
                {"label": "- server: tracker · health: ready · mode: search · 2 tools", "operation": {"kind": "index"},
                 "group": "Indexed · ~24 tokens in context now", "lines": ["  tools: create_issue, list; use McpSearch"]},
                {"label": "create_issue", "operation": {"kind": "tool_show"}, "group": "Full · ~1.4K tokens · 2 tools",
                 "trailing": "~900", "toggle_enabled": True, "toggle_operation": {"kind": "context_toggle"}},
                {"label": "list", "operation": {"kind": "tool_show"}, "group": "Full · ~1.4K tokens · 2 tools",
                 "trailing": "~500", "toggle_enabled": False, "toggle_operation": {"kind": "context_toggle"}}]
        screen = screen_after(t, panel_title="MCP · tracker · ~24 indexed / ~1.4K full", panel_layout="list", items=page, generation=2)
        for want in ("Server", "Indexed · ~24", "tools: create_issue, list", "Full · ~1.4K", "create_issue", "~900", "[ OFF ]"):
            assert want in screen, f"{want!r} missing:\n{screen}"
        assert screen.index("Indexed · ~24") < screen.index("tools: create_issue") < screen.index("Full · ~1.4K")
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_ctrl_c_clears_a_draft_before_it_cancels():
    t = Terminal(30, 120)
    try:
        t.send(composer_key="w/s1", generation=1)
        time.sleep(.4)
        t.key(b"half a thought")
        t.key(b"\x03")  # Ctrl+C with text: clear, never cancel
        t.key(b"x\r")
        assert t.action() == {"type": "submit", "text": "x", "mode": "steer", "generation": 1}
        t.key(b"\x03")  # empty composer: cancel the turn as before
        assert t.action()["type"] == "cancel"
    finally:
        t.close()
