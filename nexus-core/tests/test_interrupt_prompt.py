"""`POST /api/session/{id}/prompt {mode: "interrupt"}`: cancel the running turn and run this message first (plan 25)."""

# ruff: noqa: F811
from __future__ import annotations

import asyncio

from test_archive import _create, _idle, env  # noqa: F401  (env is a fixture)
from test_inputs_integration import (  # noqa: F401  (setup_inputs is a fixture)
    _inputs,
    _session,
    _user_texts,
    _wait_until,
    setup_inputs,
)

from nexus.llmhub.types import ToolCall


async def test_keyed_interrupt_stops_a_streaming_turn(setup_inputs):
    runtime, client, provider, workspace = setup_inputs
    session_id = await _session(client, workspace)
    provider.pause_before_done = True
    await client.post(f"/api/session/{session_id}/prompt", json={"text": "long"})
    await asyncio.wait_for(provider.entered.wait(), 3)
    response = await asyncio.wait_for(client.post(
        f"/api/session/{session_id}/interrupt", headers={"idempotency-key": "stop-test"}), 1)
    assert response.status_code == 204
    assert session_id not in runtime.turns


async def test_interrupt_mode_cancels_a_waiting_approval_and_runs_the_message(env):
    rt, client, provider, workspace = env.rt, env.client, env.provider, env.workspace
    root = await _create(client, workspace, permission="ask")
    provider.replies = [[ToolCall("b1", "bash", '{"command": "true"}')]]
    await client.post(f"/api/session/{root['id']}/prompt", json={"text": "run"})
    for _ in range(200):
        if rt.pending_approvals(root["id"]):
            break
        await asyncio.sleep(0.01)
    assert rt.pending_approvals(root["id"])
    sent = await client.post(f"/api/session/{root['id']}/prompt", json={"text": "stop that", "mode": "interrupt"})
    assert sent.status_code == 202, sent.text
    assert sent.json()["interrupted"] is True
    replied = [e["data"] for e in rt.store.events(root["id"], limit=5000) if e["type"] == "permission.replied"]
    assert replied and replied[0]["allow"] is False
    await _idle(rt, root["id"])


async def test_interrupt_mode_cancels_turn_and_runs_first_then_the_queue(setup_inputs):
    runtime, client, provider, workspace = setup_inputs
    session_id = await _session(client, workspace)
    provider.pause_before_done = True
    await client.post(f"/api/session/{session_id}/prompt", json={"text": "long"})
    await asyncio.wait_for(provider.entered.wait(), 3)
    queued = await client.post(f"/api/session/{session_id}/prompt", json={"text": "later", "mode": "queue"})
    assert queued.status_code == 202
    sent = await client.post(f"/api/session/{session_id}/prompt", json={"text": "now", "mode": "interrupt"})
    assert sent.status_code == 202, sent.text
    assert sent.json()["interrupted"] is True and sent.json()["mode"] == "interrupt"
    provider.open_gate()
    await _wait_until(lambda: session_id not in runtime.turns and len(provider.requests) >= 3, "queue did not run after")
    assert _user_texts(provider.requests[1])[-1] == "now"
    assert _user_texts(provider.requests[2])[-1] == "later"
    state = await _inputs(client, session_id)
    assert state["inputs"] == [] and state["held"] is False


async def test_interrupt_mode_when_idle_behaves_like_send(setup_inputs):
    runtime, client, provider, workspace = setup_inputs
    session_id = await _session(client, workspace)
    sent = await client.post(f"/api/session/{session_id}/prompt", json={"text": "hi", "mode": "interrupt"})
    assert sent.status_code == 202, sent.text
    assert sent.json()["origin"] == "prompt"
    await _wait_until(lambda: session_id not in runtime.turns and provider.requests, "idle interrupt did not run")
    assert _user_texts(provider.requests[0])[-1] == "hi"


async def test_interrupt_mode_empty_text_keeps_the_running_turn(setup_inputs):
    runtime, client, provider, workspace = setup_inputs
    session_id = await _session(client, workspace)
    provider.pause_before_done = True
    await client.post(f"/api/session/{session_id}/prompt", json={"text": "long"})
    await asyncio.wait_for(provider.entered.wait(), 3)
    bad = await client.post(f"/api/session/{session_id}/prompt", json={"text": " ", "mode": "interrupt"})
    assert bad.status_code == 400
    assert session_id in runtime.turns
    provider.open_gate()
    await _wait_until(lambda: session_id not in runtime.turns, "turn did not finish")


async def test_interrupt_mode_racing_the_turn_finishing_never_409s(setup_inputs):
    runtime, client, provider, workspace = setup_inputs
    session_id = await _session(client, workspace)
    provider.pause_before_done = True
    await client.post(f"/api/session/{session_id}/prompt", json={"text": "long"})
    await asyncio.wait_for(provider.entered.wait(), 3)
    await client.post(f"/api/session/{session_id}/prompt", json={"text": "later", "mode": "queue"})
    provider.open_gate()  # the turn ends on its own while the interrupt is sent
    sent = await client.post(f"/api/session/{session_id}/prompt", json={"text": "now", "mode": "interrupt"})
    assert sent.status_code == 202, sent.text
    await _wait_until(lambda: session_id not in runtime.turns and len(provider.requests) >= 3, "turns did not drain")
    assert _user_texts(provider.requests[1])[-1] == "now"
    assert _user_texts(provider.requests[2])[-1] == "later"
