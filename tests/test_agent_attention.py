"""A child blocked on approval reports it to the root agent, which decides (plan: attention routing)."""
from __future__ import annotations

import asyncio
import dataclasses
import msgspec

from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from test_phase6_integration import drain, make_config, make_runtime, tool_result_for


async def test_denied_child_tool_is_reported_to_the_root_as_needing_attention(tmp_path):
    config = make_config(mode="allow")
    permissions = msgspec.structs.replace(config.v2.permissions, on_unattended="deny", ask=["bash"])
    config = dataclasses.replace(config, v2=msgspec.structs.replace(config.v2, permissions=permissions))
    target = tmp_path / "blocked.txt"
    provider = ScriptedProvider(
        tool_response(("spawn", "subagent", {"prompt": "run the shell"})),
        tool_response(("shell", "bash", {"command": f"printf x > {target}"})),
        text_response("I could not run it"),
        text_response("parent done"),
    )
    runtime = make_runtime(tmp_path, provider, config)
    session = runtime.session("attn")
    await asyncio.wait_for(drain(session), timeout=20)

    assert not target.exists()
    text = tool_result_for(session, "spawn").content[0].text
    assert "needs attention" in text and ("shell" in text or "bash" in text)
    assert "escalation point" in text
    await runtime.aclose()


async def test_escalate_is_a_child_only_tool_and_reaches_the_root(tmp_path):
    provider = ScriptedProvider(
        tool_response(("spawn", "subagent", {"prompt": "edit the prod config"})),
        tool_response(("esc", "escalate", {"needed": "write access to /etc/prod.conf",
                                           "why": "the file is outside my workspace"})),
        text_response("Stopped: I need write access to /etc/prod.conf."),
        text_response("parent done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("esc")
    events = await asyncio.wait_for(drain(session), timeout=20)

    spawned = next(e for e in events if e.type == "agent.spawned")
    assert "escalate" not in spawned.data["tools"]  # not part of the parent's inherited set
    text = tool_result_for(session, "spawn").content[0].text
    assert "needs attention" in text and "write access to /etc/prod.conf" in text
    assert "the file is outside my workspace" in text
    await runtime.aclose()


async def test_root_agent_cannot_call_escalate(tmp_path):
    provider = ScriptedProvider(
        tool_response(("esc", "escalate", {"needed": "x", "why": "y"})),
        text_response("ok"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("rootesc")
    await asyncio.wait_for(drain(session), timeout=20)
    result = tool_result_for(session, "esc")
    assert result.is_error is True and "Recorded" not in result.content[0].text
    assert "escalation recorded" not in result.content[0].text.lower()
    await runtime.aclose()


async def test_escalate_works_in_ask_mode_without_an_approver(tmp_path):
    config = make_config(mode="ask", allow=["subagent"])
    permissions = msgspec.structs.replace(config.v2.permissions, on_unattended="deny")
    config = dataclasses.replace(config, v2=msgspec.structs.replace(config.v2, permissions=permissions))
    provider = ScriptedProvider(
        tool_response(("spawn", "subagent", {"prompt": "do it"})),
        tool_response(("esc", "escalate", {"needed": "approval to run tests", "why": "bash is gated"})),
        text_response("stopping"),
        text_response("parent done"),
    )
    runtime = make_runtime(tmp_path, provider, config)
    session = runtime.session("askesc")
    await asyncio.wait_for(drain(session), timeout=20)
    assert "approval to run tests" in tool_result_for(session, "spawn").content[0].text
    await runtime.aclose()
