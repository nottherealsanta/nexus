"""Root-agent selection durability, eligibility, and turn-time application."""
from __future__ import annotations

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
    WebSection,
)
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime


def _config(name: str = "general", *, web: WebSection | None = None) -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name=name),
            model=ModelSection(default="scripted/m"),
            permissions=PermissionsSection(mode="allow"),
            tools=ToolsSection(web=web or WebSection()),
        ),
    )


async def _runtime(path, *, agent="general") -> Runtime:
    return Runtime(path, config=_config(agent), providers={"scripted": ScriptedProvider(text_response("ok"))})


async def test_agent_select_replay_reopen_fork_and_reset(tmp_path):
    runtime = await _runtime(tmp_path)
    facade = HostFacade(runtime)
    facade.open_session("s")

    current = await facade.handle(p.AgentCurrent(session="s"))
    assert (current.name, current.source) == ("general", "config")
    selected = await facade.handle(p.AgentSelect(session="s", name="plan"))
    assert isinstance(selected, p.AgentSelectResult)
    assert selected.apply_next_turn and selected.name == "plan"
    handle = runtime.session("s")
    assert [e.type for e in handle.events].count("agent.selected") == 1
    assert handle.agent_selection.name == "plan"

    fork = runtime.sessions.fork("s", new_id="branch")
    assert fork.agent_selection.name == "plan"
    await runtime.aclose()

    reopened = await _runtime(tmp_path)
    assert reopened.session("s").agent_selection.name == "plan"
    reset = await HostFacade(reopened).handle(p.AgentReset(session="s"))
    assert reset.name == "general" and reset.source == "config"
    assert reopened.session("s").agent_selection is None
    await reopened.aclose()


async def test_unknown_and_subagent_only_selection_are_rejected(tmp_path):
    runtime = await _runtime(tmp_path)
    facade = HostFacade(runtime)
    facade.open_session("s")
    for name in ("missing",):
        result = await facade.handle(p.AgentSelect(session="s", name=name))
        assert isinstance(result, p.ErrorResult)
    (tmp_path / ".nexus" / "agents" / "delegated.md").write_text(
        "---\nname: delegated\ndescription: delegated only\n---\nbody\n",
        encoding="utf-8",
    )
    runtime.agents.refresh()
    result = await facade.handle(p.AgentSelect(session="s", name="delegated"))
    assert isinstance(result, p.ErrorResult)
    assert runtime.session("s").agent_selection is None
    await runtime.aclose()


async def test_cli_agent_command_parsing_and_completion():
    from nexus.ui.cli import commands

    parsed = commands.parse("/agent list")
    assert parsed is not None and parsed.name == "/agent" and parsed.args == ("list",)
    assert "general" not in commands.help_text()
    assert "Enter submits" in commands.help_text()
    assert "Shift+Enter inserts a line" in commands.help_text()
    assert "Ctrl+S submits" not in commands.help_text()


async def test_selected_agent_applies_next_turn_and_plan_is_read_only(tmp_path):
    provider = ScriptedProvider(text_response("planned"))
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    facade = HostFacade(runtime)
    facade.open_session("s")
    await facade.handle(p.AgentSelect(session="s", name="plan"))
    await facade.start_turn("s", "inspect")
    await facade.wait_idle(timeout=5)

    handle = runtime.session("s")
    started = next(event for event in handle.events if event.type == "turn.started")
    assert started.data["agent"]["name"] == "plan"
    assert started.data["agent"]["source"] == "session"
    assert started.data["agent"]["read_only"] is True
    assert {tool.name for tool in provider.requests[0].tools} == {
        "read", "glob", "grep", "subagent", "todowrite", "skill"
    }
    assert not ({"write", "edit", "multiedit", "bash", "websearch"} & set(started.data["agent"]["tools"]))
    assert "read-only planning agent" in provider.requests[0].system
    await runtime.aclose()


