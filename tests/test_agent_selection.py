"""Root-agent selection durability, eligibility, and turn-time application."""
from __future__ import annotations

from nexus.model.message import Message, Text
from nexus.config import Config
from nexus.config.schema import (
    AgentsSection,
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

#: Tools only a subagent has; they grant nothing, so authority comparisons ignore them.
CHILD_ONLY = frozenset({"escalate"})


def _config(name: str = "general", *, web: WebSection | None = None, background: bool = True) -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name=name),
            model=ModelSection(default="scripted/m"),
            permissions=PermissionsSection(mode="allow"),
            tools=ToolsSection(web=web or WebSection()),
            agents=AgentsSection(background=background),
        ),
    )


async def _runtime(path, *, agent="general") -> Runtime:
    _write_plan_agent(path)
    return Runtime(path, config=_config(agent), providers={"scripted": ScriptedProvider(text_response("ok"))})


def _write_plan_agent(path) -> None:
    """A custom root agent named ``plan``; read-only is prompt guidance, not enforcement."""
    agents = path / ".nexus" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "plan.md").write_text(
        "---\nname: plan\ndescription: planner\ncontexts: [root]\nbundles: [fs, task, ext]\n---\n"
        "You are a read-only planning agent.\n",
        encoding="utf-8",
    )


async def test_agent_select_replay_reopen_fork_and_reset(tmp_path):
    runtime = await _runtime(tmp_path)
    facade = HostFacade(runtime)
    facade.open_session("s")

    current = await facade.handle(p.AgentCurrent(session="s"))
    # A legacy ``general`` config resolves to the built-in root agent.
    assert (current.name, current.source) == ("build", "config")
    selected = await facade.handle(p.AgentSelect(session="s", name="plan"))
    assert isinstance(selected, p.AgentSelectResult)
    assert selected.apply_next_turn and selected.name == "plan"
    handle = runtime.session("s")
    assert [e.type for e in handle.events].count("agent.selected") == 1
    assert handle.agent_selection.name == "plan"

    runtime.session("s").append_message(Message(role="user", content=[Text(text="Keep this selection")]))
    fork = runtime.sessions.fork("s", new_id="branch")
    assert fork.agent_selection.name == "plan"
    await runtime.aclose()

    reopened = await _runtime(tmp_path)
    assert reopened.session("s").agent_selection.name == "plan"
    reset = await HostFacade(reopened).handle(p.AgentReset(session="s"))
    assert reset.name == "build" and reset.source == "config"
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
    assert "Enter queues while working" in commands.help_text()
    assert "Ctrl+Enter steers" in commands.help_text()
    assert "Alt+Enter interrupts" in commands.help_text()
    assert "Shift+Enter inserts a line" in commands.help_text()
    assert "Ctrl+S submits" not in commands.help_text()


async def test_selected_agent_applies_next_turn_and_plan_is_not_enforced_read_only(tmp_path):
    provider = ScriptedProvider(text_response("planned"))
    _write_plan_agent(tmp_path)
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
    assert started.data["agent"]["read_only"] is False
    assert {"read", "write", "edit"} <= set(started.data["agent"]["tools"])
    assert {"read", "write", "edit"} <= ({tool.name for tool in provider.requests[0].tools} - CHILD_ONLY)
    assert "read-only planning agent" in provider.requests[0].system
    await runtime.aclose()


