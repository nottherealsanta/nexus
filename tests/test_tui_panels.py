"""Side panels, Sessions dialog, and Settings for the Textual shell."""
from __future__ import annotations

import json
import time

import pytest
from test_ui_tui import FakeTransport, _client
from textual.widgets import Input, OptionList, RadioSet, Static

from nexus.host import protocol as p
from nexus.session.manager import SessionSummary
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support.tui_archived import ArchivedSessionsScreen
from nexus.ui_support.tui_panels import (
    DetailsSidebar,
    SessionRow,
    SessionSidebar,
    SessionsScreen,
    SettingsScreen,
    TopBar,
    TuiPreferences,
    mcp_markup,
    modified_files,
    relative_time,
    session_status,
)
from nexus.ui_support.tui_settings import SettingsConsole
from nexus.view import AgentView, ConversationView, ToolCallView, TurnView

_HUNK = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,3 @@\n def main():\n-    print('hi')\n+    print('hello')\n+    return 0\n"


class PanelTransport(FakeTransport):
    """Adds the session list states, archive/restore, and Doctor to the fake host."""

    def __init__(self) -> None:
        super().__init__()
        now = time.time()
        self.sessions = [
            SessionSummary(id="s", title="Current work", last_seq=4, last_activity=now),
            SessionSummary(id="busy", title="Refactor", state="running", last_activity=now),
            SessionSummary(id="asks", title="Needs approval", state="awaiting_permission", last_activity=now),
            SessionSummary(id="old", title="Yesterday", last_seq=2, last_activity=now - 86_400 * 2),
        ]
        self.archived: dict[str, SessionSummary] = {}
        self.trashed: dict[str, SessionSummary] = {}
        self.settings_body = "name: helper\n"

    async def request(self, command):
        if isinstance(command, p.SessionList):
            self.trace.append("SessionList")
            return p.SessionListResult(sessions=list(self.sessions), archived_count=len(self.archived))
        if isinstance(command, p.SessionArchive):
            self.trace.append(("SessionArchive", command.session))
            row = next(s for s in self.sessions if s.id == command.session)
            self.sessions.remove(row)
            self.archived[row.id] = row
            return p.SessionArchiveResult(session=row)
        if isinstance(command, p.SessionUnarchive):
            self.trace.append(("SessionUnarchive", command.session))
            row = self.archived.pop(command.session)
            self.sessions.append(row)
            return p.SessionUnarchiveResult(session=row)
        if isinstance(command, p.SessionListArchived):
            rows = [p.ArchivedSummary(
                **{field: getattr(row, field) for field in row.__struct_fields__},
                archived_at=time.time(), reason="user",
            ) for row in self.archived.values()]
            return p.SessionListArchivedResult(sessions=rows, has_more=False)
        if isinstance(command, p.SessionDelete):
            self.trace.append(("SessionDelete", command.session))
            row = self.archived.pop(command.session, None)
            if row is None:
                row = next(s for s in self.sessions if s.id == command.session)
                self.sessions.remove(row)
            self.trashed[row.id] = row
            return p.SessionDeleteResult(session=row.id, trash_id=row.id, delete_after=time.time() + 604800)
        if isinstance(command, p.SessionRestore):
            self.trace.append(("SessionRestore", command.trash_id))
            row = self.trashed.pop(command.trash_id)
            self.sessions.append(row)
            return p.SessionRestoreResult(session=row.id)
        if isinstance(command, p.SessionPreview):
            return p.SessionPreviewResult(text="user: archived prompt", truncated=False)
        if isinstance(command, p.SessionSearch):
            return p.SessionSearchResult(ids=list(self.archived))
        if isinstance(command, p.SettingsInventory):
            return p.SettingsInventoryResult(
                scope=command.scope,
                root_display="~/.nexus" if command.scope == "global" else "<project>/.nexus",
                categories=[p.SettingsCategory(key="tools", label="TOOLS", count=1)],
                items=[p.SettingsItem(category="tools", id="helper", label="helper", summary="test tool", rel_path="tools/helper.py")],
            )
        if isinstance(command, p.SettingsRead):
            return p.SettingsReadResult(body=self.settings_body, rel_path="tools/helper.py", builtin=False, sha256="old")
        if isinstance(command, p.SettingsWrite):
            self.trace.append(("SettingsWrite", command.scope, command.category, command.id, command.expected_sha256))
            self.settings_body = command.body
            return p.SettingsWriteResult(status="saved", sha256="new", loaded=["helper"])
        if isinstance(command, p.AgentDefaultSet):
            self.trace.append(("AgentDefaultSet", command.name, command.scope))
            self.default_agent = command.name
            return p.AgentDefaultSetResult(name=command.name, effective=command.name, scope=command.scope, rel_path="config.toml")
        if isinstance(command, p.AgentsList):
            result = await super().request(command)
            return p.AgentsListResult(agents=result.agents, default=getattr(self, "default_agent", "build"))
        if isinstance(command, p.SettingsDelete):
            self.trace.append(("SettingsDelete", command.scope, command.category, command.id))
            return p.SettingsDeleteResult(status="deleted", trash_id="trash-helper")
        if isinstance(command, p.Doctor):
            self.trace.append("Doctor")
            return p.DoctorResult(report={
                "workspace": "/work/demo",
                "providers": [{"name": "scripted"}],
                "mcp": {"servers": [
                    {"name": "filesystem", "health": "ready", "enabled": True, "connected": True, "tool_count": 4},
                    {"name": "github", "health": "failed", "enabled": True, "connected": False,
                     "last_error": "command not found"},
                ]},
            })
        return await super().request(command)


