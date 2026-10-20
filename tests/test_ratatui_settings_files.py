"""Settings → Agents, Tools, MCP servers and Skills: file-backed pages, scope only where it applies."""
import json
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
    shell.client.settings_mcp_add_preview = AsyncMock(side_effect=_host_preview)
    return shell


async def _host_preview(scope, name, entry, expected=None):
    """What the host answers to ``SettingsMcpAdd(dry_run=True)``; the UI itself never loads MCP code."""
    from nexus.errors import ConfigError
    from nexus.host_support import settings_inventory as si
    try:
        checked, secrets = si.validate_mcp_add(name, entry)
        lines, transport = si.mcp_add_preview(scope, "~/.nexus" if scope == "global" else "<project>/.agents", name, checked, secrets)
    except ConfigError as exc:
        raise ValueError(str(exc)) from None  # the client raises the host's redacted error
    return p.SettingsWriteResult(status="preview", lines=lines, transport=transport)


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
    assert headings[0] == "Global · ~/.nexus/mcp.json" and "Project · <workspace>/.agents/mcp.json" in headings
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


def _buttons(shell):
    return {i["label"]: i["operation"] for b in _blocks(shell) if b.get("t") == "buttons" for i in b["items"]}


@pytest.mark.asyncio
async def test_add_from_npx_skills_button_is_on_skills_only_and_carries_the_scope(shell):
    await shell.workflows.settings_area("skills")
    assert _buttons(shell)["Add from npx skills…"] == {"kind": "skills_install_open", "scope": "global"}
    await shell.workflows.operate({**shell.workflows.settings_page["scope"]["operation"], "value": 1})
    assert _buttons(shell)["Add from npx skills…"] == {"kind": "skills_install_open", "scope": "project"}
    for area in ("tools", "agents", "mcp"):
        await shell.workflows.settings_area(area)
        assert "Add from npx skills…" not in _buttons(shell)


@pytest.mark.asyncio
async def test_install_needs_explicit_third_party_code_confirmation_then_runs_in_the_background(shell):
    import asyncio
    gate = asyncio.Event()

    async def install(scope, source, skills, overwrite):
        await gate.wait()
        return p.SkillsInstallResult(status="installed", installed=["alpha"])

    shell.client.skills_install = AsyncMock(side_effect=install)
    wf = shell.workflows
    await wf.settings_area("skills")
    await wf.operate({"kind": "skills_install_open", "scope": "global"})
    assert wf.form_target["kind"] == "skills_install_form"
    await wf.save(wf.form["id"], "owner/repo alpha  beta", 1)
    shell.client.skills_install.assert_not_called()
    assert any("third-party code" in line for line in shell.panel_lines)
    run = next(item["operation"] for item in shell.items if item["label"].startswith("Install"))
    assert run == {"kind": "skills_install_run", "scope": "global", "source": "owner/repo", "skills": ["alpha", "beta"], "overwrite": False}

    await wf.operate(run)  # returns at once: the install is a background task
    assert shell.panel_title == wf.page_title and not wf.install_task.done()
    await wf.operate(run)
    assert shell.toasts[-1]["level"] == "warning" and "already running" in shell.toasts[-1]["title"]
    gate.set()
    await wf.install_task
    shell.client.skills_install.assert_awaited_once_with("global", "owner/repo", ["alpha", "beta"], False)
    assert shell.toasts[-1]["level"] == "success" and "alpha" in shell.toasts[-1]["title"]


@pytest.mark.asyncio
async def test_install_conflicts_offer_overwrite_and_a_stale_result_never_navigates(shell):
    wf = shell.workflows
    results = [p.SkillsInstallResult(status="conflict", conflicts=["demo"]), p.SkillsInstallResult(status="installed", installed=["demo"])]
    shell.client.skills_install = AsyncMock(side_effect=results)
    await wf.settings_area("skills")
    op = {"kind": "skills_install_run", "scope": "global", "source": "owner/repo", "skills": [], "overwrite": False}
    await wf.operate(op)
    await wf.install_task
    assert shell.panel_title.startswith("Overwrite 1 existing")
    again = next(item["operation"] for item in shell.items if item["label"].startswith("Overwrite"))
    assert again["overwrite"] is True and again["skills"] == ["demo"]
    # A session switch while the install runs: the result is a toast only.
    shell.panel_title = ""
    wf.stack.clear()
    shell.client.skills_install = AsyncMock(return_value=p.SkillsInstallResult(status="conflict", conflicts=["x"]))
    await wf.operate(op)
    shell.generation += 1
    await wf.install_task
    assert shell.panel_title == "" and shell.toasts[-1]["level"] == "warning"


