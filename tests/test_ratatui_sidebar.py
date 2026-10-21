"""The native left sidebar (Projects, Sessions, Files, Git): projection, host-backed data, safety.

The terminal never reads the filesystem: every Files/Git datum below comes from a (faked) host
result, and every client action is validated against what the last snapshot offered.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.bridge import SessionDashboard
from nexus.ui.ratatui.preferences import Preferences
from nexus.ui_support import sidebar as view
from nexus.ui_support.manager import MANAGER_SESSION_ID

RUST = Path(__file__).resolve().parents[1] / "rust/tui/src/sidebar.rs"


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    shell = ShellActions(SimpleNamespace(session="s", client=SimpleNamespace()))
    shell.workspace = "/repo"
    shell.sidebar.enabled = True
    return shell


def _row(session, workspace, *, repo="", worktree="", status="idle", updated=1.0, age="1m", **extra):
    return {"id": session, "workspace": workspace, "repo": repo or workspace, "worktree": worktree,
            "status": status, "updated": updated, "age": age, "title": session, "archived": False, **extra}


def _fake_dashboard(shell, rows=()):
    async def open_dashboard():
        dashboard = SessionDashboard(shell)
        dashboard.visible = True
        dashboard.rows = list(rows)
        shell.workflows.dashboard = dashboard
    shell.workflows.open_dashboard = open_dashboard


# -- pure projection ----------------------------------------------------------------------


def test_constants_mirror_the_rust_client():
    source = RUST.read_text()
    number = lambda name: int(re.search(rf"pub const {name}: u16 = (\d+);", source).group(1))  # noqa: E731
    assert (number("MIN_WIDTH"), number("MAX_WIDTH"), number("DEFAULT_WIDTH")) == \
        (view.MIN_WIDTH, view.MAX_WIDTH, view.DEFAULT_WIDTH)
    tabs = re.search(r"pub const TABS: \[&str; 4\] = \[(.*?)\];", source).group(1)
    assert tuple(re.findall(r'"([^"]+)"', tabs)) == view.TABS == ("Projects", "Sessions", "Files", "Git")


def test_width_and_tab_are_bounded():
    assert view.clamp_width(5) == view.MIN_WIDTH
    assert view.clamp_width(500) == view.MAX_WIDTH
    assert view.clamp_width(40) == 40
    for bad in (None, "40", 4.5, True, [1]):
        assert view.clamp_width(bad) == view.DEFAULT_WIDTH
    assert view.normalize_tab("Git") == "Git"
    assert view.normalize_tab("Nope") == view.DEFAULT_TAB and view.normalize_tab(None) == view.DEFAULT_TAB


def test_projects_group_worktrees_under_their_main_repository():
    rows = [
        _row("a", "/repo", updated=5.0),
        _row("b", "/repo", updated=4.0, status="working"),
        _row("c", "/wt/feature", repo="/repo", worktree="feature", updated=9.0, age="now"),
        _row("d", "/other", updated=7.0, status="input"),
        _row("old", "/repo", updated=1.0, archived=True),
    ]
    listed = view.project_rows(rows, "/repo")
    assert [(row["kind"], row["label"]) for row in listed] == [
        ("project", "repo"), ("checkout", "main"), ("checkout", "feature"), ("project", "other"),
    ], "current project first, main checkout before worktrees, archived sessions ignored"
    repo, main, feature, other = listed
    assert repo["count"] == 3 and repo["expandable"] and repo["current"] and repo["rank"] == 1
    assert repo["session"] == "" and repo["workspace"] == "", "an expandable project opens nothing itself"
    assert (main["count"], main["session"], main["workspace"], main["current"]) == (2, "a", "/repo", True)
    assert (feature["workspace"], feature["session"], feature["current"]) == ("/wt/feature", "c", False)
    assert feature["parent"] == main["parent"] == "/repo"
    assert not other["expandable"] and (other["workspace"], other["session"], other["rank"]) == ("/other", "d", 0)
    assert view.openable(listed) == {("/repo", "a"), ("/wt/feature", "c"), ("/other", "d")}


def test_projects_are_bounded_and_control_safe():
    rows = [_row(f"s{i}", f"/p{i}", title="x") for i in range(1000)]
    assert len(view.project_rows(rows)) <= view.MAX_PROJECT_ROWS
    hostile = view.project_rows([_row("a", "/w/\x1b[31mred")])
    assert "\x1b" not in hostile[0]["label"] and "\x1b" not in hostile[0]["detail"]


def test_the_manager_session_is_hidden_by_identity():
    rows = [_row(MANAGER_SESSION_ID, "/repo"), _row("mgr-custom", "/repo", kind="manager"), _row("keep", "/repo")]
    assert [row["id"] for row in view.visible_session_rows(rows)] == ["keep"]
    assert [row["id"] for row in view.visible_session_rows(rows, "mgr-custom")] == ["keep"]
    assert {row["session"] for row in view.project_rows(rows) if row["session"]} == {"keep"}
    payload = view.sessions_payload({"kind": "sessions", "rows": rows, "manager": {"known": True, "summary": "x"}})
    assert [row["id"] for row in payload["rows"]] == ["keep"] and "manager" not in payload
    assert view.sessions_payload(None) is None


def test_files_payload_lists_a_parent_row_below_the_root_and_escapes():
    result = SimpleNamespace(path="src/pkg", truncated=True, entries=[
        {"name": "mod\x1b[2J.py", "path": "src/pkg/mod.py", "kind": "file", "size": 2048},
        {"name": "sub", "path": "src/pkg/sub", "kind": "dir", "size": None},
        {"name": "ln", "path": "src/pkg/ln", "kind": "link", "size": None},
        {"name": "weird", "path": "src/pkg/weird", "kind": "socket", "size": 1},
    ])
    payload = view.files_payload(result, "/work/repo")
    assert payload["label"] == "repo/src/pkg" and payload["truncated"] is True
    assert [(row["kind"], row["path"]) for row in payload["rows"]] == [
        ("up", "src"), ("file", "src/pkg/mod.py"), ("dir", "src/pkg/sub"), ("link", "src/pkg/ln"),
    ]
    assert "\x1b" not in payload["rows"][1]["name"] and payload["rows"][1]["size"] == "2K"
    assert view.navigable(payload) == {"src", "src/pkg/sub"}, "links and files are never navigable"
    root = view.files_payload(SimpleNamespace(path="", entries=[], truncated=False), "/work/repo")
    assert root["rows"] == [] and root["label"] == "repo/"


def test_git_payload_is_labelled_and_read_only():
    result = SimpleNamespace(available=True, message="", branch="main", upstream="origin/main", ahead=2, behind=1,
                             truncated=False, counts={"staged": 1, "unstaged": 1, "untracked": 1}, rows=[
        {"group": "unstaged", "code": "M", "label": "modified", "path": "a.py", "from": ""},
        {"group": "staged", "code": "A", "label": "added", "path": "b.py", "from": ""},
        {"group": "untracked", "code": "?", "label": "untracked", "path": "c\x1b.txt", "from": ""},
    ])
    payload = view.git_payload(result)
    assert payload["summary"] == "main → origin/main · ahead 2 · behind 1"
    assert [(row["kind"], row.get("label") or row["text"]) for row in payload["rows"]] == [
        ("heading", "Staged changes · 1"), ("entry", "added"),
        ("heading", "Changes · 1"), ("entry", "modified"),
        ("heading", "Untracked · 1"), ("entry", "untracked"),
    ]
    assert all("\x1b" not in row.get("path", "") for row in payload["rows"])
    clean = view.git_payload(SimpleNamespace(available=True, message="", branch="main", upstream="", ahead=0,
                                              behind=0, truncated=False, counts={}, rows=[]))
    assert clean["message"] == "Working tree clean" and clean["summary"] == "main"
    gone = view.git_payload(SimpleNamespace(available=False, message="This workspace is not a Git repository",
                                            rows=[], counts={}))
    assert not gone["available"] and "not a Git repository" in gone["message"]
    assert view.git_payload(None, "boom")["error"] == "boom"


def test_human_size():
    assert [view.human_size(n) for n in (None, 0, 1023, 1024, 1536, 10 * 1024, 5 * 1024 ** 2)] == \
        ["", "0B", "1023B", "1K", "1.5K", "10K", "5M"]


# -- preferences ---------------------------------------------------------------------------


def test_preferences_bound_the_width_and_reject_unknown_tabs(tmp_path):
    path = tmp_path / "tui.json"
    path.write_text(json.dumps({"sidebar_width": 999, "sidebar_tab": "Bogus", "sessions_sidebar": True}))
    prefs = Preferences(path)
    assert prefs.values["sidebar_width"] == view.MAX_WIDTH and prefs.values["sidebar_tab"] == view.DEFAULT_TAB
    assert prefs.values["sessions_sidebar"] is True, "an existing left sidebar preference is kept"
    prefs.set("sidebar_width", 1)
    assert prefs.values["sidebar_width"] == view.MIN_WIDTH
    prefs.set("sidebar_width", True)
    assert prefs.values["sidebar_width"] == view.MIN_WIDTH
    prefs.set("sidebar_tab", "Nope")
    prefs.set("sidebar_tab", "Git")
    assert prefs.values["sidebar_tab"] == "Git"
    assert Preferences(tmp_path / "missing.json").values["sessions_sidebar"] is False, "closed by default"


# -- controller ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_command_opens_the_sidebar_on_the_sessions_tab_with_focus(shell):
    _fake_dashboard(shell, [_row("a", "/repo")])
    shell.preferences.set("sidebar_tab", "Git")
    await shell.command("/sessions", ())
    sidebar = shell.sidebar
    assert sidebar.open and sidebar.tab == "Sessions" and sidebar.focus == 1
    assert shell.workflows.dashboard.visible, "the list keeps polling for the Sessions tab"
    assert shell.workflows.dashboard.poll_seconds == 2.0
    snapshot = sidebar.snapshot()
    assert snapshot["open"] and snapshot["tab"] == "Sessions" and snapshot["focus"] == 1
    assert [row["id"] for row in snapshot["sessions"]["rows"]] == ["a"]
    await shell.command("/sessions", ())
    assert sidebar.focus == 2, "asking again only moves focus"


@pytest.mark.asyncio
async def test_without_the_sidebar_the_legacy_sessions_page_still_opens(shell):
    shell.sidebar.enabled = False
    _fake_dashboard(shell)
    await shell.command("/sessions", ())
    assert shell.workflows.dashboard.visible and not shell.sidebar.open
    assert shell.sidebar.snapshot() == {"available": False, "open": False}


@pytest.mark.asyncio
async def test_toggle_closes_and_reopens_and_stops_the_list_feed(shell):
    _fake_dashboard(shell)
    await shell.sidebar.handle({"type": "sidebar", "op": "toggle"})
    assert shell.sidebar.open and shell.workflows.dashboard.visible
    await shell.sidebar.handle({"type": "sidebar", "op": "toggle"})
    assert not shell.sidebar.open and not shell.workflows.dashboard.visible
    assert shell.sidebar.snapshot() == {"available": True, "open": False}
    assert shell.preferences.values["sessions_sidebar"] is False


@pytest.mark.asyncio
async def test_the_list_survives_session_switches_and_panels_while_open(shell):
    _fake_dashboard(shell)
    await shell.sidebar.show("Sessions")
    shell.workflows.close_dashboard()  # what a session switch or an opened panel does
    assert shell.workflows.dashboard.visible
    shell.workflows.close_dashboard(force=True)
    assert not shell.workflows.dashboard.visible
    # Without the sidebar nothing changes: a panel still replaces the page.
    shell.sidebar.enabled = False
    await shell.workflows.open_dashboard()
    shell.workflows.close_dashboard()
    assert not shell.workflows.dashboard.visible


@pytest.mark.asyncio
async def test_legacy_dashboard_state_is_absent_where_the_sidebar_owns_the_list(shell):
    from nexus.ui.ratatui.prototype import _dashboard_state, _sidebar_state
    _fake_dashboard(shell, [_row("a", "/repo")])
    await shell.workflows.open_dashboard()
    shell.sidebar.enabled = False
    assert _dashboard_state(shell)["kind"] == "sessions"
    shell.sidebar.enabled = True
    assert _dashboard_state(shell) is None
    assert _sidebar_state(shell) == {"available": True, "open": False}
    assert _sidebar_state(None) == {"available": False, "open": False}
    assert _sidebar_state(SimpleNamespace()) == {"available": False, "open": False}


@pytest.mark.asyncio
async def test_tab_and_width_actions_are_validated_and_persisted(shell):
    _fake_dashboard(shell)
    await shell.sidebar.show("Sessions")
    shell.client.git_status = AsyncMock(return_value=SimpleNamespace(
        available=False, message="not a repo", rows=[], counts={}))
    await shell.sidebar.handle({"op": "tab", "tab": "Git"})
    assert shell.sidebar.tab == "Git" and shell.preferences.values["sidebar_tab"] == "Git"
    shell.client.git_status.assert_awaited_once()
    with pytest.raises(ValueError):
        await shell.sidebar.handle({"op": "tab", "tab": "Terminal"})
    assert shell.sidebar.tab == "Git"
    for sent, stored in ((10, view.MIN_WIDTH), (999, view.MAX_WIDTH), (41, 41)):
        await shell.sidebar.handle({"op": "width", "width": sent})
        assert shell.sidebar.width == stored == shell.preferences.values["sidebar_width"]
    for hostile in ("50", None, True, 3.5, [50]):
        await shell.sidebar.handle({"op": "width", "width": hostile})
        assert shell.sidebar.width == 41, hostile
    with pytest.raises(ValueError):
        await shell.sidebar.handle({"op": "format-disk"})


@pytest.mark.asyncio
async def test_the_client_does_nothing_while_the_sidebar_is_not_enabled(shell):
    shell.sidebar.enabled = False
    await shell.sidebar.handle({"op": "toggle"})
    assert shell.preferences.values["sessions_sidebar"] is False


@pytest.mark.asyncio
async def test_files_navigate_only_through_folders_the_host_listed(shell):
    _fake_dashboard(shell)
    listings = {
        "": SimpleNamespace(path="", truncated=False, entries=[
            {"name": "src", "path": "src", "kind": "dir", "size": None},
            {"name": "a.txt", "path": "a.txt", "kind": "file", "size": 3}]),
        "src": SimpleNamespace(path="src", truncated=False, entries=[
            {"name": "deep", "path": "src/deep", "kind": "dir", "size": None}]),
    }
    shell.client.list_directory = AsyncMock(side_effect=lambda path: listings[path])
    await shell.sidebar.show("Files")
    assert shell.sidebar.files["loaded"] and [r["name"] for r in shell.sidebar.files["rows"]] == ["src", "a.txt"]
    for refused in ("../etc", "/etc", "src/deep", "a.txt", "", None, 3):
        with pytest.raises(ValueError):
            await shell.sidebar.handle({"op": "dir", "path": refused})
    shell.client.list_directory.assert_awaited_once_with("")
    await shell.sidebar.handle({"op": "dir", "path": "src"})
    assert shell.sidebar.files["path"] == "src" and shell.sidebar.files["label"] == "repo/src"
    assert [(r["kind"], r["path"]) for r in shell.sidebar.files["rows"]] == [("up", ""), ("dir", "src/deep")]
    await shell.sidebar.handle({"op": "dir", "path": ""})  # the parent row
    assert shell.sidebar.files["path"] == ""


@pytest.mark.asyncio
async def test_a_slow_answer_cannot_replace_a_newer_one(shell):
    _fake_dashboard(shell)
    await shell.sidebar.show("Files")
    gate = asyncio.Event()
    a = SimpleNamespace(path="a", truncated=False, entries=[])
    b = SimpleNamespace(path="b", truncated=False, entries=[])

    async def listing(path):
        if path == "a":
            await gate.wait()
            return a
        return b

    shell.client.list_directory = AsyncMock(side_effect=listing)
    shell.sidebar._dirs = {"a", "b"}
    slow = asyncio.create_task(shell.sidebar.navigate("a"))
    await asyncio.sleep(0)
    await shell.sidebar.navigate("b")
    gate.set()
    await slow
    assert shell.sidebar.files["path"] == "b"


@pytest.mark.asyncio
async def test_switching_project_drops_the_old_projects_files_and_status(shell):
    _fake_dashboard(shell)
    shell.client.list_directory = AsyncMock(return_value=SimpleNamespace(path="", truncated=False, entries=[
        {"name": "old.txt", "path": "old.txt", "kind": "file", "size": 1}]))
    await shell.sidebar.show("Files")
    assert shell.sidebar.files["rows"]
    shell.workspace = "/elsewhere"
    snapshot = shell.sidebar.snapshot()
    assert snapshot["files"]["rows"] == [] and not snapshot["files"]["loaded"]
    await asyncio.sleep(0)  # the reload kicked off by the change
    await asyncio.sleep(0)
    assert shell.client.list_directory.await_count == 2


@pytest.mark.asyncio
async def test_a_failing_host_is_reported_in_the_tab_not_raised(shell):
    _fake_dashboard(shell)
    shell.client.git_status = AsyncMock(side_effect=RuntimeError("Git status timed out\x1b[31m"))
    await shell.sidebar.show("Git")
    git = shell.sidebar.snapshot()["git"]
    assert git["loaded"] and "timed out" in git["error"] and "\x1b" not in git["error"]


@pytest.mark.asyncio
async def test_projects_open_only_sessions_that_were_listed(shell):
    _fake_dashboard(shell)
    rows = [_row("a", "/repo", updated=2.0), _row("b", "/wt", repo="/repo", worktree="feat", updated=1.0)]

    from nexus.host.protocol import ProjectSession, ProjectSessionsListResult
    from nexus.session.manager import SessionSummary
    result = ProjectSessionsListResult(sessions=[
        ProjectSession(workspace=row["workspace"], project_id="p", repo=row["repo"] if row["worktree"] else "",
                       worktree=row["worktree"],
                       session=SessionSummary(id=row["id"], title=row["id"], last_user_at=row["updated"], message_count=1))
        for row in rows])
    shell.client.project_sessions = AsyncMock(return_value=result)
    shell.switch_project = AsyncMock()
    await shell.sidebar.show("Projects")
    shell.client.project_sessions.assert_awaited_with(limit=300)
    listed = shell.sidebar.snapshot()["projects"]
    assert listed["loaded"] and [r["label"] for r in listed["rows"]] == ["repo", "main", "feat"]
    await shell.sidebar.handle({"op": "open_session", "workspace": "/wt", "session": "b"})
    shell.switch_project.assert_awaited_once_with("/wt", "b")
    for pair in (("/wt", "a"), ("/etc", "b"), (None, None), ("/repo", "zzz")):
        with pytest.raises(ValueError):
            await shell.sidebar.handle({"op": "open_session", "workspace": pair[0], "session": pair[1]})
    assert shell.switch_project.await_count == 1


@pytest.mark.asyncio
async def test_the_sessions_tab_never_lists_the_manager(shell):
    _fake_dashboard(shell, [_row(MANAGER_SESSION_ID, "/repo"), _row("keep", "/repo")])
    await shell.sidebar.show("Sessions")
    rows = shell.sidebar.snapshot()["sessions"]["rows"]
    assert [row["id"] for row in rows] == ["keep"]
    assert "manager" not in shell.sidebar.snapshot()["sessions"]


@pytest.mark.asyncio
async def test_sessions_close_operation_closes_the_sidebar(shell):
    _fake_dashboard(shell)
    await shell.sidebar.show("Sessions")
    await shell.workflows.operate({"kind": "sessions_close"})
    assert not shell.sidebar.open
    assert not shell.workflows.dashboard.visible


def test_polled_state_changes_with_the_sidebar(shell):
    before = shell.sidebar.fingerprint()
    shell.sidebar.files = {**shell.sidebar.files, "path": "x"}
    assert shell.sidebar.fingerprint() != before
    shell.sidebar.enabled = False
    assert shell.sidebar.fingerprint() == ""


def test_the_snapshot_block_is_json_safe(shell):
    shell.preferences.set("sessions_sidebar", True)
    block = shell.sidebar.snapshot()
    assert json.loads(json.dumps(block)) == block
    assert set(block) >= {"available", "open", "tab", "width", "focus", "sessions", "projects", "files", "git"}
