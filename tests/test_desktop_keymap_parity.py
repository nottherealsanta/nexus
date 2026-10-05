"""Desktop key bindings must match the shared Ratatui shortcut table (DESKTOP_OVERHAUL_PLAN §5.3)."""
from __future__ import annotations

import re
from pathlib import Path

from nexus.ui_support.shortcuts import LEADER_SHORTCUTS, SHORTCUTS

KEYMAP = Path(__file__).resolve().parents[1] / "rust" / "desktop" / "src" / "keymap.rs"
ROW = re.compile(r'KeyBinding::new\("([^"]+)",\s*(\w+),\s*Some\("([^"]+)"\)\)')

#: shortcuts.py action id (or leader action) -> desktop action struct.
ACTIONS = {
    "command_palette": "Palette",
    "new_session": "NewSession",
    "list_sessions": "Sessions",
    "fork_session": "ForkSession",
    "pick_agent": "Agents",
    "toggle_sessions": "ToggleSessions",
    "toggle_details": "ToggleDetails",
    "open_settings": "Settings",
    "open_context": "InspectContext",
    "toggle_voice": "Voice",
    "show_usage": "Usage",
    "cancel_turn": "Cancel",
    "reconnect": "Reconnect",
    "quit_shell": "Quit",
    "open_model_picker": "Models",
    "start_voice": "Voice",
    "toggle_logs": "ToggleLogs",
    "cycle_reasoning_effort": "CycleEffort",
    "show_shortcuts": "Shortcuts",
    "context_popover": "ContextPopover",
    "update_help": "UpdateHelp",
}
#: Rows without an id in SHORTCUTS, keyed by the key string.
UNNAMED = {
    "ctrl+t": "CycleEffort",
    "ctrl+e": "ToggleLogs",
    "shift+tab": "CycleAgent",
    "escape": "Dismiss",
    "a": "Agents",
}
#: Rows handled by the editor itself and never expressed as a single keystroke.
#: Shrink this set, never grow it silently.
KNOWN_GAPS = {
    "enter", "ctrl+enter", "alt+enter", "shift+enter",  # editor-level (ctrl/alt+enter bound in "Editor")
}


def _rows():
    return [(k, a, c) for k, a, c in ROW.findall(KEYMAP.read_text())]


def _global(key: str):
    # The global context plus the scoped `Nexus && …` contexts resolve from the
    # root, so a route in either satisfies global reachability.
    return {a for k, a, c in _rows() if k == key and c.split()[0] == "Nexus"}


def test_shared_shortcuts_have_matching_desktop_routes():
    for key, action_id, _ in SHORTCUTS:
        if key in KNOWN_GAPS:
            continue
        if key == "escape twice":
            # A timed gesture, not a single keystroke; see the dedicated test.
            continue
        want = ACTIONS.get(action_id) if action_id else UNNAMED.get(key)
        assert want, f"no desktop action mapped for {key}"
        assert want in _global(key.replace("+", "-")), f"{key} should route to {want}"


def test_escape_twice_stops_the_turn_in_the_desktop_shell():
    # "escape twice" is a timed gesture, not a single keystroke, so it cannot be
    # a keymap row. The shell must implement the 1.5 s cancel window.
    source = (KEYMAP.parent / "main.rs").read_text()
    assert "duration_since" in source
    assert "from_millis(1500)" in source
    assert '{"type":"cancel"}' in source


def test_leader_shortcuts_have_matching_desktop_routes():
    for letter, action_id, _ in LEADER_SHORTCUTS:
        if action_id in KNOWN_GAPS:
            continue
        assert ACTIONS[action_id] in _global(f"ctrl-x {letter}"), f"ctrl+x {letter} -> {action_id}"


def test_scoped_picker_keys_never_shadow_global_ones_in_the_same_context():
    seen: dict[tuple[str, str], str] = {}
    for key, action, context in _rows():
        assert (key, context) not in seen, f"{key} bound twice in {context}"
        seen[(key, context)] = action