def _edit(call_id, path, *, diff=None, created=False, status="completed", name="edit"):
    return ToolCallView(
        call_id=call_id, name=name, status=status, input={"path": path},
        diff=diff, metrics={"created": True} if created else {},
    )


def test_modified_files_aggregates_successful_edits_across_agents():
    child = AgentView(id="child", body=ConversationView(turns=[TurnView(id="c", tools=[
        _edit("c1", "docs/guide.md", diff={"path": "docs/guide.md", "added_lines": 3, "removed_lines": 0, "hunk": "+x"}),
    ])]))
    view = ConversationView(
        turns=[TurnView(id="t", tools=[
            _edit("1", "src/app.py", diff={"path": "src/app.py", "added_lines": 2, "removed_lines": 1, "hunk": _HUNK}),
            _edit("2", "src/app.py", diff={"path": "src/app.py", "added_lines": 1, "removed_lines": 1, "hunk": "+y"}),
            _edit("3", "src/new.py", created=True, name="write"),
            _edit("4", "src/broken.py", status="failed"),
            ToolCallView(call_id="5", name="read", status="completed", input={"path": "README.md"}),
        ])],
        agents={"child": child},
    )
    files = {change.path: change for change in modified_files(view)}
    assert set(files) == {"src/app.py", "src/new.py", "docs/guide.md"}
    assert (files["src/app.py"].added, files["src/app.py"].removed) == (3, 2)
    assert len(files["src/app.py"].hunks) == 2
    assert files["src/new.py"].created


def test_session_status_relative_time_and_preferences(tmp_path):
    seen = {"s": 4, "done": 2}
    assert session_status(SessionSummary(id="x", state="running"), seen, "s") == "working"
    assert session_status(SessionSummary(id="x", state="awaiting_input"), seen, "s") == "input"
    assert session_status(SessionSummary(id="done", last_seq=5), seen, "s") == "done"
    assert session_status(SessionSummary(id="s", last_seq=9), seen, "s") == "idle"
    assert session_status(SessionSummary(id="new", last_seq=9), seen, "s") == "idle"
    assert relative_time(100.0, now=110.0) == "just now"
    assert relative_time(100.0, now=100.0 + 3 * 3600) == "3h ago"

    path = tmp_path / "tui.json"
    prefs = TuiPreferences(path)
    prefs.set("theme", "nexus-light")
    prefs.set("details_sidebar", "yes")  # wrong type is ignored
    assert json.loads(path.read_text())["theme"] == "nexus-light"
    assert TuiPreferences(path)["details_sidebar"] is True
    assert TuiPreferences(None)["theme"] == "nexus-dark"


