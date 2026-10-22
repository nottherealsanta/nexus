"""``SessionActivity`` after a turn: nothing of a subagent outlives its root turn.

The command and its result keep their wire shape for older clients, but a
session whose turn ended is quiescent: no running children, no pending reports,
and ``wait_seconds`` is zero even when the deprecated
``[agents] oneshot_wait_seconds`` is set, so ``nexus run`` exits at the first
terminal event.
"""

from __future__ import annotations

import dataclasses
import io
from types import SimpleNamespace

import msgspec
from test_phase6_integration import drain, make_config, make_runtime

from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from nexus.ui.cli.run import _follow


async def test_a_session_is_quiescent_once_its_turn_with_a_child_ended(tmp_path):
    base = make_config()
    agents = msgspec.structs.replace(base.v2.agents, oneshot_wait_seconds=900)
    config = dataclasses.replace(base, v2=msgspec.structs.replace(base.v2, agents=agents))
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "work"})),
        text_response("child report"),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, config)
    host = HostFacade(runtime)
    await drain(runtime.session("root"))

    activity = await host.handle(p.SessionActivity(session="root"))
    assert activity == p.SessionActivityResult(session="root")
    decoded = msgspec.json.decode(msgspec.json.encode(activity), type=p.Result)
    assert decoded == activity
    await runtime.aclose()


async def test_a_one_shot_run_stops_following_at_the_first_terminal_event():
    queried: list[str] = []

    class Client:
        async def session_activity(self, session):
            queried.append(session)
            return p.SessionActivityResult(session=session)

    async def events():
        yield SimpleNamespace(type="text")
        yield SimpleNamespace(type="turn.completed")
        raise AssertionError("the run kept reading after a quiescent terminal event")

    err = io.StringIO()
    seen = [event.type async for event in _follow(Client(), "root", events(), err)]
    assert seen == ["text", "turn.completed"] and queried == ["root"]
    assert err.getvalue() == ""
