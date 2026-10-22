"""A subagent's report reaches the root only as the result of its own call.

There is no second delivery path: no host report sink, no ``subagent_report``
input, no ``agent.reported``-style event. The model-facing text never points at
the retired continuation tools.
"""

from __future__ import annotations

from test_phase6_integration import drain, make_runtime, tool_result_for

from nexus.agents import SubagentOutcome
from nexus.agents.pool import AgentPool
from nexus.host import HostFacade
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response


def test_a_failed_childs_handoff_says_to_start_a_new_subagent():
    outcome = SubagentOutcome(
        agent="task", session_id="root/sub/1", status="failed", is_error=True,
        stop_reason="max_iterations", handoff="## What was done\n- half of it",
        handoff_path="/tmp/handoff.md",
    )
    text = outcome.render()
    assert "start a new subagent and put what it needs from this report in its prompt" in text
    assert "## What was done" in text
    assert "subagent_send" not in text and "subagent_stop" not in text
    assert "continuable" not in outcome.to_dict()


def test_the_pool_holds_budgets_only():
    pool = AgentPool()
    for name in ("report_sink", "root_active", "report_backlog", "root", "get"):
        assert not hasattr(pool, name)
    assert pool.budget("root") is None and pool.discard_if_idle("root") is True


async def test_the_host_installs_no_report_path_and_queues_nothing(tmp_path):
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "look"}), ("t2", "subagent", {"prompt": "look again"})),
        text_response("found it"),
        text_response("also found it"),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    host = HostFacade(runtime)
    assert not hasattr(host, "deliver_report")
    session = runtime.session("root")
    events = await drain(session)

    for call in ("t1", "t2"):
        result = tool_result_for(session, call)
        assert result is not None and "found it" in result.content[0].text
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        origin = data.get("origin") if isinstance(data.get("origin"), dict) else {}
        assert not (event.type == "input.queued" and origin.get("kind") == "subagent_report")
        assert not (event.type == "context.notice" and data.get("kind") == "subagent_wake_paused")
        assert event.type not in {"agent.resumed", "agent.stopped"}
    # Each report appears once in the root conversation: as its tool result.
    user_texts = [
        block.text for message in session.messages if message.role == "user"
        for block in message.content if type(block).__name__ == "Text"
    ]
    assert not any("Subagent report" in text for text in user_texts)
    await runtime.aclose()