def test_mcp_markup_covers_disabled_empty_and_failed_servers():
    assert "disabled" in mcp_markup({})
    assert "No servers" in mcp_markup({"mcp": {"servers": []}})
    text = mcp_markup({"mcp": {"servers": [{"name": "gh", "health": "failed", "connected": False,
                                            "last_error": "boom"}]}})
    assert "gh" in text and "boom" in text
    assert "Unavailable" in mcp_markup(None, error="refused")


@pytest.mark.asyncio
async def test_sidebars_follow_terminal_width_and_show_session_status():
    transport = PanelTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause(0.3)
        assert app.query_one(SessionSidebar).display and app.query_one(DetailsSidebar).display
        rows = {row.session_id: row for row in app.query(SessionRow)}
        assert rows["busy"].has_class("-working") and rows["asks"].has_class("-input")
        assert rows["s"].has_class("-active")
        app.query_one(SessionSidebar).set_current_running(True)
        assert rows["s"].has_class("-working")
        assert app.query_one(SessionSidebar).status_counts()["working"] == 2
        app.query_one(SessionSidebar).set_current_running(False)
        assert not rows["s"].has_class("-working")
        app.controller.view = ConversationView(phase="running")
        await app._sync_timeline()
        assert rows["s"].has_class("-working")
        app.controller.view = ConversationView()
        await app._sync_timeline()
        assert not rows["s"].has_class("-working")
        details = app.query_one(DetailsSidebar)
        mcp = str(details.query_one("#details-mcp").render())
        assert "filesystem" in mcp and "command not found" in mcp

        app.controller.view = ConversationView(turns=[TurnView(id="t", tools=[
            _edit("1", "src/app.py", diff={"path": "src/app.py", "added_lines": 2, "removed_lines": 1, "hunk": _HUNK}),
        ])])
        app._sync_details()
        await pilot.pause()
        row = str(details.query_one(".file-row").render())
        assert "src/" in row and "app.py" in row and "+2" in row
        await pilot.press("ctrl+l")
        assert not details.display
        assert app.prefs["details_sidebar"] is False

        await pilot.resize_terminal(100, 40)
        await pilot.pause()
        assert not app.query_one(SessionSidebar).display


@pytest.mark.asyncio
async def test_sidebar_groups_sessions_by_day_like_the_sessions_dialog():
    transport = PanelTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        assert await app._archive_session("old")
        await app._switch_session("s")
        await app._poll_sessions()
        await pilot.pause(0.3)
        container = app.query_one("#session-list")
        order = [child.render().plain if child.has_class("session-day") else child.session_id
                 for child in container.children]
        assert order == ["Today", "s", "busy", "asks", "Archived", "old"]
        rows = {row.session_id: row for row in container.query(SessionRow)}
        assert rows["asks"].query_one(".session-sub").render().plain == "Needs input"
        assert rows["s"].has_class("-active") and rows["old"].status == "archived"
        await app._poll_sessions()
        await pilot.pause(0.3)
        assert len(container.query(".session-day")) == 2


@pytest.mark.asyncio
async def test_top_bar_shows_session_and_toggles_panels():
    transport = PanelTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause(0.3)
        bar = app.query_one(TopBar)
        assert bar.query_one("#topbar-crumb").render().plain == "Current work"
        assert "nexus" not in " ".join(str(child.render()) for child in bar.query(Static))
        assert bar.query_one("#topbar-status").render().plain == "Idle"
        toggle = bar.query_one("#topbar-sidebar-toggle")
        assert toggle.has_class("-on")
        await pilot.click("#topbar-sidebar-toggle")
        await pilot.pause()
        assert not app.query_one(SessionSidebar).display and app.prefs["sessions_sidebar"] is False
        assert not toggle.has_class("-on")
        await pilot.click("#topbar-sidebar-toggle")
        await pilot.pause()
        assert app.query_one(SessionSidebar).display and app.prefs["sessions_sidebar"] is True
        await pilot.click("#topbar-details-toggle")
        await pilot.pause()
        assert not app.query_one(DetailsSidebar).display and app.prefs["details_sidebar"] is False
        app.controller.view = ConversationView(phase="running")
        await app._sync_timeline()
        assert "Working" in bar.query_one("#topbar-status").render().plain and bar.has_class("-working")
        app.controller.view = ConversationView()
        await app._sync_timeline()
        await app._switch_session("old")
        await pilot.pause()
        assert bar.query_one("#topbar-crumb").render().plain == "Yesterday"
        await pilot.click("#topbar-new")
        await pilot.pause(0.3)
        assert app.controller.session not in {"s", "old"}
        assert bar.query_one("#topbar-crumb").render().plain == "New Session"