async def test_default_root_catalog_advertises_canonical_tools(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    [event async for event in runtime.session("catalog").send("inspect")]

    assert {tool.name for tool in provider.requests[0].tools} == {
        "read", "glob", "grep", "edit", "write", "bash", "apply_patch",
        "subagent", "todowrite", "webfetch", "skill",
    }
    assert all(tool.name == tool.name.lower() for tool in provider.requests[0].tools)
    rows = await runtime.list_tools()
    unavailable_search = next(row for row in rows if row["name"] == "websearch")
    assert unavailable_search["availability"] == "unavailable"
    assert "SearXNG" in unavailable_search["reason"]
    assert "input_schema" not in unavailable_search
    await runtime.aclose()


async def test_disabled_fetch_is_not_advertised_and_has_host_reason(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(
        tmp_path,
        config=_config(web=WebSection(fetch_enabled=False)),
        providers={"scripted": provider},
    )
    [event async for event in runtime.session("fetch-disabled").send("inspect")]
    assert "webfetch" not in {tool.name for tool in provider.requests[0].tools}
    row = next(row for row in await runtime.list_tools() if row["name"] == "webfetch")
    assert row["availability"] == "unavailable"
    assert "fetch_enabled" in row["reason"]
    await runtime.aclose()


async def test_missing_outbound_service_is_reported_as_unavailable(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    runtime._outbound_http_service = None
    [event async for event in runtime.session("http-unavailable").send("inspect")]
    assert not ({"webfetch", "websearch"} & {tool.name for tool in provider.requests[0].tools})
    rows = await runtime.list_tools()
    for name in ("webfetch", "websearch"):
        row = next(row for row in rows if row["name"] == name)
        assert row["availability"] == "unavailable"
        assert "Outbound HTTP service" in row["reason"]
    await runtime.aclose()


async def test_configured_web_bundle_is_selectable_but_children_keep_parent_ceiling(tmp_path):
    from nexus.model.providers.scripted import tool_response

    configured = WebSection(
        searxng_instances=["https://Search.Example/"],
        allowed_origins=["https://search.example"],
    )
    provider = ScriptedProvider(
        tool_response(("root-task", "subagent", {"prompt": "delegate", "subagent_type": "general"})),
        tool_response(("child-task", "subagent", {"prompt": "delegate again", "subagent_type": "general"})),
        text_response("grandchild report"),
        text_response("child report"),
        text_response("root report"),
    )
    runtime = Runtime(tmp_path, config=_config(web=configured), providers={"scripted": provider})
    [event async for event in runtime.session("web-tree").send("delegate")]

    expected = {
        "read", "glob", "grep", "edit", "write", "bash", "apply_patch",
        "subagent", "todowrite", "webfetch", "websearch", "skill",
    }
    assert [{tool.name for tool in request.tools} for request in provider.requests] == [expected] * 5
    assert all(name == name.lower() for request in provider.requests for name in (tool.name for tool in request.tools))
    await runtime.aclose()


async def test_research_profile_keeps_web_read_only_through_grandchildren(tmp_path):
    from nexus.model.providers.scripted import tool_response

    configured = WebSection(
        searxng_instances=["https://search.example/"],
        allowed_origins=["https://search.example"],
    )
    provider = ScriptedProvider(
        tool_response(("root-task", "subagent", {"prompt": "delegate", "subagent_type": "general"})),
        tool_response(("child-task", "subagent", {"prompt": "delegate again", "subagent_type": "general"})),
        text_response("grandchild report"),
        text_response("child report"),
        text_response("root report"),
    )
    runtime = Runtime(
        tmp_path,
        config=_config("general", web=configured),
        providers={"scripted": provider},
    )
    # The workspace role is read-only and selects the research profile.
    role_dir = tmp_path / ".nexus" / "agents"
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "general.md").write_text(
        "---\nname: general\ndescription: general researcher\ncontexts: [root, subagent]\n"
        "profile: research\n---\nResearch only.\n",
        encoding="utf-8",
    )
    runtime.agents.refresh()
    [event async for event in runtime.session("research-tree").send("delegate")]

    expected = {"read", "glob", "grep", "subagent", "todowrite", "webfetch", "websearch", "skill"}
    catalogs = [{tool.name for tool in request.tools} for request in provider.requests]
    assert catalogs == [expected] * 5
    assert all(not ({"write", "edit", "bash", "apply_patch"} & names) for names in catalogs)
    await runtime.aclose()


async def test_read_only_explore_cannot_request_unavailable_websearch_or_widen(tmp_path):
    from nexus.model.providers.scripted import tool_response

    (tmp_path / ".nexus" / "agents").mkdir(parents=True)
    (tmp_path / ".nexus" / "agents" / "explore.md").write_text(
        "---\nname: explore\ndescription: constrained explorer\ncontexts: [subagent]\n"
        "tools: [read, glob, grep, subagent, todowrite, webfetch, websearch, bash, write]\n"
        "---\nExplore safely.\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        tool_response(("root-task", "subagent", {"prompt": "explore", "subagent_type": "explore"})),
        text_response("findings"),
        text_response("done"),
    )
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    [event async for event in runtime.session("explore-ceiling").send("delegate")]
    spawned = next(event for event in runtime.session("explore-ceiling").events if event.type == "agent.spawned")
    assert set(spawned.data["tools"]) == {"read", "glob", "grep", "subagent", "todowrite", "webfetch"}
    assert "websearch" in spawned.data["dropped_tools"]
    assert all("websearch" not in {tool.name for tool in request.tools} for request in provider.requests[1:])
    await runtime.aclose()


async def test_child_and_grandchild_catalogs_inherit_parent_authority(tmp_path):
    from nexus.model.providers.scripted import tool_response

    provider = ScriptedProvider(
        tool_response(("root-task", "subagent", {
            "prompt": "delegate",
            "subagent_type": "general",
        })),
        tool_response(("child-task", "subagent", {
            "prompt": "delegate again",
            "subagent_type": "general",
        })),
        text_response("grandchild report"),
        text_response("child report"),
        text_response("root report"),
    )
    runtime = Runtime(tmp_path, config=_config("plan"), providers={"scripted": provider})
    [event async for event in runtime.session("tree").send("delegate")]

    expected = {"read", "glob", "grep", "subagent", "todowrite", "skill"}
    catalogs = [
        {tool.name for tool in request.tools}
        for request in provider.requests
    ]
    # Each delegated agent needs a tool call and a final response; the model is
    # requested again after a tool result. The inherited catalog stays fixed
    # across every request in the root/child/grandchild chain.
    assert len(catalogs) == 5
    assert catalogs == [expected] * len(catalogs)
    assert all(not (catalog - expected) for catalog in catalogs)
    await runtime.aclose()


async def test_read_only_declaration_cannot_widen_parent_tool_authority(tmp_path):
    from nexus.model.providers.scripted import tool_response

    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "explore.md").write_text(
        "---\nname: explore\ndescription: constrained planner\n"
        "contexts: [subagent]\ntools: [read, glob, grep, subagent, todowrite, skill, webfetch, websearch, bash, write]\n"
        "---\nplan\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        tool_response(("root-task", "subagent", {
            "prompt": "plan safely",
            "subagent_type": "explore",
        })),
        text_response("plan report"),
        text_response("root report"),
    )
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    # The root catalog contains the full coding authority; the read-only role's
    # own declarations still cannot add bash/write or any unavailable tools.
    [event async for event in runtime.session("readonly-ceiling").send("delegate")]

    spawned = next(
        event for event in runtime.session("readonly-ceiling").events
        if event.type == "agent.spawned"
    )
    assert set(spawned.data["tools"]) == {
        "read", "glob", "grep", "subagent", "todowrite", "webfetch", "skill"
    }
    assert not ({"bash", "write", "edit", "multiedit"} & set(spawned.data["tools"]))
    # A skill activation's declared tools are intersected with this iteration's
    # catalog; declarations by a role never create a catalog entry.
    assert {tool.name for tool in provider.requests[0].tools} == {
        "read", "glob", "grep", "edit", "write", "bash", "apply_patch",
        "subagent", "todowrite", "webfetch", "skill",
    }
    await runtime.aclose()


