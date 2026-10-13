"""Host ``AgentCancel``: stop one child agent without stopping its parent or siblings."""

from __future__ import annotations

import asyncio

from test_phase6_integration import make_runtime, wait_for

from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, Wait, text_response, tool_response


async def test_agent_cancel_stops_only_that_child(tmp_path):
    never = asyncio.Event()
    fast = asyncio.Event()
    provider = ScriptedProvider(
        tool_response(
            ("slow-call", "Task", {"prompt": "slow", "subagent_type": "general"}),
            ("fast-call", "Task", {"prompt": "fast", "subagent_type": "general"}),
        ),
        [Wait(event=never), *text_response("slow report")],
        [Wait(event=fast), *text_response("fast report")],
        text_response("root done"),
    )
    runtime = make_runtime(tmp_path, provider)
    host = HostFacade(runtime)
    session = runtime.session("root")
    running = asyncio.create_task(session.send("go").__anext__())
    await wait_for(lambda: len([e for e in session.events if e.type == "agent.spawned"]) == 2)
    spawned = {e.data["agent"]["parent_call_id"]: e.data["agent"]["id"] for e in session.events
               if e.type == "agent.spawned"}

    missing = await host.handle(p.AgentCancel(session="root", agent_id="root/sub/99"))
    assert not missing.cancelled and "No such agent" in missing.message

    result = await host.handle(p.AgentCancel(session="root", agent_id=spawned["slow-call"], reason="not needed"))
    assert result.cancelled and "this agent only" in result.message
    fast.set()
    await running
    await wait_for(lambda: any(e.type == "turn.completed" for e in session.events))

    done = {e.data["agent"]["parent_call_id"]: e.data for e in session.events if e.type == "agent.completed"}
    assert done["slow-call"]["status"] == "cancelled"
    assert done["fast-call"]["status"] == "completed"
    assert not any(e.type in {"turn.cancelled", "turn.failed"} for e in session.events if not e.data.get("agent"))

    again = await host.handle(p.AgentCancel(session="root", agent_id=spawned["slow-call"]))
    assert not again.cancelled and "not running" in again.message
    await runtime.aclose()


async def test_agent_cancel_command_is_wire_serialisable():
    import msgspec

    command = p.AgentCancel(session="s", agent_id="s/sub/1")
    decoded = msgspec.json.decode(msgspec.json.encode(command), type=p.Command)
    assert decoded == command
