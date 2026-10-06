"""Settings → Agents, Tools, MCP servers and Skills: file-backed pages, scope only where it applies."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.host import protocol as p
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui_support import settings_page as sp


def _item(category, id_, label=None, **kw):
    return p.SettingsItem(category=category, id=id_, label=label or id_, summary=kw.pop("summary", ""), **kw)


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    shell = ShellActions(SimpleNamespace(session="s", client=SimpleNamespace()))
    items = {
        "global": [_item("agents", "task", builtin=True), _item("agents", "build", builtin=True), _item("agents", "reviewer", summary="Reviews diffs"),
                   _item("skills", "demo", "demo skill"), _item("tools", "stats", summary="Counts things"), _item("mcp", "mcp.json")],
        "project": [_item("skills", "local", "local skill"), _item("mcp", "mcp.json")],
    }
    async def inventory(scope):
        return p.SettingsInventoryResult(scope=scope, root_display="~/.nexus" if scope == "global" else "<workspace>/.agents", items=items[scope])
    shell.client.settings_inventory = inventory
    shell.client.default_agent = AsyncMock(return_value="build")
    shell.client.list_agents = AsyncMock(return_value=[{"name": "build", "contexts": ["root"]}, {"name": "orchestrator", "contexts": ["root"]}])
    shell.client.set_default_agent = AsyncMock()
    shell.client.inspect_context = AsyncMock(return_value=SimpleNamespace(mcp_servers=[
        {"name": "github", "scope": "global", "config_enabled": True, "config_tool_loading": "search", "tool_count": 31, "status": "running", "schema_tokens": 4100},
        {"name": "postgres", "scope": "project", "config_enabled": False, "config_tool_loading": "all", "tool_count": 0, "status": "failed: exit 1", "schema_tokens": 0}]))
    shell.client.settings_read = AsyncMock(return_value=SimpleNamespace(sha256="h", body="{}", rel_path="mcp.json", builtin=False))
    shell.client.settings_mcp_enabled_set = AsyncMock(return_value=SimpleNamespace(status="written"))
    shell.client.settings_mcp_loading_set = AsyncMock(return_value=SimpleNamespace(status="written"))
    return shell


def _blocks(shell):
    def walk(blocks):
        for b in blocks:
            yield b
            if b.get("t") == "section":
                yield from walk(b["blocks"])
    return list(walk(shell.workflows.settings_page["blocks"]))


def _labels(shell):
    return [b["label"] for b in _blocks(shell) if b.get("t") == "row"]


@pytest.mark.asyncio
async def test_scope_control_only_where_a_page_can_differ_per_project(shell):
    for area, scoped in [("agents", False), ("tools", True), ("skills", True), ("mcp", True)]:
        await shell.workflows.settings_area(area)
        page = shell.workflows.settings_page
        assert (page["scope"] is not None) == scoped, area
        assert page["intro"] and page["footer"]
        if scoped:
            assert page["scope"]["options"] == ["Global", "Project"] and page["scope"]["value"] == 0


@pytest.mark.asyncio
async def test_switching_scope_lists_that_scopes_files_and_footer(shell):
    await shell.workflows.settings_area("skills")
    assert _labels(shell) == ["demo skill"] and shell.workflows.settings_page["footer"] == "Saved in ~/.nexus"
    await shell.workflows.operate({**shell.workflows.settings_page["scope"]["operation"], "value": 1})
    assert _labels(shell) == ["local skill"] and shell.workflows.settings_page["footer"] == "Saved in <workspace>/.agents"
    assert shell.panel_title == "Settings · Skills", "changing scope never leaves the page"


@pytest.mark.asyncio
async def test_agents_list_build_first_then_builtins_then_custom_with_a_default_select(shell):
    await shell.workflows.settings_area("agents")
    assert _labels(shell) == ["New sessions start with", "build · built-in", "task · built-in", "reviewer"]
    select = next(b for b in _blocks(shell) if b.get("t") == "row" and b["id"] == "default-agent")["control"]
    assert select["value"] == "build" and [v for _, v in select["options"]] == ["build", "orchestrator"]
    await shell.workflows.operate({**select["operation"], "value": "orchestrator"})
    shell.client.set_default_agent.assert_awaited_once_with("orchestrator", "global")
    assert shell.toasts[-1]["level"] == "success"


@pytest.mark.asyncio
async def test_a_file_row_edits_through_the_host_editor_and_new_file_and_reset_are_offered(shell):
    await shell.workflows.settings_area("tools")
    edit = next(b for b in _blocks(shell) if b.get("t") == "row")["control"]["operation"]
    assert edit == {"kind": "settings_read", "scope": "global", "category": "tools", "id": "stats"}
    buttons = {i["label"]: i["operation"] for b in _blocks(shell) if b.get("t") == "buttons" for i in b["items"]}
    assert buttons["New file"] == {"kind": "settings_new", "scope": "global", "category": "tools"}
    assert buttons["Reset category…"]["kind"] == "confirm" and buttons["Reset category…"]["lines"] == ["stats"]
    assert next(b for b in _blocks(shell) if b.get("t") == "row")["description"] == "Counts things"


@pytest.mark.asyncio
async def test_mcp_shows_each_servers_state_for_the_scope_with_switch_and_loading(shell):
    await shell.workflows.settings_area("mcp")
    sections = [b for b in _blocks(shell) if b.get("t") == "section"]
    assert [s["title"] for s in sections] == ["github"], "only this scope's servers"
    assert sections[0]["summary"] == "running · 31 tools" and sections[0]["tone"] == "success"
    rows = {b["id"]: b for b in sections[0]["blocks"] if b.get("t") == "row"}
    assert rows["github:on"]["control"]["on"] is True
    loading = rows["github:load"]["control"]
    assert loading["options"] == ["Search", "All"] and loading["values"] == ["search", "all"] and loading["active"] == 0
    assert "~4100 tokens" in rows["github:load"]["description"]
    await shell.workflows.operate({**rows["github:on"]["control"]["operation"], "value": False})
    shell.client.settings_mcp_enabled_set.assert_awaited_once_with("global", "github", False, "h")
    await shell.workflows.operate({**loading["operation"], "value": "all"})
    shell.client.settings_mcp_loading_set.assert_awaited_once_with("global", "github", "all", "h")
    # The other scope shows its own server, off and failed, with the reason visible.
    await shell.workflows.operate({**shell.workflows.settings_page["scope"]["operation"], "value": 1})
    section = next(b for b in _blocks(shell) if b.get("t") == "section")
    assert section["title"] == "postgres" and section["tone"] == "error" and section["summary"] == "failed: exit 1 · 0 tools · off"


@pytest.mark.asyncio
async def test_mcp_edit_conflicts_are_reported_not_overwritten(shell):
    shell.client.settings_mcp_enabled_set = AsyncMock(return_value=SimpleNamespace(status="conflict"))
    await shell.workflows.settings_area("mcp")
    await shell.workflows.operate(sp.op("mcp", "enabled", scope="global", name="github", value=False))
    assert shell.toasts[-1]["level"] == "warning" and "changed on disk" in shell.toasts[-1]["title"] + shell.toasts[-1]["body"]
    with pytest.raises(ValueError, match="Unknown tool loading mode"):
        await shell.workflows.operate(sp.op("mcp", "loading", scope="global", name="github", value="everything"))


@pytest.mark.asyncio
async def test_mcp_page_still_lists_files_when_live_server_state_is_unavailable(shell):
    shell.client.inspect_context = AsyncMock(side_effect=RuntimeError("no session"))
    await shell.workflows.settings_area("mcp")
    assert "mcp.json" in _labels(shell) and not any(b.get("t") == "section" for b in _blocks(shell))
    assert shell.toasts[-1]["level"] == "warning"