@pytest.mark.asyncio
async def test_narrow_toggle_opens_sessions_overlay_without_changing_preference():
    transport = PanelTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.3)
        sidebar = app.query_one(SessionSidebar)
        assert not sidebar.display
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert sidebar.display and sidebar.has_class("-overlay")
        assert app.prefs["sessions_sidebar"] is True
        assert app.focused is app.query_one("#session-filter")
        await pilot.press("escape")
        await pilot.pause()
        assert not sidebar.display and app.focused is app.query_one("#chat-editor")
        await pilot.click("#topbar-sidebar-toggle")
        await pilot.pause(0.3)
        assert sidebar.display
        busy = next(row for row in sidebar.query(SessionRow) if row.session_id == "busy")
        busy.focus()
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.controller.session == "busy" and not sidebar.display
        await pilot.resize_terminal(200, 50)
        await pilot.pause()
        assert sidebar.display and not sidebar.has_class("-overlay")


@pytest.mark.asyncio
async def test_sessions_dialog_groups_days_archives_and_restores():
    transport = PanelTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+o")
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        options = screen.query_one("#sessions-options", OptionList)
        prompts = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert prompts[0] == "Today" and any("Needs input" in text for text in prompts)
        assert options.highlighted_option.id == "s"

        await pilot.press("down")
        archived = options.highlighted_option.id
        await pilot.press("ctrl+d")
        await pilot.pause(0.2)
        assert ("SessionArchive", archived) in transport.trace
        assert archived not in [options.get_option_at_index(i).id for i in range(options.option_count)]

        await pilot.press("ctrl+z")
        await pilot.pause(0.2)
        assert ("SessionUnarchive", archived) in transport.trace

        for key in "old":
            await pilot.press(key)
        await pilot.pause()
        assert options.highlighted_option.id == "old"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.controller.session == "old"


@pytest.mark.asyncio
async def test_archive_browser_previews_and_resumes_without_trash():
    transport = PanelTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        assert await app._archive_session("old")
        await pilot.pause()
        assert app.query_one("#sidebar-archived").render().plain == "Archived · 1"
        await app._dispatch_chat_command("/archived")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ArchivedSessionsScreen)
        assert "Yesterday" in str(screen.query_one("#archived-list", OptionList).get_option_at_index(0).prompt)
        screen.query_one("#archived-list", OptionList).focus()
        await pilot.press("p")
        await pilot.pause()
        assert "archived prompt" in screen.query_one("#archived-detail").render().plain
        await pilot.press("enter")
        await pilot.pause()
        assert ("SessionUnarchive", "old") in transport.trace
        assert app.controller.session == "old"


@pytest.mark.asyncio
async def test_sidebar_shows_archived_sessions_and_deletes_to_restorable_trash():
    transport = PanelTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        assert await app._archive_session("old")
        await pilot.pause()
        sidebar = app.query_one(SessionSidebar)
        rows = {row.session_id: row for row in sidebar.query(SessionRow)}
        assert set(rows) == {"s", "busy", "asks", "old"}
        assert rows["old"].status == "archived"
        assert rows["busy"].status == "working"
        glyph = rows["busy"].query_one(".session-glyph").render().plain
        assert glyph in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        assert rows["old"].query_one(".session-delete").visible
        await pilot.click(rows["old"].query_one(".session-delete"))
        await pilot.pause()
        assert ("SessionDelete", "old") in transport.trace
        assert "old" not in {row.session_id for row in sidebar.query(SessionRow)}
        await app.action_restore_archived()
        assert ("SessionRestore", "old") in transport.trace


