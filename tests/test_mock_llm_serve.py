"""Regression coverage for the deterministic real-runtime mock LLM harness."""

from __future__ import annotations

import asyncio
from contextlib import aclosing

import pytest
from mock_llm_serve import (
    CHILD_AGENT_ID,
    CHILD_TRANSCRIPT,
    FINAL_RESPONSE,
    FIXTURE_NAME,
    SESSION_ID,
    USER_PROMPT,
    build_runtime,
)

from nexus.host import HostFacade


@pytest.mark.asyncio
async def test_real_runtime_mock_model_tools_and_task_activity(tmp_path):
    runtime, provider, gate = build_runtime(tmp_path)
    facade = HostFacade(runtime)
    facade.open_session(SESSION_ID)
    events = facade.subscribe(SESSION_ID, from_seq=0, follow=True, client_id="pytest")

    try:
        async with aclosing(events) as stream:
            # Attaching the real facade subscription makes this an attended view.
            await asyncio.wait_for(stream.__anext__(), timeout=3)
            turn_id = await facade.start_turn(SESSION_ID, USER_PROMPT)
            assert turn_id

            # The child model waits only after its real child Read completes.
            await asyncio.wait_for(gate.pending.wait(), timeout=12)
            pending, _ = facade.state(SESSION_ID)
            assert any(message.role == "user" for message in pending.messages)
            assert (tmp_path / FIXTURE_NAME).read_text(encoding="utf-8") == "alpha\nnexus\n"

            parent_tools = {tool.call_id: tool for tool in pending.tools}
            assert parent_tools["root-read"].status == "completed"
            assert parent_tools["root-edit"].status == "completed"
            assert parent_tools["root-read-error"].is_error is True
            assert "not found" in (parent_tools["root-read-error"].display or "").casefold()
            assert parent_tools["root-task"].status == "running"
            assert pending.agents[CHILD_AGENT_ID].status == "spawned"

            child = pending.agents[CHILD_AGENT_ID]
            child_tools = {tool.call_id: tool for tool in child.body.tools}
            assert child.status == "spawned"
            assert child_tools["child-read"].status == "completed"
            assert child.body.messages
            assert any(message.role == "assistant" for message in child.body.messages)

            gate.release.set()
            await asyncio.wait_for(facade.wait_idle(timeout=15), timeout=18)

        complete, _ = facade.state(SESSION_ID)
        assert complete.messages[-1].text == FINAL_RESPONSE
        assert complete.agents[CHILD_AGENT_ID].status == "completed"
        assert any(message.text == CHILD_TRANSCRIPT for message in complete.agents[CHILD_AGENT_ID].body.messages)
        assert any(
            message.thinking == "I will inspect the fixture before changing it."
            for message in complete.agents[CHILD_AGENT_ID].body.messages
        )
        task = next(tool for tool in complete.tools if tool.call_id == "root-task")
        assert task.status == "completed"
        assert task.child_agent_ids == [CHILD_AGENT_ID]
        child = complete.agents[CHILD_AGENT_ID]
        assert child.spawned_ts is not None and child.completed_ts is not None
        assert child.completed_ts >= child.spawned_ts
        assert provider.calls == 7
    finally:
        gate.release.set()
        await runtime.aclose()


def test_mock_app_and_browser_driver_are_test_only_and_never_claim_real_model_identity():
    from pathlib import Path

    source = Path(__file__).with_name("mock_llm_serve.py").read_text(encoding="utf-8")
    assert "nexus-e2e-model" in source
    assert "GPT-6" not in source
    assert "ANTHROPIC_API_KEY" not in source
    assert "OPENAI_API_KEY" not in source