async def test_skill_activation_cannot_widen_child_catalog(tmp_path):
    from nexus.model.providers.scripted import tool_response

    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "explore.md").write_text(
        "---\nname: explore\ndescription: read-only child\ncontexts: [subagent]\n"
        "---\nexplore\n",
        encoding="utf-8",
    )
    skill = tmp_path / ".nexus" / "skills" / "broad"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: broad\ndescription: tries to widen child access\n"
        "allowed-tools: [Read, Write, Bash]\n---\nbody\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        tool_response(("root-task", "subagent", {
            "prompt": "inspect",
            "subagent_type": "explore",
        })),
        tool_response(("child-skill", "skill", {"name": "broad"})),
        text_response("explored"),
        text_response("root report"),
    )
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    [event async for event in runtime.session("skill-ceiling").send("delegate")]

    expected = {"read", "glob", "grep", "subagent", "todowrite", "webfetch", "skill"}
    assert len(provider.requests) == 4
    assert {tool.name for tool in provider.requests[1].tools} == expected
    assert {tool.name for tool in provider.requests[2].tools} == expected
    assert {tool.name for tool in provider.requests[3].tools} == {
        "read", "glob", "grep", "edit", "write", "bash", "apply_patch",
        "subagent", "todowrite", "webfetch", "skill",
    }
    await runtime.aclose()


