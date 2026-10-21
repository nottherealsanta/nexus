"""The left sidebar in a real PTY: tabs, wrapping, tab clicks, Ctrl+B, divider drag and width bounds.

Build rust/tui first (`cargo build --manifest-path rust/tui/Cargo.toml`). Developer integration
test: it needs controlling-TTY access, like ``test_ratatui_pty_sessions.py`` whose helpers it reuses.
"""
import time

import pytest

from test_ratatui_pty_sessions import BINARY, Terminal

PROJECTS = {"loaded": True, "truncated": False, "error": "", "rows": [
    {"kind": "project", "key": "/w", "depth": 0, "label": "nexus", "count": 3, "age": "5m", "rank": 2,
     "expandable": True, "current": True, "workspace": "", "session": ""},
    {"kind": "checkout", "key": "/w|/w", "parent": "/w", "depth": 1, "label": "main", "count": 2, "age": "5m",
     "rank": 2, "expandable": False, "current": True, "workspace": "/w", "session": "s1"},
    {"kind": "checkout", "key": "/w|/wt", "parent": "/w", "depth": 1, "label": "feature-x", "count": 1,
     "age": "1h", "rank": 2, "expandable": False, "current": False, "workspace": "/wt", "session": "s2"},
]}


def sidebar(**fields):
    return {"available": True, "open": True, "tab": "Projects", "width": 34, "focus": 0, "sessions": None,
            "projects": PROJECTS, "files": {}, "git": {}, **fields}


def mouse(t, code, column, row, release=False):
    """An SGR mouse report at 1-based terminal coordinates (0 press, 32 drag)."""
    t.key(f"\x1b[<{code};{column};{row}{'m' if release else 'M'}".encode())


def opened(columns, **fields):
    t = Terminal(30, columns)
    t.send(sidebar=sidebar(**fields), title="Fix token refresh", breadcrumb="~/w › main",
           composer_key="w/s1", generation=1)
    time.sleep(.4)
    t.redraw()
    return t


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_the_sidebar_draws_all_tabs_and_groups_worktrees_beside_the_conversation():
    t = opened(140)
    try:
        text = t.text()
        for label in ("Projects", "Sessions", "Files", "Git"):
            assert label in text, f"{label} tab missing:\n{text}"
        assert "nexus" in text and "main" in text and "feature-x" in text
        assert "Fix token refresh" in text, "the conversation is still there"
        # Both start on screen row 2, the sidebar first: its tabs come left of the session tab.
        assert text.index("Projects") < text.index("Fix token refresh")
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_tab_labels_wrap_in_a_narrow_sidebar_and_every_label_is_clickable():
    t = opened(140, width=24)
    try:
        text = t.text()
        assert all(label in text for label in ("Projects", "Sessions", "Files", "Git")), text
        # Width 24 leaves 22 columns: Projects and Sessions share row 1, Files and Git wrap to row 2.
        mouse(t, 0, 3, 3)
        mouse(t, 0, 3, 3, release=True)
        assert t.action() == {"type": "sidebar", "op": "tab", "tab": "Files"}
        mouse(t, 0, 12, 3)
        mouse(t, 0, 12, 3, release=True)
        assert t.action() == {"type": "sidebar", "op": "tab", "tab": "Git"}
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_ctrl_b_toggles_the_sidebar_and_a_click_on_a_tab_asks_for_it():
    t = opened(140)
    try:
        t.key(b"\x02")  # Ctrl+B
        assert t.action() == {"type": "sidebar", "op": "toggle"}
        mouse(t, 0, 25, 2)  # the Files label in the first tab row
        mouse(t, 0, 25, 2, release=True)
        assert t.action() == {"type": "sidebar", "op": "tab", "tab": "Files"}
        t.key(b"\x11")
        assert t.action()["type"] == "quit"
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_dragging_the_divider_sends_one_bounded_width_when_released():
    t = opened(140)
    try:
        mouse(t, 0, 34, 6)  # the divider is the last column of the 34-column sidebar
        mouse(t, 32, 50, 6)
        mouse(t, 32, 120, 6)  # beyond the maximum
        mouse(t, 0, 120, 6, release=True)
        assert t.action() == {"type": "sidebar", "op": "width", "width": 60}
        mouse(t, 0, 60, 6)
        mouse(t, 32, 2, 6)  # below the minimum
        mouse(t, 0, 2, 6, release=True)
        assert t.action() == {"type": "sidebar", "op": "width", "width": 24}
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_the_sidebar_hides_instead_of_squeezing_the_conversation():
    t = opened(70)
    try:
        text = t.text()
        assert "Projects" not in text and "Sessions" not in text, text
        assert "Fix token refresh" in text
    finally:
        t.close()
