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
    for area, scoped in [("agents", False), ("tools", True), ("skills", True), ("mcp", False)]:
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
async def test_mcp_shows_global_then_project_servers_with_state_switch_and_loading(shell):
    shell.client.inspect_context = AsyncMock(return_value=SimpleNamespace(mcp_servers=[
        {"name": "github", "scope": "global", "config_enabled": True, "config_tool_loading": "search", "tool_count": 31, "status": "connected",
         "schema_tokens": 4100, "transport": "stdio", "command_label": "docker (14 args)", "source_path": "~/.nexus/mcp.json",
         "resource_count": 2, "include_tools": ["a"], "ignored_keys": ["autoApprove"]},
        {"name": "postgres", "scope": "project", "config_enabled": False, "config_tool_loading": "all", "tool_count": 0, "status": "failed",
         "error": "exit 1", "transport": "http", "url": "https://x/mcp", "source_path": ".agents/mcp.json"},
        {"name": "bad", "scope": "project", "invalid": True, "status": "invalid", "error": "command must be a string", "source_path": ".agents/mcp.json"},
        {"name": "/w/.agents/mcp.json", "scope": "project", "file_error": True, "status": "warning", "error": "ignored top-level keys: x"}]))
    await shell.workflows.settings_area("mcp")
    assert shell.workflows.settings_page["scope"] is None
    top = shell.workflows.settings_page["blocks"]
    headings = [b["text"] for b in top if b.get("t") == "heading"]
    assert headings[0] == "GLOBAL · ~/.nexus/mcp.json" and "PROJECT · <workspace>/.agents/mcp.json" in headings
    assert [b["title"] for b in top if b.get("t") == "section"] == ["github", "postgres", "bad"]
    assert any(b.get("t") == "callout" and b["level"] == "warning" for b in top)
    sections = {b["title"]: b for b in top if b.get("t") == "section"}
    assert sections["github"]["summary"] == "connected · stdio · 31 tools" and sections["github"]["tone"] == "success"
    rows = {b["id"].split(":", 2)[2]: b for b in sections["github"]["blocks"] if b.get("t") == "row"}
    assert rows["target"]["control"]["value"] == "docker (14 args)" and rows["filters"]["control"]["value"] == "include a"
    assert rows["ignored"]["control"]["value"] == "autoApprove" and "does not act" in rows["ignored"]["description"]
    assert rows["tools"]["control"]["value"] == "31 tools · 2 resources"
    assert rows["on"]["control"]["on"] is True
    loading = rows["load"]["control"]
    assert loading["options"] == ["Search", "All"] and loading["values"] == ["search", "all"] and loading["active"] == 0
    assert "~4100 tokens" in rows["load"]["description"]
    await shell.workflows.operate({**rows["on"]["control"]["operation"], "value": False})
    shell.client.settings_mcp_enabled_set.assert_awaited_once_with("global", "github", False, "h")
    await shell.workflows.operate({**loading["operation"], "value": "all"})
    shell.client.settings_mcp_loading_set.assert_awaited_once_with("global", "github", "all", "h")
    pg = sections["postgres"]
    assert pg["tone"] == "error" and pg["summary"] == "failed · http · 0 tools · off"
    assert next(b for b in pg["blocks"] if b["id"].endswith(":status"))["control"]["value"] == "failed: exit 1"
    bad = sections["bad"]
    assert bad["tone"] == "error" and not any(b["id"].endswith((":on", ":load")) for b in bad["blocks"])
    new_files = [i["operation"] for b in top if b.get("t") == "buttons" for i in b["items"] if i["label"] == "New file"]
    assert [o["scope"] for o in new_files] == ["global", "project"]


@pytest.mark.asyncio
async def test_mcp_empty_scope_says_where_a_new_file_goes(shell):
    shell.client.inspect_context = AsyncMock(return_value=SimpleNamespace(mcp_servers=[]))
    await shell.workflows.settings_area("mcp")
    notes = [b["text"] for b in shell.workflows.settings_page["blocks"] if b.get("t") == "note"]
    assert "No servers in <workspace>/.agents/mcp.json. New file creates one." in notes


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
    assert "mcp.json" in _labels(shell) and not any(b.get("t") == "section" and b["id"].startswith("srv-") for b in _blocks(shell))
    assert shell.toasts[-1]["level"] == "warning"


@pytest.mark.asyncio
async def test_every_operation_the_client_can_send_is_accepted_by_the_bridge(shell):
    from settings_page_ops import assert_every_client_op_is_accepted
    for area in ("agents", "tools", "skills", "mcp"):
        await shell.workflows.settings_area(area)
        assert_every_client_op_is_accepted(shell)
