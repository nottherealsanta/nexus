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
        assert handle.disabled_extensions == {"skills": {"project-skill"}, "mcp": {"project-server"}}
    finally:
        await reopened.aclose()


@pytest.mark.parametrize("category", ["skills", "mcp"])
@pytest.mark.parametrize("locked", [False, True])
async def test_tui_individual_controls(category, locked):
    from test_ui_tui import FakeTransport, _client
    from nexus.ui.tui.app import NexusTextualApp
    from nexus.ui_support.tui_context_header import ContextHeader, ExtensionsModal
    from textual.widgets import Button

    class Transport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.enabled = True
            self.selections = []

        async def request(self, command):
            if isinstance(command, (p.ContextInspect, p.ContextExtensionSelect)):
                if isinstance(command, p.ContextExtensionSelect):
                    self.selections.append(command)
                    self.enabled = command.enabled
                return p.ContextInspectResult(session=command.session, context_locked=locked,
                    skills_index=[{"name": "skill", "scope": "project", "enabled": self.enabled}],
                    mcp_servers=[{"name": "server", "scope": "global", "enabled": self.enabled}])
            return await super().request(command)

    transport = Transport()
    app = NexusTextualApp(_client(transport))
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        result = await app.controller.client.inspect_context(app.controller.session)
        app.query_one(ContextHeader).set_data(result)
        await pilot.click(f"#context-{category}")
        await pilot.pause()
        assert isinstance(app.screen, ExtensionsModal)
        button = app.screen.query_one("#extension-0", Button)
        assert button.disabled == locked
        if not locked:
            await pilot.click("#extension-0")
            await pilot.pause()
            assert transport.selections[-1].enabled is False
            assert str(button.label).startswith("Off")
            await pilot.pause(0.4)
            await pilot.click("#extension-0")
            await pilot.pause()
            assert transport.selections[-1].enabled is True
        else:
            await pilot.click("#extension-0")
            await pilot.pause()
            assert not transport.selections
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