@pytest.mark.asyncio
async def test_install_errors_and_odd_names_are_shown_safely(shell):
    wf = shell.workflows
    shell.client.skills_install = AsyncMock(side_effect=RuntimeError("boom \x1b[31m"))
    await wf.settings_area("skills")
    await wf.operate({"kind": "skills_install_run", "scope": "project", "source": "owner/repo", "skills": []})
    await wf.install_task
    assert shell.toasts[-1]["level"] == "error" and "\x1b" not in shell.toasts[-1]["body"]
    from nexus.ui.ratatui.workflows import skills_install_summary
    title, level, body = skills_install_summary(p.SkillsInstallResult(status="partial", installed=["a\x1bb"], failed=["c: bad"]))
    assert level == "warning" and "\x1b" not in title and "Failed: c: bad" in body
    assert skills_install_summary(p.SkillsInstallResult(status="unavailable"))[1] == "warning"
    with pytest.raises(ValueError):
        wf.form = {"id": "f", "revision": 0}
        wf.form_target = {"kind": "skills_install_form", "scope": "global"}
        await wf.save("f", "   ", 1)


def _add_buttons(shell):
    return [i["operation"] for b in _blocks(shell) if b.get("t") == "buttons" for i in b["items"] if i["label"] == "Add server…"]


@pytest.mark.asyncio
async def test_mcp_page_offers_add_server_once_per_scope_and_only_there(shell):
    await shell.workflows.settings_area("mcp")
    assert _add_buttons(shell) == [{"kind": "mcp_add_open", "scope": "global"}, {"kind": "mcp_add_open", "scope": "project"}]
    for area in ("skills", "tools", "agents"):
        await shell.workflows.settings_area(area)
        assert not _add_buttons(shell)
    await shell.workflows.settings_area("mcp")
    from nexus.ui.ratatui.prototype import operation_allowed
    assert operation_allowed(shell, set(), {"kind": "mcp_add_open", "scope": "project"})
    assert not operation_allowed(shell, set(), {"kind": "mcp_add_run", "scope": "global"}), "only offered after the review"
    assert not operation_allowed(shell, set(), {"kind": "mcp_add_open", "scope": "elsewhere"})


@pytest.mark.asyncio
async def test_add_server_edits_one_json_server_then_confirms_masked_then_runs_in_the_background(shell):
    import asyncio
    import json
    gate = asyncio.Event()

    async def add(scope, name, entry, expected):
        await gate.wait()
        return p.SettingsWriteResult(status="written", loaded=["mcp:my"], message="my-server added to mcp.json")

    shell.client.settings_mcp_add = AsyncMock(side_effect=add)
    wf = shell.workflows
    await wf.settings_area("mcp")
    await wf.operate({"kind": "mcp_add_open", "scope": "project"})
    assert wf.form_target == {"kind": "mcp_add_form", "scope": "project"} and json.loads(wf.form["body"]) == {"my-server": {"command": "npx", "args": ["-y", "@scope/server"]}}
    body = json.dumps({"db": {"command": "/opt/bin/pg-mcp", "args": ["--ro", "x"], "env": {"PASSWORD": "sk-live-super-secret", "OK": "${env:OK}"}}})
    await wf.save(wf.form["id"], body, 1)
    shell.client.settings_mcp_add.assert_not_called()
    text = "\n".join(shell.panel_lines)
    assert "Nexus starts this command now and in new sessions." in text
    for expected in ("Name: db", "<project>/.agents/mcp.json", "Transport: stdio", "Command: pg-mcp · 2 arguments", "Environment: PASSWORD, OK (values hidden)", "may be committed"):
        assert expected in text, expected
    assert "sk-live-super-secret" not in text and "/opt/bin" not in text
    run = next(item["operation"] for item in shell.items if item["label"] == "Add server")
    assert run == {"kind": "mcp_add_run", "scope": "project"}, "the payload carries the scope, never the draft"
    assert "sk-live-super-secret" not in json.dumps(shell.items)

    await wf.operate(run)  # returns at once: the add is a background task
    assert shell.panel_title == wf.page_title and not wf.mcp_add_task.done() and wf.mcp_add_draft == {}
    await wf.operate(run)
    assert shell.toasts[-1]["level"] == "warning" and "already running" in shell.toasts[-1]["title"]
    gate.set()
    await wf.mcp_add_task
    shell.client.settings_mcp_add.assert_awaited_once_with("project", "db", json.loads(body)["db"], None)
    assert shell.toasts[-1]["level"] == "success" and "db" in shell.toasts[-1]["title"] and shell.panel_title == wf.page_title


