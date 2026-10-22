"""The native right (details) sidebar: stored tab and width, the snapshot projection, the width action."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.preferences import Preferences
from nexus.ui.ratatui.prototype import project
from nexus.ui_support import details as view
from nexus.view import initial_state

RUST = Path(__file__).resolve().parents[1] / "rust/tui/src/details_sidebar.rs"


def _shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    controller = SimpleNamespace(session="s", view=initial_state("s"), agent_name="build", model="test",
                                 running=False, client=SimpleNamespace())
    return ShellActions(controller)


def test_constants_mirror_the_rust_client():
    source = RUST.read_text()
    number = lambda name: int(re.search(rf"pub const {name}: u16 = (\d+);", source).group(1))  # noqa: E731
    assert (number("MIN_WIDTH"), number("MAX_WIDTH"), number("DEFAULT_WIDTH")) == \
        (view.MIN_WIDTH, view.MAX_WIDTH, view.DEFAULT_WIDTH) == (24, 60, 40)
    tabs = re.search(r"pub const TABS: \[\(&str, &str\); 7\] = \[(.*?)\n\];", source, re.S).group(1)
    stored = tuple(re.findall(r'\("([^"]+)", "[^"]+"\)', tabs))
    labels = tuple(re.findall(r'\("[^"]+", "([^"]+)"\)', tabs))
    assert stored == ("Session", "Files", "Browser", "Git", "MCP", "Jobs", "Logs")
    assert len(set(stored)) == len(stored)
    assert labels == ("Session", "Changes", "Files", "Git", "MCP", "Jobs", "Logs")


def test_width_and_tab_are_bounded_and_changes_is_the_files_alias():
    assert view.clamp_width(5) == view.MIN_WIDTH and view.clamp_width(500) == view.MAX_WIDTH
    assert view.clamp_width(True) == view.DEFAULT_WIDTH == view.clamp_width("wide")
    assert view.normalize_tab("Changes") == "Files" == view.normalize_tab("Files")
    assert view.normalize_tab("Bogus") == view.normalize_tab(None) == view.DEFAULT_TAB


def test_preferences_bound_the_width_and_keep_the_files_tab(tmp_path):
    path = tmp_path / "tui.json"
    path.write_text(json.dumps({"details_width": 999, "details_tab": "Changes"}))
    prefs = Preferences(path)
    assert prefs.values["details_width"] == view.MAX_WIDTH
    assert prefs.values["details_tab"] == "Files"
    path.write_text(json.dumps({"details_width": 3, "details_tab": "Bogus"}))
    prefs = Preferences(path)
    assert (prefs.values["details_width"], prefs.values["details_tab"]) == (view.MIN_WIDTH, "Session")
    prefs.set("details_width", 1000)
    assert prefs.values["details_width"] == view.MAX_WIDTH
    prefs.set("details_width", True)
    assert prefs.values["details_width"] == view.MAX_WIDTH, "a boolean is not a width"
    prefs.set("details_width", 33)
    prefs.set("details_tab", "Changes")
    assert Preferences(path).values["details_tab"] == "Files" and prefs.values["details_width"] == 33
    prefs.set("details_tab", "Bogus")
    assert prefs.values["details_tab"] == "Files"
    assert Preferences(tmp_path / "missing.json").values["details_width"] == view.DEFAULT_WIDTH


def test_the_snapshot_projects_the_stored_width_and_tab(tmp_path, monkeypatch):
    state = _shell(tmp_path, monkeypatch)
    panel = project(state.controller, 1, shell=state)["details_panel"]
    assert (panel["tab"], panel["width"]) == ("Session", view.DEFAULT_WIDTH)
    state.preferences.set("details_width", 52)
    state.preferences.set("details_tab", "Files")
    panel = project(state.controller, 2, shell=state)["details_panel"]
    assert (panel["tab"], panel["width"]) == ("Files", 52)
    # Every tab's data stays in the one projection: the client shows one tab at a time.
    assert {"session", "files", "mcp", "logs_header", "files_summary"} <= set(panel)
