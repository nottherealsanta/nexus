"""The Projects/Sessions selector (an overlay, `sidebar.rs`) in a real PTY: tabs, tab clicks, Ctrl+B, divider drag and width bounds.

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
            "projects": {**PROJECTS, "browser_path": "/nexus",
                         "browser_rows": [{"name": "src", "kind": "dir", "path": "/nexus/src"},
                                          {"name": "readme.txt", "kind": "file", "path": "/nexus/readme.txt"}]}, "files": {}, "git": {}, **fields}


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
def test_the_selector_draws_path_and_directory_entries_over_the_conversation():
    t = opened(140, projects={**PROJECTS, "browser_open": True, "browser_path": "/nexus",
                              "browser_rows": [{"name": "src", "kind": "dir", "path": "/nexus/src"},
                                               {"name": "readme.txt", "kind": "file", "path": "/nexus/readme.txt"}]})
    try:
        text = t.text()
        for label in ("Projects", "Sessions"):
            assert label in text, f"{label} tab missing:\n{text}"
        # Files and Git live in the details drawer now, not the left sidebar.
        assert "Files" not in text and "Git" not in text, text
        assert "/nexus" in text and "▸ src" in text and "readme.txt" in text
        assert "Open /nexus as a project" in text
        assert "Fix token refresh" in text, "the conversation is still there"
        # The selector is a centered overlay beneath the top bar (not a docked column): the
        # session title in the top bar is painted before its tabs.
        assert text.index("Fix token refresh") < text.index("Projects")
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_both_selector_tabs_are_drawn_and_clickable_whatever_the_stored_width():
    # The selector is centered at a fixed size (rust/tui/src/sidebar.rs `selector_area`), so the
    # stored sidebar width no longer changes where the tab labels are. At 140 columns it spans
    # columns 26..115 and its tab row is screen row 5: Projects at 27..35, Sessions at 38..47.
    t = opened(140, width=24)
    try:
        text = t.text()
        assert all(label in text for label in ("Projects", "Sessions")), text
        mouse(t, 0, 40, 5)
        mouse(t, 0, 40, 5, release=True)
        assert t.action() == {"type": "sidebar", "op": "tab", "tab": "Sessions"}
        mouse(t, 0, 30, 5)
        mouse(t, 0, 30, 5, release=True)
        assert t.action() == {"type": "sidebar", "op": "tab", "tab": "Projects"}
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_ctrl_b_toggles_the_sidebar_and_a_click_on_a_tab_asks_for_it():
    t = opened(140)
    try:
        t.key(b"\x02")  # Ctrl+B
        assert t.action() == {"type": "sidebar", "op": "toggle"}
        mouse(t, 0, 40, 5)  # the Sessions label in the selector's tab row
        mouse(t, 0, 40, 5, release=True)
        assert t.action() == {"type": "sidebar", "op": "tab", "tab": "Sessions"}
        t.key(b"\x11")
        assert t.action()["type"] == "quit"
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_dragging_the_divider_sends_one_bounded_width_when_released():
    # The selector is a fixed-size centered overlay, so the width has no visible effect, but the
    # divider (its right border column, 115 at 140 columns) still reports one bounded width.
    t = opened(140)
    try:
        mouse(t, 0, 115, 10)
        mouse(t, 32, 50, 10)
        mouse(t, 32, 120, 10)  # beyond the maximum
        mouse(t, 0, 120, 10, release=True)
        assert t.action() == {"type": "sidebar", "op": "width", "width": 60}
        mouse(t, 0, 115, 10)
        mouse(t, 32, 2, 10)  # below the minimum
        mouse(t, 0, 2, 10, release=True)
        assert t.action() == {"type": "sidebar", "op": "width", "width": 24}
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_the_selector_still_fits_a_narrow_terminal_as_an_overlay():
    # It overlays the conversation instead of docking beside it, so a narrow terminal no longer hides it.
    t = opened(70)
    try:
        text = t.text()
        assert "Projects" in text and "Sessions" in text, text
        assert "Fix token refresh" in text
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_project_path_editing_and_bracketed_paste_use_sidebar_protocol():
    t = opened(140, projects={"loaded": True, "browser_open": True, "browser_path": "/repo", "browser_rows": []})
    try:
        # Focus the selector, then edit its path rather than the composer.
        mouse(t, 0, 30, 5)
        mouse(t, 0, 30, 5, release=True)
        assert t.action() == {"type": "sidebar", "op": "tab", "tab": "Projects"}
        t.key(b"/\x1b[H\x1b[3~\x1b[200~/tmp/\x1b[201~\r")
        assert t.action() == {"type": "sidebar", "op": "project_path", "path": "/tmp/repo"}
    finally:
        t.close()