async def test_default_root_catalog_advertises_canonical_tools(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    [event async for event in runtime.session("catalog").send("inspect")]

    assert ({tool.name for tool in provider.requests[0].tools} - CHILD_ONLY) == {
        "read", "glob", "grep", "edit", "write", "bash", "apply_patch",
        "subagent", "subagent_send", "subagent_stop", "subagent_list",
        "todowrite", "question", "webfetch", "websearch", "skill", "code",
    }
    assert all(tool.name == tool.name.lower() for tool in provider.requests[0].tools)
    rows = await runtime.list_tools()
    search = next(row for row in rows if row["name"] == "websearch")
    assert search.get("availability", "available") == "available"
    assert search["input_schema"]["required"] == ["query"]
    await runtime.aclose()


async def test_disabled_fetch_is_not_advertised_and_has_host_reason(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(
        tmp_path,
        config=_config(web=WebSection(fetch_enabled=False)),
        providers={"scripted": provider},
    )
    [event async for event in runtime.session("fetch-disabled").send("inspect")]
    assert "webfetch" not in ({tool.name for tool in provider.requests[0].tools} - CHILD_ONLY)
    row = next(row for row in await runtime.list_tools() if row["name"] == "webfetch")
    assert row["availability"] == "unavailable"
    assert "fetch_enabled" in row["reason"]
    await runtime.aclose()


async def test_missing_outbound_service_is_reported_as_unavailable(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    runtime._outbound_http_service = None
    [event async for event in runtime.session("http-unavailable").send("inspect")]
    assert "webfetch" not in ({tool.name for tool in provider.requests[0].tools} - CHILD_ONLY)
    assert "websearch" in ({tool.name for tool in provider.requests[0].tools} - CHILD_ONLY)
    rows = await runtime.list_tools()
    fetch = next(row for row in rows if row["name"] == "webfetch")
    assert fetch["availability"] == "unavailable"
    assert "Outbound HTTP service" in fetch["reason"]
    search = next(row for row in rows if row["name"] == "websearch")
    assert search.get("availability", "available") == "available"
    await runtime.aclose()


async def test_configured_web_bundle_is_selectable_but_children_keep_parent_ceiling(tmp_path):
    from nexus.model.providers.scripted import tool_response

    configured = WebSection(
        searxng_instances=["https://Search.Example/"],
        allowed_origins=["https://search.example"],
    )
    provider = ScriptedProvider(
        tool_response(("root-task", "subagent", {"prompt": "delegate", "subagent_type": "general"})),
        text_response("child report"),
        text_response("root report"),
    )
    runtime = Runtime(tmp_path, config=_config(web=configured, background=False), providers={"scripted": provider})
    [event async for event in runtime.session("web-tree").send("delegate")]

    expected = {
        "read", "glob", "grep", "edit", "write", "bash", "apply_patch",
        "subagent", "todowrite", "question", "webfetch", "websearch", "skill", "code",
    }
    # Only the root session holds `subagent` and `code` (registered per root
    # iteration, never in the manifest a child draws from); the child keeps the rest.
    child = expected - {"subagent", "code"}
    assert [({tool.name for tool in request.tools} - CHILD_ONLY) for request in provider.requests] == [
        expected, child, expected,
    ]
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
        text_response("child report"),
        text_response("root report"),
    )
    runtime = Runtime(
        tmp_path,
        config=_config("general", web=configured, background=False),
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

    expected = {"read", "glob", "grep", "subagent", "todowrite", "question", "webfetch", "websearch", "skill"}
    catalogs = [({tool.name for tool in request.tools} - CHILD_ONLY) for request in provider.requests]
    assert catalogs == [expected, expected - {"subagent"}, expected]
    assert all(not ({"write", "edit", "bash", "apply_patch"} & names) for names in catalogs)
    await runtime.aclose()


async def test_explore_keeps_its_declared_tools_within_the_parent_ceiling(tmp_path):
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
    runtime = Runtime(tmp_path, config=_config(background=False), providers={"scripted": provider})
    [event async for event in runtime.session("explore-ceiling").send("delegate")]
    spawned = next(event for event in runtime.session("explore-ceiling").events if event.type == "agent.spawned")
    # Read-only is prompt guidance only: declared bash/write are kept; the
    # declared root-only `subagent` is dropped.
    assert set(spawned.data["tools"]) == {
        "read", "glob", "grep", "todowrite", "webfetch", "websearch", "bash", "write"}
    assert not ({"bash", "write"} & set(spawned.data["dropped_tools"]))
    assert "subagent" in spawned.data["dropped_tools"]
    assert "websearch" in ({tool.name for tool in provider.requests[1].tools} - CHILD_ONLY)
    await runtime.aclose()


async def test_child_catalog_inherits_parent_authority_minus_subagent(tmp_path):
    from nexus.model.providers.scripted import tool_response

    provider = ScriptedProvider(
        tool_response(("root-task", "subagent", {
            "prompt": "delegate",
            "subagent_type": "general",
        })),
        text_response("child report"),
        text_response("root report"),
    )
    _write_plan_agent(tmp_path)
    runtime = Runtime(tmp_path, config=_config("plan", background=False), providers={"scripted": provider})
    [event async for event in runtime.session("tree").send("delegate")]

    expected = {"read", "glob", "grep", "edit", "write", "subagent", "todowrite", "question", "skill"}
    catalogs = [
        ({tool.name for tool in request.tools} - CHILD_ONLY)
        for request in provider.requests
    ]
    # The model is requested again after a tool result. The child inherits the
    # root catalog minus the root-only `subagent`, with no dropped-tool noise.
    assert catalogs == [expected, expected - {"subagent"}, expected]
    spawned = next(e for e in runtime.session("tree").events if e.type == "agent.spawned")
    assert "subagent" not in spawned.data["tools"]
    assert "subagent" not in spawned.data["dropped_tools"]
    await runtime.aclose()


async def test_explore_declaration_cannot_widen_parent_tool_authority(tmp_path):
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
    runtime = Runtime(tmp_path, config=_config(background=False), providers={"scripted": provider})
    # The root catalog contains the full coding authority; the role's own
    # declarations are kept (nothing is stripped) but never add to it.
    [event async for event in runtime.session("readonly-ceiling").send("delegate")]

    spawned = next(
        event for event in runtime.session("readonly-ceiling").events
        if event.type == "agent.spawned"
    )
    assert set(spawned.data["tools"]) == {
        "read", "glob", "grep", "todowrite", "webfetch", "websearch", "skill",
        "bash", "write",
    }
    assert not ({"edit", "multiedit"} & set(spawned.data["tools"]))
    # A skill activation's declared tools are intersected with this iteration's
    # catalog; declarations by a role never create a catalog entry.
    assert ({tool.name for tool in provider.requests[0].tools} - CHILD_ONLY) == {
        "read", "glob", "grep", "edit", "write", "bash", "apply_patch",
        "subagent", "todowrite", "question", "webfetch", "websearch", "skill", "code",
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
    runtime = Runtime(tmp_path, config=_config(background=False), providers={"scripted": provider})
    [event async for event in runtime.session("skill-ceiling").send("delegate")]

    expected = {
        "read", "glob", "grep", "edit", "write", "bash", "apply_patch",
        "subagent", "todowrite", "question", "webfetch", "websearch", "skill", "code",
    }
    child = expected - {"subagent", "code"}
    assert len(provider.requests) == 4
    assert ({tool.name for tool in provider.requests[1].tools} - CHILD_ONLY) == child
    assert ({tool.name for tool in provider.requests[2].tools} - CHILD_ONLY) == child
    assert ({tool.name for tool in provider.requests[3].tools} - CHILD_ONLY) == expected
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
    assert ({tool.name for tool in provider.requests[1].tools} - CHILD_ONLY) == {"read"}
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
    assert {"read", "grep", "glob", "todowrite", "skill"} <= tools
    assert "subagent" not in tools
    assert not ({"write", "edit", "multiedit", "bash", "BashOutput", "KillShell"} & tools)
    await runtime.aclose()


async def test_root_agent_fallbacks_ride_in_request_metadata(tmp_path):
    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "build.md").write_text(
        "---\nname: build\ndescription: root\ncontexts: [root]\n"
        "fallback: [scripted/backup, other/model]\n---\nBuild.\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(tmp_path, config=_config("build"), providers={"scripted": provider})
    [event async for event in runtime.session("fb").send("go")]
    assert provider.requests[0].metadata["agent_fallback"] == ["scripted/backup", "other/model"]
    await runtime.aclose()


async def test_model_and_effort_are_remembered_per_root_agent(tmp_path):
    _write_plan_agent(tmp_path)
    runtime = Runtime(tmp_path, home=tmp_path / "home", config=_config("build"),
                      providers={"scripted": ScriptedProvider(text_response("ok"))})
    facade = HostFacade(runtime)
    facade.open_session("s")
    await facade.handle(p.ModelSelect(session="s", ref="scripted/build-model"))
    await facade.handle(p.AgentSelect(session="s", name="plan"))
    await facade.handle(p.ModelSelect(session="s", ref="scripted/plan-model"))
    assert runtime.session("s").model_selection.reference == "scripted/plan-model"

    await facade.handle(p.AgentSelect(session="s", name="build"))
    assert runtime.session("s").model_selection.reference == "scripted/build-model"
    await facade.handle(p.AgentSelect(session="s", name="plan"))
    assert runtime.session("s").model_selection.reference == "scripted/plan-model"

    # A second runtime on the same home sees the stored choice (machine state).
    other = Runtime(tmp_path, home=tmp_path / "home", config=_config("build"),
                    providers={"scripted": ScriptedProvider(text_response("ok"))})
    assert other._agent_choices.get("build") == ("scripted/build-model", None)
