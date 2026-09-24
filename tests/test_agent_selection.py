"""Root-agent selection durability, eligibility, and turn-time application."""
from __future__ import annotations

import pytest

from nexus.config import Config
from nexus.config.schema import AgentSection, ConfigV2, ModelSection, PermissionsSection
from nexus.errors import ConfigError
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime


def _config(name: str = "general") -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name=name),
            model=ModelSection(default="scripted/m"),
            permissions=PermissionsSection(mode="allow"),
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
    assert "Ctrl+Enter submits" in commands.help_text()
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
    assert not ({"Write", "Edit", "MultiEdit", "Bash", "Task"} & set(started.data["agent"]["tools"]))
    assert "read-only planning agent" in provider.requests[0].system
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
    assert spawned.data["tools"] == ["Read"]
    assert set(spawned.data["dropped_tools"]) >= {"Write", "Bash"}
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
    assert {"Read", "Grep", "Glob", "LS", "Task"} <= tools
    assert not ({"Write", "Edit", "MultiEdit", "Bash", "BashOutput", "KillShell"} & tools)
    await runtime.aclose()