@pytest.mark.asyncio
async def test_settings_console_edits_host_inventory_with_sha():
    transport = PanelTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        await app._dispatch_chat_command("/settings")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsConsole)
        # Full-screen page with a left sidebar of areas.
        assert screen.query_one("#settings-console").region.width == 160
        sections = screen.query_one("#settings-sections", OptionList)
        sections.highlighted = screen._section_index("tools")
        await pilot.pause()
        assert screen.category == "tools"
        await screen._open_item(0)
        await pilot.pause()
        assert not screen.query_one("#agent-form").display
        editor = screen.query_one("#settings-file-editor")
        editor.text = "name: improved\n"
        sections.highlighted = screen._section_index("skills")
        await pilot.pause()
        assert "Discard unsaved changes" in app.screen.query_one("#settings-confirm-text").render().plain
        await pilot.click("#settings-confirm-no")
        await pilot.pause()
        assert screen.category == "tools"
        await screen.action_save()
        await pilot.pause()
        assert transport.settings_body == "name: improved\n"
        # Edits default to the user's ~/.nexus scope.
        assert ("SettingsWrite", "global", "tools", "helper", "old") in transport.trace
        await pilot.click("#settings-scope-project")
        await pilot.pause()
        assert screen.scope == "project"
        assert screen.query_one("#settings-scope-path").render().plain == "<project>/.nexus"


@pytest.mark.asyncio
async def test_settings_agent_form_edits_frontmatter_and_saves_override():
    transport = PanelTransport()
    transport.settings_body = "---\nname: task\ndescription: worker\ncontexts: [subagent]\n---\nBody.\n"
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        app.action_open_settings("agents")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsConsole) and screen.category == "agents"
        screen._visible_items = [p.SettingsItem(category="agents", id="task", label="task", summary="", builtin=True)]
        await screen._open_item(0)
        await pilot.pause()
        assert screen.query_one("#agent-form").display
        screen.query_one("#agent-model", Input).value = "anthropic/claude-sonnet-5"
        screen.query_one("#agent-reasoning_effort", Input).value = "high"
        screen.query_one("#agent-fallback", Input).value = "openai/gpt-5, low"
        await pilot.pause()
        await screen.action_save()
        await pilot.pause()
        body = transport.settings_body
        assert "model: anthropic/claude-sonnet-5\n" in body
        assert "reasoning_effort: high\n" in body
        assert "fallback: [openai/gpt-5, low]\n" in body
        assert body.endswith("---\nBody.\n")
        assert ("SettingsWrite", "global", "agents", "task", "old") in transport.trace


@pytest.mark.asyncio
async def test_settings_agents_sets_the_default_agent_for_new_sessions():
    transport = PanelTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        app.action_open_settings("agents")
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#settings-default-agent-row").display
        buttons = {button.name: button for button in screen.query(".settings-default-agent")}
        assert next(iter(buttons)) == "build" and "subonly" not in buttons
        assert buttons["build"].has_class("-current")
        await screen._choose_default_agent("zebra")
        await pilot.pause()
        assert ("AgentDefaultSet", "zebra", "global") in transport.trace
        current = [button.name for button in screen.query(".settings-default-agent.-current")]
        assert current == ["zebra"]
        assert "saved to ~/.nexus/config.toml" in screen.query_one("#settings-default-agent-note").render().plain
        await screen._change_category("tools")
        await pilot.pause()
        assert not screen.query_one("#settings-default-agent-row").display


@pytest.mark.asyncio
async def test_settings_switch_theme_and_layout_and_persist(tmp_path):
    path = tmp_path / "tui.json"
    app = NexusTextualApp(_client(PanelTransport()), session="s", preferences_path=path)
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        radios = screen.query_one("#settings-theme", RadioSet)
        light = next(button for button in radios.query("RadioButton") if button.name == "nexus-light")
        light.value = True
        await pilot.pause()
        assert app.theme == "nexus-light"
        screen.query_one("#pref-sessions_sidebar").value = False
        await pilot.pause()
        assert not app.query_one(SessionSidebar).display
        await pilot.press("escape")
    stored = json.loads(path.read_text())
    assert stored["theme"] == "nexus-light" and stored["sessions_sidebar"] is False
