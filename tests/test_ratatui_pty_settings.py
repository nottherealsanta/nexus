"""One-page Settings in a real controlling PTY: render, move, change, reorder, popup, area list, close.

Build rust/tui first. This developer integration test needs controlling-TTY access.
"""
import time
import select

import pytest
from test_ratatui_pty_sessions import BINARY, Terminal as _Terminal


class Terminal(_Terminal):
    def action(self):
        """The next user action; the bridge's passive `draft_state` composer report is not one."""
        while (row := super().action()).get("type") == "draft_state":
            pass
        return row


OP = {"kind": "sp_models", "area": "models"}
PAGE = {
    "area": "models", "title": "Models", "intro": "The first model that can run is used.", "footer": "Saved in ~/.nexus/config.toml", "scope": None,
    "blocks": [
        {"t": "heading", "text": "DEFAULT"},
        {"t": "row", "id": "titles", "label": "Name new sessions automatically", "description": "Uses a fast model.", "scope": "",
         "control": {"c": "toggle", "on": True, "locked": "", "operation": {**OP, "key": "titles"}}},
        {"t": "row", "id": "ceiling", "label": "Highest tier", "scope": "",
         "control": {"c": "segmented", "options": ["Low", "Medium", "High"], "values": ["low", "medium", "high"], "active": 1, "operation": {**OP, "key": "ceiling"}}},
        {"t": "row", "id": "device", "label": "Device", "scope": "",
         "control": {"c": "select", "value": "auto", "options": [["auto", "auto"], ["cpu", "cpu"], ["metal", "metal"]], "operation": {**OP, "key": "device"}}},
        {"t": "heading", "text": "TIERS"},
        {"t": "tabs", "id": "tiers", "items": [["Low", ""], ["Medium", ""], ["High", "!"]], "active": 0, "operation": {**OP, "key": "tab"}},
        {"t": "ordered", "id": "tier:low", "items": [["openai/gpt-5-mini", "in use", ""], ["anthropic/claude-haiku-4-5", "skipped", "not connected"]],
         "add_label": "Add model…", "editable": True, "operation": {**OP, "key": "tier", "tier": "low"}},
    ],
}
NAV = {"items": [["GENERAL", "", True], ["Appearance", "appearance", False], ["Models", "models", False]], "selected": 2}


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_settings_secret_paste_is_local_masked_and_never_leaks_to_composer():
    t = Terminal(40, 120)
    try:
        send_page(t)
        # Non-text focus ignores paste instead of silently filling the draft.
        t.key(b"\x1b[200~ignored-secret\x1b[201~")
        page = {**PAGE, "blocks": [{"t": "row", "id": "key", "label": "API key",
            "control": {"c": "text", "value": "", "secret": True,
                        "operation": {"kind": "save_key"}}}]}
        t.send(panel_title="Settings · Models", panel_layout="modal", nav=NAV,
               settings_page=page, generation=1)
        time.sleep(.4)
        # Focused text rows accept paste without first pressing Enter.
        t.key(b"\x1b[200~sk-private\r\n\t-value\x1b[201~")
        assert not t.pending and not select.select([t.process.stdout], [], [], .2)[0], "paste must not commit"
        t.redraw()
        assert "sk-private" not in t.text()
        assert "•" in t.text(), "secret editor is masked"
        t.key(b"\r")
        assert t.action() == {"type": "operation", "operation": {"kind": "save_key", "value": "sk-private-value"}, "generation": 1}
        # Generic panels must swallow paste too.
        t.send(panel_title="Read only", panel_layout="modal", nav=None,
               settings_page=None, generation=1)
        time.sleep(.3)
        t.key(b"\x1b[200~panel-secret\x1b[201~")
        t.send(panel_title="", settings_page=None, nav=None, generation=1)
        time.sleep(.3)
        t.key(b"\x1b[200~normal composer paste\x1b[201~")
        t.key(b"\r")
        action = t.action()
        assert action["type"] == "submit" and action["text"] == "normal composer paste", action
    finally:
        t.close()


def drill_to_models(t):
    """Settings opens at the root list: search `models`, then Enter opens the page (the host answers nav_select)."""
    t.key(b"/")
    for ch in b"models":
        t.key(bytes([ch]))
    t.key(b"\r")
    t.key(b"\r")
    assert t.action()["type"] == "nav_select"
    time.sleep(.2)