async def test_explicit_session_model_overrides_root_agent_default(tmp_path):
    from nexus.model.capabilities import Capabilities

    provider = ScriptedProvider(
        text_response("ok"), name="scripted", capabilities=Capabilities(thinking=True)
    )
    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "custom.md").write_text(
        "---\nname: custom\ndescription: custom\ncontexts: [root]\n"
        "model: other-model\nreasoning_effort: high\n---\ncustom body\n",
        encoding="utf-8",
    )
    runtime = Runtime(tmp_path, config=_config("custom"), providers={"scripted": provider})
    runtime.select_session_model("explicit", "scripted/selected-model")
    [event async for event in runtime.session("explicit").send("go")]
    request = provider.requests[0]
    assert request.model == "selected-model"
    assert request.params.reasoning_effort is None
    await runtime.aclose()


async def test_child_task_model_overrides_role_default_and_keeps_role_body(tmp_path):
    from nexus.model.providers.scripted import tool_response

    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "custom-child.md").write_text(
        "---\nname: custom-child\ndescription: child\ncontexts: [subagent]\n"
        "provider: scripted\nmodel: role-model\ntools: [Read]\n---\n"
        "Child-specific instructions.\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        tool_response(("task", "subagent", {
            "prompt": "do child work",
            "subagent_type": "custom-child",
            "model": "task-model",
        })),
        text_response("child finished"),
        text_response("root finished"),
    )
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    [event async for event in runtime.session("child-default").send("delegate")]
    assert provider.requests[1].model == "task-model"
    assert "Child-specific instructions." in provider.requests[1].system
    assert {tool.name for tool in provider.requests[1].tools} == {"read"}
    await runtime.aclose()


async def test_disappeared_selected_definition_fails_before_user_content(tmp_path):
    runtime = await _runtime(tmp_path)
    facade = HostFacade(runtime)
    facade.open_session("s")
    custom_dir = tmp_path / ".nexus" / "agents"
    custom_dir.mkdir(parents=True, exist_ok=True)
    custom_path = custom_dir / "custom-root.md"
    custom_path.write_text(
        "---\nname: custom-root\ndescription: temporary root\ncontexts: [root]\n---\nprompt\n",
        encoding="utf-8",
    )
    runtime.agents.refresh()
    await facade.handle(p.AgentSelect(session="s", name="custom-root"))
    custom_path.unlink()
    await facade.start_turn("s", "must not persist")
    await facade.wait_idle(timeout=5)
    assert not any(message.role == "user" for message in runtime.session("s").messages)
    failed = [event for event in runtime.session("s").events if event.type == "turn.failed"]
    assert failed and "root agent" in failed[-1].data["error"]
    await runtime.aclose()


async def test_root_tool_narrowing_also_caps_delegated_children(tmp_path):
    from nexus.model.providers.scripted import tool_response

    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "narrow-root.md").write_text(
        "---\nname: narrow-root\ndescription: narrow root\n"
        "contexts: [root]\ntools: [Read, Task]\n---\nroot prompt\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        tool_response(
            ("parent-task", "Task", {
                "prompt": "write something",
                "subagent_type": "general",
                "tools": ["Write", "Bash", "Read"],
            })
        ),
        text_response("child report"),
        text_response("root report"),
    )
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    facade = HostFacade(runtime)
    await facade.handle(p.AgentSelect(session="s", name="narrow-root"))
    await facade.start_turn("s", "delegate")
    await facade.wait_idle(timeout=5)

    spawned = next(event for event in runtime.session("s").events if event.type == "agent.spawned")
    assert spawned.data["tools"] == ["read"]
    assert set(spawned.data["dropped_tools"]) >= {"write", "bash"}
    await runtime.aclose()


async def test_subagent_profile_is_a_ceiling_even_when_parent_has_more(tmp_path):
    from nexus.model.providers.scripted import tool_response

    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "researcher.md").write_text(
        "---\nname: researcher\ndescription: research child\n"
        "contexts: [subagent]\nprofile: research\n---\nresearch prompt\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        tool_response(("parent-task", "Task", {
            "prompt": "investigate",
            "subagent_type": "researcher",
        })),
        text_response("child report"),
        text_response("root report"),
    )
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    facade = HostFacade(runtime)
    await facade.start_turn("s", "delegate")
    await facade.wait_idle(timeout=5)

    spawned = next(event for event in runtime.session("s").events if event.type == "agent.spawned")
    tools = set(spawned.data["tools"])
    assert {"read", "grep", "glob", "subagent", "todowrite", "skill"} <= tools
    assert not ({"write", "edit", "multiedit", "bash", "BashOutput", "KillShell"} & tools)
    await runtime.aclose()
