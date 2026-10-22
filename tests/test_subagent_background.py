"""Subagents are foreground-only: a ``subagent`` call blocks until its child ends.

The child's report is the call's own result, the root turn cannot finish while a
child runs, and cancelling the root turn cancels the child with it. Nothing about
the child outlives the turn: no concurrency slot, no live-cancel entry, no queued
report.
"""

from __future__ import annotations

import asyncio

from test_phase6_integration import drain, make_runtime, tool_result_for, wait_for

from nexus.agents.runner import cancel_child
from nexus.model.providers.scripted import ScriptedProvider, Wait, text_response, tool_response


def _top_level(events, event_type):
    return [
        e for e in events
        if e.type == event_type and not (isinstance(e.data, dict) and e.data.get("agent"))
    ]


async def test_the_call_blocks_and_returns_the_report_as_its_result(tmp_path):
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "survey the repo"})),
        text_response("child findings"),
        text_response("root done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    events = await drain(session)

    result = tool_result_for(session, "t1")
    assert result is not None and result.is_error is False
    assert "child findings" in result.content[0].text

    types = [e.type for e in events]
    [spawned] = [e for e in events if e.type == "agent.spawned"]
    [completed] = [e for e in events if e.type == "agent.completed"]
    # The child ran entirely inside the root turn, before the root's final answer.
    assert types.index("agent.spawned") < types.index("agent.completed") < types.index("turn.completed")
    assert completed.data["status"] == "completed"
    # New logs carry no background-mode fields.
    for key in ("background", "continuable", "run"):
        assert key not in spawned.data and key not in completed.data
    # No report is queued back to the root; the tool result was the delivery.
    assert not [
        e for e in events
        if e.type == "input.queued" and (e.data.get("origin") or {}).get("kind") == "subagent_report"
    ]
    budget = runtime.agent_pool.budget("root")
    assert budget is not None and budget.active == 0
    assert cancel_child(spawned.data["agent"]["id"]) is False
    await runtime.aclose()


async def test_cancelling_the_root_turn_cancels_its_running_child(tmp_path):
    never = asyncio.Event()
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "slow"})),
        [Wait(event=never), *text_response("never sent")],
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await session.start_turn("go")
    await wait_for(lambda: any(e.type == "agent.spawned" for e in session.events))
    [spawned] = [e for e in session.events if e.type == "agent.spawned"]
    agent_id = spawned.data["agent"]["id"]

    session.cancel("user stop")
    await asyncio.wait_for(session.wait_idle(), timeout=10)

    events = list(session.events)
    completed = [e for e in events if e.type == "agent.completed"]
    assert [e.data["status"] for e in completed] == ["cancelled"]
    assert _top_level(events, "turn.cancelled")
    assert not _top_level(events, "turn.completed")
    # The child is gone with its turn: nothing left to cancel and no slot held.
    assert cancel_child(agent_id) is False
    assert runtime.agent_pool.budget("root").active == 0
    await runtime.aclose()


async def test_the_session_budget_window_is_shared_within_a_turn_and_reset_per_turn(tmp_path):
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "one"})),
        text_response("first"),
        text_response("turn one done"),
        tool_response(("t2", "subagent", {"prompt": "two"})),
        text_response("second"),
        text_response("turn two done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)
    budget = runtime.agent_pool.budget("root")
    assert budget.children == 1
    await drain(session)
    # Same session budget object; its fan-out window restarted with the new turn.
    assert runtime.agent_pool.budget("root") is budget
    assert budget.children == 1 and budget.active == 0
    assert "second" in tool_result_for(session, "t2").content[0].text
    await runtime.aclose()
