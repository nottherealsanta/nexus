"""Host ``AgentClose``: close one child agent for good (``agent.stopped``).

Modeled on ``tests/test_host_agent_cancel.py``: where cancel ends a run and leaves the
child idle, close cancels a run in flight if there is one and then makes the child
terminal, freeing its live slot.
"""

from __future__ import annotations

import asyncio

import msgspec
from test_phase6_integration import wait_for
from test_subagent_background import SPAWN, Routed, _config

from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, Wait, text_response, tool_response
from nexus.runtime import Runtime
from nexus.view import fold


def _runtime(tmp_path, provider):
    return Runtime(tmp_path, config=_config(), providers={"scripted": provider})


async def test_close_an_idle_child_records_agent_stopped_and_frees_the_slot(tmp_path):
    provider = Routed([tool_response(SPAWN), text_response("done")], [text_response("child report")])
    runtime = _runtime(tmp_path, provider)
    host = HostFacade(runtime)
    session = runtime.session("root")
    [event async for event in session.send("go")]
    [record] = list(runtime.agent_pool.get("root").records.values())
    await asyncio.wait_for(record.task, 10)
    budget = runtime.agent_pool.get("root").budget
    assert record.state == "idle" and budget.live == 1

    missing = await host.handle(p.AgentClose(session="root", agent_id="root/sub/99"))
    assert not missing.closed and "No such agent" in missing.message

    result = await host.handle(p.AgentClose(session="root", agent_id=record.agent_id, reason="finished"))
    assert result.closed and "Stopped subagent" in result.message
    assert record.state == "stopped" and budget.live == 0
    [stopped] = [e for e in session.events if e.type == "agent.stopped"]
    assert stopped.data["by"] == "user" and stopped.data["reason"] == "finished"
    agent = fold(session.events).agents[record.agent_id]
    assert agent.stopped and agent.stopped_by == "user" and agent.stopped_reason == "finished"

    again = await host.handle(p.AgentClose(session="root", agent_id=record.agent_id))
    assert again.closed and "already stopped" in again.message
    assert len([e for e in session.events if e.type == "agent.stopped"]) == 1
    cancel = await host.handle(p.AgentCancel(session="root", agent_id=record.agent_id))
    assert not cancel.cancelled and "AgentClose" in cancel.message
    await runtime.aclose()


async def test_close_a_running_child_cancels_it_first_and_does_not_report_it(tmp_path):
    never = asyncio.Event()
    provider = Routed([tool_response(SPAWN), text_response("done")], [])
    provider.child = ScriptedProvider([Wait(never), *text_response("never")])
    runtime = _runtime(tmp_path, provider)
    host = HostFacade(runtime)
    session = runtime.session("root")
    [event async for event in session.send("go")]
    [record] = list(runtime.agent_pool.get("root").records.values())
    await wait_for(lambda: record.state == "running")

    result = await host.handle(p.AgentClose(session="root", agent_id=record.agent_id))
    assert result.closed and "Cancelled and stopped" in result.message
    assert record.state == "stopped" and record.task.done()
    types = [e.type for e in session.events]
    assert types.index("agent.completed") < types.index("agent.stopped")
    completed = next(e for e in session.events if e.type == "agent.completed")
    assert completed.data["status"] == "cancelled"
    # The root asked for the stop: no report of the cancelled run is queued for it.
    assert not [e for e in session.events if e.type == "input.queued"]
    await runtime.aclose()


async def test_close_after_a_restart_rebuilds_the_child_from_the_log(tmp_path):
    provider = Routed([tool_response(SPAWN), text_response("done")], [text_response("child report")])
    runtime = _runtime(tmp_path, provider)
    session = runtime.session("root")
    [event async for event in session.send("go")]
    [record] = list(runtime.agent_pool.get("root").records.values())
    await asyncio.wait_for(record.task, 10)
    agent_id = record.agent_id
    await runtime.aclose()

    restarted = _runtime(tmp_path, ScriptedProvider(text_response("x")))
    host = HostFacade(restarted)
    assert restarted.agent_pool.get("root") is None
    result = await host.handle(p.AgentClose(session="root", agent_id=agent_id))
    assert result.closed
    events = restarted.session("root").events
    assert [e.data["by"] for e in events if e.type == "agent.stopped"] == ["user"]
    assert restarted.agent_pool.get("root").get(agent_id).state == "stopped"
    await restarted.aclose()


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