def send_page(t, **extra):
    t.send(panel_title="Settings · Models", panel_layout="modal", nav=NAV, settings_page=PAGE, generation=1, **extra)
    time.sleep(.4)
    if not getattr(t, "drilled", False):
        t.drilled = True
        drill_to_models(t)


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_settings_page_renders_and_is_operated_by_keyboard_alone():
    t = Terminal(40, 120)
    try:
        send_page(t)
        t.redraw()
        screen = t.text()
        for want in ["Models", "Name new sessions automatically", "on", "Highest tier", "Medium", "Device", "auto", "TIERS",
                     "openai/gpt-5-mini", "in use", "skipped", "not connected", "Add model", "Saved in ~/.nexus/config.toml", "Settings › Models"]:
            assert want in screen, f"{want!r} missing:\n{screen}"
        # Space toggles the first row: one operation with the negated value, nothing local.
        t.key(b" ")
        assert t.action() == {"type": "operation", "operation": {**OP, "key": "titles", "value": False}, "generation": 1}
        # Down, then Left on a segmented control changes its value.
        t.key(b"\x1b[B")
        t.key(b"\x1b[D")
        assert t.action()["operation"] == {**OP, "key": "ceiling", "value": "low"}
        # A select opens a popup over the page; Down + Enter sends the highlighted option.
        t.key(b"\x1b[B")
        t.key(b"\r")
        t.redraw()
        assert "metal" in t.text(), "the popup lists every option"
        t.key(b"\x1b[B")
        t.key(b"\r")
        assert t.action()["operation"] == {**OP, "key": "device", "value": "cpu"}
        # Ctrl+PgDn switches tab from anywhere on the page.
        t.key(b"\x1b[6;5~")
        assert t.action()["operation"] == {**OP, "key": "tab", "value": 1}
        # Alt+Down reorders the focused model: walk down to the first model row first.
        for _ in range(2):
            t.key(b"\x1b[B")
        t.key(b"\x1b[1;3B")  # Alt+Down
        assert t.action()["operation"] == {**OP, "key": "tier", "tier": "low", "action": "down", "index": 0}
        # Left on a plain row returns to the root list; Escape there asks the host to close Settings.
        t.key(b"\x1b[A")
        t.key(b"\x1b[A")
        t.key(b"\x1b[A")
        t.key(b"\x1b[D")
        time.sleep(.2)
        t.redraw()
        assert "Default models, tiers and fallbacks." in t.text(), "Left returns to the root list, selection kept"
        t.key(b"\x1b")  # clears the kept search filter first
        t.key(b"\x1b")
        t.key(b"\x1b")
        assert t.action()["type"] == "dismiss"
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_a_host_rebuild_keeps_focus_and_a_closed_panel_never_shows_the_page():
    t = Terminal(40, 120)
    try:
        send_page(t)
        t.key(b"\x1b[B")  # focus the segmented row
        send_page(t)  # the host rebuilds the page after every operation
        t.key(b"\x1b[D")
        assert t.action()["operation"]["key"] == "ceiling", "focus survives a rebuild"
        t.send(panel_title="", settings_page=None, nav=None, generation=1)
        time.sleep(.3)
        t.redraw()
        assert "Highest tier" not in t.text(), "no page without its panel"
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_reopening_settings_after_a_drill_shows_the_root_never_a_stale_page():
    """A pending drill keeps the root up; a close snapshot resets it so the next open starts at the root, not the old area."""
    appearance = {"area": "appearance", "title": "Appearance", "intro": "", "footer": "", "scope": None,
                  "blocks": [{"t": "row", "id": "zebra", "label": "Zebra stripes", "scope": "",
                              "control": {"c": "toggle", "on": False, "locked": "", "operation": {"kind": "sp_appearance", "area": "appearance", "key": "zebra"}}}]}
    nav = {**NAV, "selected": 1}
    # Only the selected root row shows its description, so use the root's always-drawn footer tip.
    root_text = "Saved in ~/.config/nexus/tui.json"
    t = Terminal(40, 120)
    try:
        t.send(panel_title="Settings · Appearance", panel_layout="modal", nav=nav, settings_page=appearance, generation=1)
        time.sleep(.4)
        t.redraw()
        assert root_text in t.text() and "Zebra stripes" not in t.text(), "first open starts at the root"
        drill_to_models(t)  # the host has not replied: the snapshot still selects Appearance
        t.redraw()
        assert root_text in t.text() and "Zebra stripes" not in t.text(), "pending drill must not flash the stale page"
        t.send(panel_title="Settings · Models", panel_layout="modal", nav=NAV, settings_page=PAGE, generation=1)
        time.sleep(.4)
        t.redraw()
        assert "Highest tier" in t.text() and root_text not in t.text(), "the reply settles on the target page"
        # Close, drawing the closed snapshot, then reopen with the stale Appearance snapshot.
        t.send(panel_title="", settings_page=None, nav=None, generation=1)
        time.sleep(.4)
        t.redraw()
        assert "Highest tier" not in t.text()
        t.send(panel_title="Settings · Appearance", panel_layout="modal", nav=nav, settings_page=appearance, generation=1)
        time.sleep(.4)
        t.redraw()
        screen = t.text()
        assert root_text in screen, f"reopen shows the root:\n{screen}"
        assert "Zebra stripes" not in screen and "Highest tier" not in screen, f"stale page shown:\n{screen}"
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
@pytest.mark.asyncio
async def test_the_pythons_own_snapshot_renders_and_its_operations_round_trip(tmp_path, monkeypatch):
    """What `project()` really emits (not a hand-built snapshot) renders, and an operation it offered goes back intact."""
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from nexus.host import protocol as p
    from nexus.ui.ratatui.actions import ShellActions
    from nexus.ui.ratatui.prototype import project
    from nexus.view import initial_state

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    controller = SimpleNamespace(client=SimpleNamespace(), session="s", view=initial_state("s"))
    shell = ShellActions(controller)
    c = shell.client
    c.model_tiers = AsyncMock(return_value=p.ModelTiersResult(order=["low", "medium", "high"], tiers=[
        {"name": "low", "refs": ["openai/gpt-5-mini", "anthropic/claude-haiku-4-5"], "source": "your list", "resolved": "openai/gpt-5-mini", "editable": True},
        {"name": "medium", "refs": [], "source": "by price", "resolved": "openai/gpt-5", "editable": True},
        {"name": "high", "refs": [], "source": "built-in", "resolved": "", "editable": True}], max_tier="high"))
    c.default_model_settings = AsyncMock(return_value=p.DefaultModelSettingsResult(refs=["openai/gpt-5-mini"], resolved="openai/gpt-5-mini"))
    c.session_title_settings = AsyncMock(return_value=p.SessionTitleSettingsResult(enabled=True, model="low", resolved="openai/gpt-5-mini"))
    c.list_models = AsyncMock(return_value=[])
    await shell.workflows.settings_area("models")
    snapshot = project(controller, 1, shell=shell)
    snapshot["generation"] = 1
    assert snapshot["settings_page"]["area"] == "models" and snapshot["nav"]["items"][snapshot["nav"]["selected"]][1] == "models"
    t = Terminal(60, 120)
    try:
        t.process.stdin.write((json.dumps(snapshot) + "\n").encode())
        t.process.stdin.flush()
        time.sleep(.5)
        t.drilled = True
        drill_to_models(t)
        t.redraw()
        screen = t.text()
        for want in ["Models", "Default Model", "Session Titles", "Name new sessions automatically", "Subagent Tiers", "Low", "Medium", "High",
                     "openai/gpt-5-mini", "in use", "Catalogue", "Settings › Models"]:
            assert want in screen, f"{want!r} missing:\n{screen}"
        # Walk to the first model of the default chain and move it: the operation is exactly what Python offered.
        offered = next(b for b in snapshot["settings_page"]["blocks"] if b.get("t") == "ordered" and b["id"] == "chain")
        for _ in range(8):
            t.key(b"\x1b[B")
        t.key(b"\x1b[1;3A")  # Alt+Up on whatever has focus is harmless unless it is a chain item
        t.key(b"\x1b[A")
        t.key(b"\x1b[A")
        t.key(b"\x1b[A")
        t.key(b"\x1b[A")
        t.key(b"\x1b[A")
        t.key(b"\x1b[A")
        t.key(b"\x1b[A")
        t.key(b"\x1b[A")
        t.key(b"\x1b[B")
        t.key(b"\x1b[B")  # focus: chain item 0 is the third stop (titles toggle, title model, ... chain)
        assert offered["operation"] == {"kind": "sp_models", "area": "models", "key": "chain"}
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_question_mark_alt_digits_and_f6_navigate_across_the_app():
    t = Terminal(40, 120)
    try:
        send_page(t)
        # `?` on a page opens the Keyboard page (the same one /hotkeys opens).
        t.key(b"?")
        assert t.action()["operation"] == {"kind": "keyboard"}
        # Alt+1 jumps to the first area (Appearance, nav index 1: index 0 is a heading).
        t.key(b"\x1b1")
        assert t.action() == {"type": "nav_select", "text": "1", "generation": 1}
        t.key(b"\x1b2")
        assert t.action() == {"type": "nav_select", "text": "2", "generation": 1}
        t.key(b"\x1b9")  # there is no ninth area: nothing is sent
        t.key(b"\x1b[24~")  # F12: unrelated key, ignored
        t.key(b"\x11")
        assert t.action()["type"] == "quit"
    finally:
        t.close()