@pytest.mark.asyncio
async def test_add_server_url_servers_say_connect_and_hide_credentials(shell):
    wf = shell.workflows
    await wf.settings_area("mcp")
    await wf.operate({"kind": "mcp_add_open", "scope": "global"})
    body = '{"mcpServers": {"api": {"url": "https://u:pw-secret-1@Host.example:8443/mcp?key=qs-secret-2", "headers": {"Authorization": "Bearer hdr-secret-3"}}}}'
    await wf.save(wf.form["id"], body, 1)
    text = "\n".join(shell.panel_lines)
    assert "URL: https://host.example:8443/mcp" in text and "Headers: Authorization (values hidden)" in text
    assert "connects to this URL" in text and "starts this command" not in text and "~/.nexus" in text
    for secret in ("pw-secret-1", "qs-secret-2", "hdr-secret-3"):
        assert secret not in text and secret not in str(shell.items)


@pytest.mark.asyncio
@pytest.mark.parametrize("status, level, fragment", [
    ("exists", "warning", "already exists"), ("conflict", "warning", "changed on disk"), ("legacy", "warning", "not added")])
async def test_add_server_refusals_are_toasts_not_navigation(shell, status, level, fragment):
    shell.client.settings_mcp_add = AsyncMock(return_value=p.SettingsWriteResult(status=status, message="because \x1b[31m"))
    wf = shell.workflows
    await wf.settings_area("mcp")
    await wf.operate({"kind": "mcp_add_open", "scope": "global"})
    await wf.save(wf.form["id"], '{"s": {"command": "x"}}', 1)
    await wf.operate({"kind": "mcp_add_run", "scope": "global"})
    await wf.mcp_add_task
    toast = shell.toasts[-1]
    assert toast["level"] == level and fragment in toast["title"] + toast["body"] and "\x1b" not in toast["body"]
    assert shell.panel_title == wf.page_title


@pytest.mark.asyncio
async def test_add_server_written_but_failed_to_load_warns_with_escaped_reasons(shell):
    shell.client.settings_mcp_add = AsyncMock(return_value=p.SettingsWriteResult(status="written", failed=["s: exit 1 \x1b[0m"], message="s added to mcp.json"))
    wf = shell.workflows
    await wf.settings_area("mcp")
    await wf.operate({"kind": "mcp_add_open", "scope": "global"})
    await wf.save(wf.form["id"], '{"s": {"command": "x"}}', 1)
    await wf.operate({"kind": "mcp_add_run", "scope": "global"})
    await wf.mcp_add_task
    assert shell.toasts[-1]["level"] == "warning" and "did not load" in shell.toasts[-1]["title"] and "\x1b" not in shell.toasts[-1]["body"]


@pytest.mark.asyncio
async def test_add_server_failure_and_stale_draft_are_safe(shell):
    wf = shell.workflows
    shell.client.settings_mcp_add = AsyncMock(side_effect=RuntimeError("boom \x1b[31m"))
    await wf.settings_area("mcp")
    await wf.operate({"kind": "mcp_add_run", "scope": "global"})  # no draft at all
    assert "no longer current" in shell.toasts[-1]["title"] and wf.mcp_add_task is None
    await wf.operate({"kind": "mcp_add_open", "scope": "global"})
    await wf.save(wf.form["id"], '{"s": {"command": "x"}}', 1)
    await wf.operate({"kind": "mcp_add_run", "scope": "project"})  # a different scope than the draft
    assert "no longer current" in shell.toasts[-1]["title"]
    await wf.operate({"kind": "mcp_add_open", "scope": "global"})
    await wf.save(wf.form["id"], '{"s": {"command": "x"}}', 1)
    shell.generation += 1  # a session or project switch between the review and the confirmation
    await wf.operate({"kind": "mcp_add_run", "scope": "global"})
    shell.client.settings_mcp_add.assert_not_called()
    await wf.operate({"kind": "mcp_add_open", "scope": "global"})
    await wf.save(wf.form["id"], '{"s": {"command": "x"}}', 1)
    await wf.operate({"kind": "mcp_add_run", "scope": "global"})
    await wf.mcp_add_task
    assert shell.toasts[-1]["level"] == "error" and "\x1b" not in shell.toasts[-1]["body"]


