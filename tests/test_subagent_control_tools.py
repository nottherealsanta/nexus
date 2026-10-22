"""The retired ``subagent_send`` / ``subagent_stop`` / ``subagent_list`` tools stay gone.

``subagent`` is the only subagent tool: it blocks, so there is nothing to
continue, stop or list afterwards. The runner exposes only ``spawn``, a child
can never hold ``subagent``, and the root model is never offered the retired
names or a background/wait switch, even under a legacy ``background = true``.
"""

from __future__ import annotations

import dataclasses

import msgspec
import pytest
from test_phase6_integration import drain, make_config, make_runtime
from test_subagent_runner import Factory, make_runner

from nexus.agents.runner import ROOT_ONLY_TOOLS, SubagentRunner
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response

RETIRED = {"subagent_send", "subagent_stop", "subagent_list"}


def test_subagent_is_the_only_root_only_tool():
    assert ROOT_ONLY_TOOLS == {"subagent"}


def test_the_runner_has_no_background_or_continuation_api(tmp_path):
    runner = make_runner(tmp_path, Factory())
    for name in ("start", "resume", "stop", "list_agents", "persistent", "wake_paused",
                 "agent_permission_key", "max_followup_chars"):
        assert not hasattr(runner, name), name
    assert callable(runner.spawn)
    for legacy in ({"pool": object()}, {"persistent": True}, {"continuation_blocker": None}):
        with pytest.raises(TypeError):
            make_runner(tmp_path, Factory(), **legacy)
    assert isinstance(runner, SubagentRunner)


def test_a_child_never_gets_subagent_even_when_requested(tmp_path):
    runner = make_runner(tmp_path, Factory(), parent_tools={"read", "subagent"})
    role = runner.agents.resolve("task", context="subagent")
    selection = runner.select_tools(role, {"prompt": "x", "tools": ["read", "subagent"]})
    assert "subagent" not in selection.selected and "read" in selection.selected


async def test_the_root_model_is_offered_only_the_blocking_tool(tmp_path):
    base = make_config()
    legacy = msgspec.structs.replace(base.v2.agents, background=True)
    config = dataclasses.replace(base, v2=msgspec.structs.replace(base.v2, agents=legacy))
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "x"})),
        text_response("child"),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, config)
    await drain(runtime.session("root"))

    root_tools = {tool.name: tool for tool in provider.requests[0].tools}
    assert "subagent" in root_tools and not RETIRED & set(root_tools)
    schema = root_tools["subagent"].input_schema
    assert not {"background", "wait"} & set(schema.get("properties", {}))
    child_tools = {tool.name for tool in provider.requests[1].tools}
    assert not ({"subagent"} | RETIRED) & child_tools
    await runtime.aclose()
