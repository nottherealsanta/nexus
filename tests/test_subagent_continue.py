"""Sessions written by the retired background mode stay readable and inert.

A root log can hold a background child that went idle, was continued
(``agent.resumed``) and never stopped. Reopening it in a foreground-only host
must still fold the transcript, but the child holds no concurrency slot, no
report is rebuilt or queued, nothing wakes the root, and ``AgentCancel`` /
``AgentClose`` answer without writing to the log.
"""

from __future__ import annotations

from test_phase6_integration import drain, make_config, make_runtime, tool_result_for

from nexus.events import Event
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.message import Message, Text
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from nexus.view import fold

LEGACY = "root/sub/7"
META = {
    "id": LEGACY, "agent_id": LEGACY, "parent": "root", "parent_session": "root",
    "root_id": "root", "parent_agent_id": None, "parent_call_id": "old-call",
    "root_turn_id": "old-turn", "depth": 1, "type": "task", "task": "old-task",
    "tier": "medium", "index": 7, "session": LEGACY,
}


def _legacy_events() -> list[Event]:
    def agent(event_type: str, **data) -> Event:
        return Event(type=event_type, session="root", data={"agent": dict(META), **META, **data})

    return [
        agent("agent.spawned", description="old work", model="medium", tools=["read"],
              dropped_tools=[], clamped=False, background=True, continuable=True, worktree=None),
        agent("agent.completed", run=1, continuable=True, status="completed", ok=True,
              is_error=False, usage={"input_tokens": 3}, dropped_tools=[], clamped=False),
        agent("agent.resumed", run=2, message="one more thing", message_chars=14,
              background=True, continuable=True),
        agent("agent.completed", run=2, continuable=True, status="completed", ok=True,
              is_error=False, usage={"input_tokens": 2}, dropped_tools=[], clamped=False),
    ]


async def _seed_legacy_root(tmp_path) -> int:
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("unused")))
    session = runtime.session("root")
    session.append_message(Message(role="user", content=[Text(text="delegate old work")]))
    for event in _legacy_events():
        session.append_event(event)
    count = len(session.events)
    await runtime.aclose()
    return count


async def test_reopening_folds_the_legacy_child_and_replays_nothing(tmp_path):
    seeded = await _seed_legacy_root(tmp_path)
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("unused")))
    host = HostFacade(runtime)

    activity = await host.handle(p.SessionActivity(session="root"))
    assert (activity.turn_active, activity.queued, activity.running_agents,
            activity.reports_pending, activity.wait_seconds) == (False, 0, 0, 0, 0)
    session = runtime.session("root")
    assert len(session.events) == seeded            # no rebuilt report, no wake
    agent = fold(session.events).agents[LEGACY]
    assert agent.runs == 2 and agent.status == "completed"
    transcript = host.agent_transcript("root", LEGACY)
    assert transcript["found"] is True
    await runtime.aclose()


async def test_agent_cancel_and_close_answer_without_writing(tmp_path):
    seeded = await _seed_legacy_root(tmp_path)
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("unused")))
    host = HostFacade(runtime)

    cancel = await host.handle(p.AgentCancel(session="root", agent_id=LEGACY))
    assert not cancel.cancelled and "not running" in cancel.message
    close = await host.handle(p.AgentClose(session="root", agent_id=LEGACY, reason="done"))
    assert close.closed and "nothing left to close" in close.message
    again = await host.handle(p.AgentClose(session="root", agent_id=LEGACY))
    assert again.closed
    missing = await host.handle(p.AgentClose(session="root", agent_id="root/sub/99"))
    assert not missing.closed and "No such agent" in missing.message
    events = runtime.session("root").events
    assert len(events) == seeded and not [e for e in events if e.type == "agent.stopped"]
    await runtime.aclose()


async def test_a_legacy_idle_child_holds_no_slot(tmp_path):
    await _seed_legacy_root(tmp_path)
    provider = ScriptedProvider(
        tool_response(("fresh", "subagent", {"prompt": "new work"})),
        text_response("fresh report"),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, make_config(max_concurrent=1, max_fanout=1))
    session = runtime.session("root")
    await drain(session)
    result = tool_result_for(session, "fresh")
    assert result is not None and not result.is_error and "fresh report" in result.content[0].text
    await runtime.aclose()


async def test_reopening_session_with_queued_legacy_report_does_not_wake_root(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("unused")))
    session = runtime.session("root")
    session.append_message(Message(role="user", content=[Text(text="delegate old work")]))
    for event in _legacy_events():
        session.append_event(event)
    session.append_event(Event(type="input.queued", session="root", data={
        "queued_id": "q1",
        "content": [{"type": "text", "text": "legacy report"}],
        "mode": "steer",
        "origin": {
            "kind": "subagent_report",
            "agent": LEGACY,
            "run": 2,
            "status": "completed",
            "agents": [f"{LEGACY}:2"],
        },
    }))
    seeded = len(session.events)
    await runtime.aclose()

    provider = ScriptedProvider(text_response("never"))
    runtime2 = make_runtime(tmp_path, provider)
    host = HostFacade(runtime2)

    activity = await host.handle(p.SessionActivity(session="root"))
    assert (activity.turn_active, activity.running_agents,
            activity.reports_pending, activity.wait_seconds) == (False, 0, 0, 0)
    assert activity.queued == 1

    session2 = runtime2.session("root")
    assert not session2.active
    assert len(session2.events) == seeded
    assert provider.requests == []

    await runtime2.aclose()