@pytest.mark.asyncio
async def test_add_server_result_after_a_switch_never_navigates(shell):
    shell.client.settings_mcp_add = AsyncMock(return_value=p.SettingsWriteResult(status="written", message="ok"))
    wf = shell.workflows
    await wf.settings_area("mcp")
    await wf.operate({"kind": "mcp_add_open", "scope": "global"})
    await wf.save(wf.form["id"], '{"s": {"command": "x"}}', 1)
    await wf.operate({"kind": "mcp_add_run", "scope": "global"})
    shell.panel_title = ""
    wf.stack.clear()
    shell.generation += 1
    await wf.mcp_add_task
    assert shell.panel_title == "" and shell.toasts[-1]["level"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("body, message", [
    ("", "Not valid JSON"), ("{", "Not valid JSON"), ('{"a": "NOPE-secret" oops}', "Not valid JSON"), ("[]", "one JSON object"),
    ('{"a": {"command": "x"}, "a": {"command": "y"}}', "Duplicate JSON key"),
    ('{"a": {"command": "x", "command": "y"}}', "Duplicate JSON key"),
    ('{"a": {"command": "x"}, "b": {"command": "y"}}', "exactly one server"),
    ('{"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}', "exactly one server"),
    ('{}', "exactly one server"),
    ('{"bad name": {"command": "x"}}', "Server name"), ('{"a": {}}', "non-empty"), ('{"a": "npx"}', "non-empty"),
    ('{"a": {"command": NaN}}', "NaN"),
    ('{"a": {"type": "ws", "url": "ws://x", "env": {"K": "NOPE-secret"}}}', "not supported"),
])
async def test_add_server_drafts_that_cannot_be_reviewed_stay_in_the_editor(shell, body, message):
    wf = shell.workflows
    await wf.settings_area("mcp")
    await wf.operate({"kind": "mcp_add_open", "scope": "global"})
    form = wf.form["id"]
    with pytest.raises(ValueError, match=message) as raised:
        await wf.save(form, body, 1)
    assert "NOPE-secret" not in str(raised.value) and wf.form is not None and wf.mcp_add_draft == {}


def test_add_draft_accepts_the_plain_map_and_every_wrapper_dialect():
    from nexus.ui.ratatui.settings_pages.mcp import parse_add_draft
    entry = {"command": "x", "args": ["1"]}
    assert parse_add_draft(json.dumps({"s": entry})) == ("s", entry)
    for wrapper in ("servers", "mcpServers", "mcp", "context_servers"):
        assert parse_add_draft(json.dumps({wrapper: {"s": entry}})) == ("s", entry)
    # a server that is itself called like a wrapper is still a server
    assert parse_add_draft(json.dumps({"mcp": entry})) == ("mcp", entry)
    assert parse_add_draft(json.dumps({"servers": {"mcp": entry}})) == ("mcp", entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("status, fragment", [("exists", "already exists"), ("legacy", "legacy .nexus"), ("conflict", "changed on disk")])
async def test_add_server_refusals_from_the_host_preview_never_open_the_confirmation(shell, status, fragment):
    shell.client.settings_mcp_add_preview = AsyncMock(return_value=p.SettingsWriteResult(status=status, message="s already exists in mcpServers; legacy .nexus/mcp.json"))
    wf = shell.workflows
    await wf.settings_area("mcp")
    await wf.operate({"kind": "mcp_add_open", "scope": "global"})
    with pytest.raises(ValueError, match=fragment):
        await wf.save(wf.form["id"], '{"s": {"command": "x"}}', 1)
    assert wf.form is not None and wf.mcp_add_draft == {}
    assert not [item for item in shell.items if item["label"] == "Add server"]


def test_the_mcp_settings_page_loads_no_mcp_code():
    import ast
    import nexus.ui.ratatui.settings_pages.mcp as page
    tree = ast.parse(open(page.__file__, encoding="utf-8").read())
    modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not [m for m in modules if m.endswith(("mcp.client", "mcp.errors"))]


@pytest.mark.asyncio
async def test_every_operation_the_client_can_send_is_accepted_by_the_bridge(shell):
    from settings_page_ops import assert_every_client_op_is_accepted
    for area in ("agents", "tools", "skills", "mcp"):
        await shell.workflows.settings_area(area)
        assert_every_client_op_is_accepted(shell)
