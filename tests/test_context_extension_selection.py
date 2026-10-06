"""Discovery, durable choices, execution filtering and first-turn cache locks."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.runtime import Runtime
from nexus.model.providers.scripted import ScriptedProvider, text_response
from test_agent_selection import _config


def skill(root, name):
    folder = root / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Use {name}\n---\nInstructions for {name}\n")


def server():
    return {"command": sys.executable, "args": [str(Path(__file__).parent / "fixtures/mcp/fs_server.py")]}


async def test_discovery_selection_replay_and_cache_lock(tmp_path):
    import json
    workspace = tmp_path / "project"
    workspace.mkdir()
    home = tmp_path / "home"
    skill(home / ".nexus", "global-skill")
    skill(workspace / ".agents", "project-skill")
    (home / ".nexus/mcp.json").write_text(json.dumps({"mcpServers": {"global-server": server(), "shared": server()}}))
    (workspace / ".agents/mcp.json").write_text(json.dumps({"servers": {"project-server": server(), "shared": {**server(), "enabled": False}}}))
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(workspace, home=home, config=_config(), providers={"scripted": provider})
    facade = HostFacade(runtime)
    facade.open_session("s")
    try:
        result = await facade.handle(p.ContextInspect(session="s"))
        assert isinstance(result, p.ContextInspectResult), result
        assert not result.context_locked
        skills = {row["name"]: row for row in result.skills_index}
        assert skills["global-skill"]["scope"] == "global"
        assert skills["project-skill"]["scope"] == "project"
        servers = {row["name"]: row for row in result.mcp_servers}
        assert servers["global-server"]["scope"] == "global"
        assert servers["project-server"]["scope"] == "project"
        assert servers["shared"]["scope"] == "project"
        assert servers["project-server"]["tool_count"] > 0
        assert not servers["shared"]["enabled"]
        disabled_tool_names = set(servers["project-server"]["tools"])
        assert disabled_tool_names
        result = await facade.handle(p.ContextMcpLoadingSelect(
            session="s", server="project-server", mode="all",
        ))
        mcp_name = next(row["name"] for row in result.tools if row["name"] in disabled_tool_names)
        mcp_before = next(row for row in result.tools if row["name"] == mcp_name)
        mcp_off = await facade.handle(p.ContextExtensionSelect(
            session="s", category="tools", name=mcp_name, enabled=False,
        ))
        mcp_row = next(row for row in mcp_off.tools if row["name"] == mcp_name)
        assert not mcp_row["enabled"]
        assert mcp_row["input_schema"] == mcp_before["input_schema"]
        assert mcp_row["description"] == mcp_before["description"]
        assert mcp_row["source"] == "mcp:project-server"
        await facade.handle(p.ContextExtensionSelect(
            session="s", category="tools", name=mcp_name, enabled=True,
        ))
        for category, name in (("skills", "project-skill"), ("mcp", "project-server")):
            selected = await facade.handle(p.ContextExtensionSelect(session="s", category=category, name=name, enabled=False))
            assert isinstance(selected, p.ContextInspectResult), selected
        result = await facade.handle(p.ContextInspect(session="s"))
        assert not next(row for row in result.skills_index if row["name"] == "project-skill")["included"]
        assert not disabled_tool_names.intersection(row["name"] for row in result.tools)
        assert "project-skill:" not in (result.system_text or "")
        assert runtime._selected_skills(runtime.session("s")).get("PROJECT-SKILL") is None
        from nexus.tools.spec import ToolContext
        selected_manifest = runtime._selected_manifest(runtime.manifest, runtime.session("s"))
        resource_result = await selected_manifest.tools["ReadMcpResource"].run(
            {"server": "project-server", "uri": "file:///secret"}, ToolContext(workspace=workspace, session_id="s", turn_id="test", config=_config()))
        assert resource_result.is_error
        from types import SimpleNamespace
        for logical_id in ("s/sub/1", "s/sub/1/sub/1"):
            child_manager = runtime._build_child_tool_manager(
                SimpleNamespace(session_id=logical_id, workspace=workspace,
                                tools=("ReadMcpResource", *disabled_tool_names), permissions=None),
                _config(), None)
            assert not disabled_tool_names.intersection(child_manager.names)
            resource = next(tool for tool in child_manager.tools if tool.name == "ReadMcpResource")
            outcome = await resource.run({"server": "project-server", "uri": "file:///secret"},
                                         ToolContext(workspace=workspace, session_id="s", turn_id="test", config=_config()))
            assert outcome.is_error
        facade.open_session("other")
        assert not runtime.session("other").disabled_extensions["skills"]
        fork = runtime.sessions.fork("s", new_id="branch")
        assert fork.disabled_extensions["skills"] == {"project-skill"}
        unknown = await facade.handle(p.ContextExtensionSelect(session="s", category="skills", name="missing", enabled=False))
        assert isinstance(unknown, p.ErrorResult)
        [event async for event in runtime.session("s").send("hello")]
        result = await facade.handle(p.ContextInspect(session="s"))
        assert result.context_locked
        request = provider.requests[-1]
        assert "project-skill:" not in (request.system or "")
        assert not disabled_tool_names.intersection(tool.name for tool in request.tools)
        for command in (p.ContextExtensionSelect(session="s", category="skills", name="project-skill", enabled=True), p.AgentSelect(session="s", name="build"), p.AgentReset(session="s")):
            denied = await facade.handle(command)
            assert isinstance(denied, p.ErrorResult)
            assert "prompt cache" in denied.message
    finally:
        await runtime.aclose()
    reopened = Runtime(workspace, home=home, config=_config(), providers={"scripted": ScriptedProvider(text_response("ok"))})
    try:
        handle = reopened.session("s")
        assert handle.context_locked
        assert handle.disabled_extensions == {"skills": {"project-skill"}, "mcp": {"project-server"}, "tools": set()}
    finally:
        await reopened.aclose()


async def test_a_single_tool_can_be_switched_off_and_back_on_until_the_first_turn(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(workspace, home=tmp_path / "home", config=_config(), providers={"scripted": provider})
    facade = HostFacade(runtime)
    facade.open_session("s")
    try:
        before = await facade.handle(p.ContextInspect(session="s"))
        name = next(row["name"] for row in before.tools if row["name"] not in {"Task", "ReadMcpResource"})
        assert all(row["enabled"] for row in before.tools)
        off = await facade.handle(p.ContextExtensionSelect(session="s", category="tools", name=name, enabled=False))
        assert isinstance(off, p.ContextInspectResult), off
        row = next(row for row in off.tools if row["name"] == name)
        assert row["enabled"] is False, "a switched-off tool stays listed so it can be switched back on"
        original = next(row for row in before.tools if row["name"] == name)
        assert {key: value for key, value in row.items() if key != "enabled"} == {
            key: value for key, value in original.items() if key != "enabled"
        }
        assert row["input_schema"]
        assert row["source"] == "built-in"
        assert isinstance(row["read_only"], bool)
        assert "permission" not in row
        assert len([r for r in off.tools if r["name"] == name]) == 1
        assert runtime.session("s").disabled_extensions["tools"] == {name}
        fork = runtime.sessions.fork("s", new_id="tool-branch")
        assert fork.disabled_extensions["tools"] == {name}
        unknown = await facade.handle(p.ContextExtensionSelect(session="s", category="tools", name="nope", enabled=False))
        assert isinstance(unknown, p.ErrorResult)
        on = await facade.handle(p.ContextExtensionSelect(session="s", category="tools", name=name, enabled=True))
        assert next(row for row in on.tools if row["name"] == name)["enabled"] is True
        await facade.handle(p.ContextExtensionSelect(session="s", category="tools", name=name, enabled=False))
        [event async for event in runtime.session("s").send("hello")]
        assert name not in {tool.name for tool in provider.requests[-1].tools}
        denied = await facade.handle(p.ContextExtensionSelect(session="s", category="tools", name=name, enabled=True))
        assert isinstance(denied, p.ErrorResult)
    finally:
        await runtime.aclose()


async def test_task_toggle_and_agent_allowlist(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    agents = workspace / ".agents" / "agents"
    agents.mkdir(parents=True)
    (agents / "reader.md").write_text("---\nname: reader\ndescription: Reader\ncontexts: [root]\ntools: [read]\n---\nRead only.\n")
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(workspace, home=tmp_path / "home", config=_config(), providers={"scripted": provider})
    facade = HostFacade(runtime)
    facade.open_session("s")
    try:
        before = await facade.handle(p.ContextInspect(session="s"))
        assert any(row["name"] == "subagent" and row["enabled"] for row in before.tools)
        assert isinstance(await facade.handle(p.ContextExtensionSelect(
            session="s", category="tools", name="subagent", enabled=False,
        )), p.ContextInspectResult)
        disabled = await facade.handle(p.ContextInspect(session="s"))
        task_row = next(row for row in disabled.tools if row["name"] == "subagent")
        assert not task_row["enabled"]
        assert task_row["input_schema"] == next(row for row in before.tools if row["name"] == "subagent")["input_schema"]
        assert task_row["source"] == "built-in"
        async for _ in runtime.session("s").send("hi"):
            pass
        assert "subagent" not in {tool.name for tool in provider.requests[0].tools}
        facade.open_session("reader-session")
        await facade.handle(p.AgentSelect(session="reader-session", name="reader"))
        selected = await facade.handle(p.ContextInspect(session="reader-session"))
        assert {row["name"] for row in selected.tools} == {"read"}
        rejected = await facade.handle(p.ContextExtensionSelect(
            session="reader-session", category="tools", name="subagent", enabled=False,
        ))
        assert isinstance(rejected, p.ErrorResult)
    finally:
        await runtime.aclose()


async def test_settings_disabled_web_tool_is_visible_but_not_enabled(tmp_path):
    from dataclasses import replace
    from nexus.config.schema import ToolsSection, WebSection

    config = _config()
    import msgspec
    config = replace(config, v2=msgspec.structs.replace(config.v2, tools=ToolsSection(web=WebSection(fetch_enabled=False))))
    runtime = Runtime(tmp_path, home=tmp_path / "home", config=config,
                      providers={"scripted": ScriptedProvider(text_response("ok"))})
    facade = HostFacade(runtime)
    facade.open_session("s")
    try:
        result = await facade.handle(p.ContextInspect(session="s"))
        row = next(row for row in result.tools if row["name"] == "webfetch")
        from nexus.tools.builtin import WEBFETCH_SPEC

        assert row["description"] == WEBFETCH_SPEC.description
        assert row["input_schema"] == WEBFETCH_SPEC.input_schema
        assert row["bundle"] == WEBFETCH_SPEC.bundle
        assert row["read_only"] == (not WEBFETCH_SPEC.mutates)
        assert row["source"] == "built-in"
        assert row["enabled"] is False
        assert row["config_enabled"] is False
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("tool_name", ["webfetch", "websearch"])
async def test_session_disabled_available_web_tool_schema_and_inspect(tmp_path, tool_name):
    provider = ScriptedProvider(text_response("ok"), text_response("ok"))
    runtime = Runtime(
        tmp_path, home=tmp_path / "home", config=_config(),
        providers={"scripted": provider},
    )
    facade = HostFacade(runtime)
    facade.open_session("s")
    web_names = {"webfetch", "websearch"}
    try:
        before = await facade.handle(p.ContextInspect(session="s"))
        assert web_names <= {row["name"] for row in before.tools if row["enabled"]}
        off = await facade.handle(p.ContextExtensionSelect(
            session="s", category="tools", name=tool_name, enabled=False,
        ))
        assert isinstance(off, p.ContextInspectResult), off
        row = next(row for row in off.tools if row["name"] == tool_name)
        assert not row["enabled"]
        assert row["config_enabled"]
        on = await facade.handle(p.ContextExtensionSelect(
            session="s", category="tools", name=tool_name, enabled=True,
        ))
        assert isinstance(on, p.ContextInspectResult), on
        assert next(row for row in on.tools if row["name"] == tool_name)["enabled"]
        # Keep a reenabled branch for a provider request: the first turn locks
        # extension selection, so reenable must be exercised before that turn.
        runtime.sessions.fork("s", new_id="reenabled")
        off = await facade.handle(p.ContextExtensionSelect(
            session="s", category="tools", name=tool_name, enabled=False,
        ))
        [event async for event in runtime.session("s").send("hello")]
        advertised = {tool.name for tool in provider.requests[-1].tools}
        assert web_names & advertised == web_names - {tool_name}
        assert advertised == {row["name"] for row in off.tools if row["enabled"]}
        inspected = await facade.handle(p.ContextInspect(session="s"))
        assert not next(row for row in inspected.tools if row["name"] == tool_name)["enabled"]
        [event async for event in runtime.session("reenabled").send("hello")]
        advertised = {tool.name for tool in provider.requests[-1].tools}
        assert web_names <= advertised
        assert advertised == {row["name"] for row in on.tools if row["enabled"]}
    finally:
        await runtime.aclose()


def test_disabled_mcp_search_wrapper_is_not_reintroduced():
    from types import SimpleNamespace
    from nexus.runtime import Runtime
    from nexus.mcp.bridge import build_search_tools

    class Catalog:
        def statuses(self):
            return (SimpleNamespace(name="search", enabled=True),)

        def snapshot(self):
            return SimpleNamespace(servers=())

        def tool_catalog(self):
            return {"search": SimpleNamespace(tools=(SimpleNamespace(name="mcp__search__echo"),))}

    catalog = Catalog()
    wrapper = build_search_tools(catalog, {"search": "search"})[0]
    runtime = Runtime.__new__(Runtime)
    runtime._mcp = catalog
    from nexus.ext.manifest import Manifest
    manifest = Manifest(tools={wrapper.name: wrapper}, mcp={"search": {"tool_loading": "search"}})
    session = SimpleNamespace(disabled_extensions={"tools": {wrapper.name}, "skills": set(), "mcp": set()})
    selected = runtime._selected_manifest(manifest, session)
    assert wrapper.name not in selected.tools


async def test_disabled_extension_retains_schema_and_metadata(tmp_path):
    tools = tmp_path / ".agents" / "tools"
    tools.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures/extensions/echo_spec.py"
    (tools / "echo.py").write_text(fixture.read_text().replace(
        '"mutates": False,', '"mutates": False, "timeout_s": 7.0,'
    ))
    runtime = Runtime(tmp_path, home=tmp_path / "home", config=_config(),
                      providers={"scripted": ScriptedProvider(text_response("ok"))})
    facade = HostFacade(runtime)
    facade.open_session("s")
    try:
        before = await facade.handle(p.ContextInspect(session="s"))
        original = next(row for row in before.tools if row["name"] == "EchoFixture")
        off = await facade.handle(p.ContextExtensionSelect(
            session="s", category="tools", name="EchoFixture", enabled=False,
        ))
        row = next(row for row in off.tools if row["name"] == "EchoFixture")
        assert row == {**original, "enabled": False}
        assert row["input_schema"]["required"] == ["message"]
        assert row["source"] == "extension"
        source = Path(runtime.manifest.tools["EchoFixture"].source)
        raw = await runtime.inspect_context(runtime.session("s"))
        raw_row = next(row for row in raw["tools"] if row["name"] == "EchoFixture")
        assert raw_row["origin"] == str(source.relative_to(tmp_path))
        assert row["origin"]  # Host output retains its existing redaction.
        assert row["bundle"] == "fs"
        assert row["read_only"] is True
        assert row["timeout_s"] == 7.0
        assert "permission" not in row
    finally:
        await runtime.aclose()
