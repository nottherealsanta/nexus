"""Host ``AgentClose``: kept on the wire; with foreground-only subagents it cancels.

A subagent ends with its run, so closing a running child cancels that child
alone (its parent turn and siblings continue, like ``AgentCancel``), and closing
one that already ended is an idempotent no-op that writes nothing. Legacy idle
children from the retired background mode are covered in
``tests/test_subagent_continue.py``.
"""

from __future__ import annotations

import asyncio

import msgspec
from test_phase6_integration import make_runtime, wait_for

from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, Wait, text_response, tool_response


async def test_close_a_running_child_cancels_only_that_child(tmp_path):
    never = asyncio.Event()
    fast = asyncio.Event()
    provider = ScriptedProvider(
        tool_response(
            ("slow-call", "subagent", {"prompt": "slow"}),
            ("fast-call", "subagent", {"prompt": "fast"}),
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

    result = await host.handle(p.AgentClose(session="root", agent_id=spawned["slow-call"], reason="enough"))
    assert result.closed and "Cancelled subagent" in result.message
    fast.set()
    await running
    await wait_for(lambda: any(e.type == "turn.completed" for e in session.events))

    done = {e.data["agent"]["parent_call_id"]: e.data for e in session.events if e.type == "agent.completed"}
    assert done["slow-call"]["status"] == "cancelled"
    assert done["fast-call"]["status"] == "completed"
    assert not [e for e in session.events if e.type == "agent.stopped"]

    again = await host.handle(p.AgentClose(session="root", agent_id=spawned["slow-call"]))
    assert again.closed and "nothing left to close" in again.message
    missing = await host.handle(p.AgentClose(session="root", agent_id="root/sub/99"))
    assert not missing.closed and "No such agent" in missing.message
    await runtime.aclose()


async def test_agent_close_command_is_wire_serialisable():
    command = p.AgentClose(session="s", agent_id="s/sub/1", reason="done")
    assert msgspec.json.decode(msgspec.json.encode(command), type=p.Command) == command
    result = p.AgentCloseResult(session="s", agent_id="s/sub/1", closed=True, message="ok")
    assert msgspec.json.decode(msgspec.json.encode(result), type=p.Result) == result
    assert p.AgentCloseResult(session="s", agent_id="a").closed is False


async def test_client_close_agent_sends_the_command():
    from nexus.client.protocol import Client

    sent = []

    class Fake(Client):
        def __init__(self) -> None:  # no transport needed for this probe
            pass

        async def _request(self, command):
            sent.append(command)
            return p.AgentCloseResult(session=command.session, agent_id=command.agent_id, closed=True)

    result = await Fake().close_agent("s", "s/sub/1", reason="why")
    assert sent == [p.AgentClose(session="s", agent_id="s/sub/1", reason="why")] and result.closed
