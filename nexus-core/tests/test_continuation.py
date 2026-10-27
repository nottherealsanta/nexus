"""Manual continuation from stored context (PLAN.md Step 11).

Every test is offline and uses a scripted provider that records the exact
`Request` it receives. The crash-window tests write the durable rows a crash
would leave behind and then start a fresh runtime, so no live provider or killed
process is needed.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest

from nexus import cli
from nexus import runtime as runtime_module
from nexus.bus import Bus
from nexus.codec import message_to_json, to_json
from nexus.continuation import reconcile, unresolved_calls
from nexus.llmhub.types import Done, Message, PartDone, ProviderError, Text, ToolCall, ToolResult
from nexus.runtime import Runtime, message_json
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"
CONTINUATION_NOTICE = "[Nexus recovery: continuation reconciled interrupted tool calls]"


class Scripted:
    name = "scripted"

    def __init__(self, script=None):
        self.script = list(script or [])
        self.requests = []
        self.gates = []

    def body(self, req):
        return {"model": req.model, "messages": len(req.messages), "instructions": req.instructions,
                "tools": [t.name for t in req.tools], "effort": req.reasoning_effort}

    async def stream(self, req):
        self.requests.append(req)
        gate = self.gates.pop(0) if self.gates else None
        if gate is not None:
            await gate.wait()
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        parts = item
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


class Env:
    def __init__(self, rt, client, provider, workspace, store, tmp_path, app):
        self.rt, self.client, self.provider, self.workspace = rt, client, provider, workspace
        self.store, self.tmp_path, self.app = store, tmp_path, app

    def __iter__(self):
        return iter((self.rt, self.client, self.provider, self.workspace))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    store = Store(tmp_path / "nexus.db")
    rt = make_runtime("scripted/m", store)
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    return Env(rt, client, provider, workspace, store, tmp_path, app)


async def _idle(rt, session_id):
    for _ in range(500):
        if session_id not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"still running: {list(rt.turns)}")


async def _create(client, workspace, **fields):
    response = await client.post("/api/session", json={"directory": str(workspace), **fields})
    assert response.status_code == 200, response.text
    return response.json()


async def _child(client, parent, **fields):
    response = await client.post("/api/session", json={"parent": parent, **fields})
    assert response.status_code == 200, response.text
    return response.json()


async def _messages(client, session_id):
    response = await client.get(f"/api/session/{session_id}/message")
    assert response.status_code == 200, response.text
    return response.json()


async def _events(client, session_id):
    return (await client.get(f"/api/session/{session_id}/event", params={"limit": 5000})).json()["events"]


def _request_messages(request):
    return [message_to_json(message) for message in request.messages]


def _texts(messages):
    return [part.get("text") for message in messages for part in message["parts"]
            if part["type"] == "text" and part.get("text")]


def _cli_client(app):
    client = cli.Client("http://test", TOKEN)
    client.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                    headers={"authorization": f"Bearer {TOKEN}"})
    return client


def _seed_crash(db: Path, workspace: Path, *, continuation=False, started_tool=False):
    """Write the durable state of a crash, optionally during a continuation."""
    store = Store(db)
    bus = Bus()
    bus.attach(store)
    project, _ = store.project_for_path(str(workspace))
    session = store.create_session(project, title="crashed", agent="build")
    user = store.append_message(session.id, Message("user", (Text("run it"),)))
    event = bus.publish("message.added", message_json(user), session_id=session.id)
    store.link_message_event(user.id, event["id"])

    previous = store.start_turn(session.id, data={"origin": "prompt"})
    previous_request = store.start_request(previous["id"], session.id, 1)
    restored = store.save_assistant_message(
        session.id, Message("assistant", (Text("first answer"),)), turn_id=previous["id"],
        request_id=previous_request["id"])
    event = bus.publish("message.added", message_json(restored), session_id=session.id)
    store.link_message_event(restored.id, event["id"])
    store.finish_request(previous_request["id"], stop="end_turn", response_id="resp_1",
                         parts=[to_json(Text("first answer"))])
    store.finish_turn(previous["id"], "completed", steps=1)
    store.set_turn_recovery(previous["id"], "none")

    data = {"origin": "prompt"}
    if continuation:
        data = {"origin": "continuation", "continuation": {"predecessor": {
            "turn_id": previous["id"], "request_id": previous_request["id"], "status": "completed",
            "request_status": "completed", "step": 1, "link": "request",
        }}}
    turn = store.start_turn(session.id, data=data)
    request = store.start_request(turn["id"], session.id, 1)
    if started_tool:
        call = ToolCall("c1", "bash", '{"command": "touch crashed.txt"}')
        store.save_assistant_message(
            session.id, Message("assistant", (call,)), turn_id=turn["id"], request_id=request["id"])
        store.mark_tool_started(turn["id"], session.id, call)
    bus.publish("text_delta", {"type": "text_delta", "text": "partial"}, session_id=session.id)
    store.close()
    return session.id, turn["id"], previous["id"], request["id"]


def _seed_dangling(db: Path, workspace: Path, *, legacy: bool):
    store = Store(db)
    bus = Bus()
    bus.attach(store)
    project, _ = store.project_for_path(str(workspace))
    session = store.create_session(project, title="dangling", agent="build")
    store.append_message(session.id, Message("user", (Text("go"),)))
    call = ToolCall("c1", "bash", '{"command": "touch dangling.txt"}')
    if legacy:
        store.append_message(session.id, Message("assistant", (call,)))
    else:
        turn = store.start_turn(session.id, data={"origin": "prompt"})
        store.save_assistant_message(session.id, Message("assistant", (call,)), turn_id=turn["id"])
        # Simulate a completed turn whose call record survived without a result.
        store.finish_turn(turn["id"], "completed", steps=1)
        store.set_turn_recovery(turn["id"], "none")
    store.close()
    return session.id


# -- the first request --------------------------------------------------------


async def test_continuation_sends_the_stored_context_unchanged(env):
    rt, client, provider, workspace = env
    session = await _create(client, workspace)
    provider.script = [[Text("first answer")]]
    assert (await client.post(f"/api/session/{session['id']}/prompt", json={"text": "hello"})).status_code == 202
    await _idle(rt, session["id"])
    first = provider.requests[0]
    stored = await _messages(client, session["id"])

    provider.script = [[Text("second answer")]]
    seen = len(provider.requests)
    response = await client.post(f"/api/session/{session['id']}/continue", json={})
    assert response.status_code == 202, response.text
    started = response.json()
    assert started["origin"] == "continuation" and started["status"] == "busy"
    await _idle(rt, session["id"])

    request = provider.requests[seen]
    assert _request_messages(request) == [{"role": m["role"], "parts": m["parts"]} for m in stored]
    assert not any("continue" in text.lower() for text in _texts(_request_messages(request)))
    assert request.instructions == first.instructions
    assert (request.model, request.reasoning_effort, request.conversation_id) == (
        first.model, first.reasoning_effort, first.conversation_id)
    assert [tool.name for tool in request.tools] == [tool.name for tool in first.tools]

    after = await _messages(client, session["id"])
    assert [m["role"] for m in after] == ["user", "assistant", "assistant"]
    assert after[-1]["parts"][0]["text"] == "second answer"

    turns = rt.store.turns_for_session(session["id"])
    assert turns[-1]["data"]["origin"] == "continuation"
    predecessor = turns[-1]["data"]["continuation"]["predecessor"]
    assert predecessor["turn_id"] == turns[-2]["id"]
    assert predecessor["request_id"].startswith("req_")
    assert started["turn_id"] == turns[-1]["id"]
    busy = [event for event in await _events(client, session["id"])
            if event["type"] == "session.status" and event["data"].get("turn_id") == turns[-1]["id"]
            and event["data"]["status"] == "busy"]
    assert len(busy) == 1 and busy[0]["data"]["origin"] == "continuation"
    assert busy[0]["data"]["continuation"]["predecessor"]["turn_id"] == turns[-2]["id"]


async def test_ordinary_prompt_still_adds_exactly_one_user_message(env):
    rt, client, provider, workspace = env
    session = await _create(client, workspace)
    provider.script = [[Text("ok")]]
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "only me"})
    await _idle(rt, session["id"])
    [request] = provider.requests
    assert _request_messages(request) == [{"role": "user", "parts": [{
        "type": "text", "text": "only me", "origin": None, "raw": None}]}]
    turn = rt.store.turns_for_session(session["id"])[-1]
    assert turn["data"]["origin"] == "prompt" and "continuation" not in turn["data"]
    assert [event["data"]["origin"] for event in await _events(client, session["id"])
            if event["type"] == "session.status" and event["data"].get("turn_id") == turn["id"]] == [
                "prompt", "prompt"]


# -- provider failure, interruption and recovery -----------------------------


async def test_continuation_after_a_provider_failure(env):
    rt, client, provider, workspace = env
    session = await _create(client, workspace)
    provider.script = [ProviderError("network down")]
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "hello"})
    await _idle(rt, session["id"])
    stored = await _messages(client, session["id"])
    assert [m["role"] for m in stored] == ["user"]
    assert any(event["type"] == "session.error" for event in await _events(client, session["id"]))

    provider.script = [[Text("recovered")]]
    assert (await client.post(f"/api/session/{session['id']}/continue", json={})).status_code == 202
    await _idle(rt, session["id"])
    assert _request_messages(provider.requests[-1]) == [{"role": m["role"], "parts": m["parts"]} for m in stored]
    assert (await _messages(client, session["id"]))[-1]["parts"][0]["text"] == "recovered"


async def test_continuation_after_clean_interruption_keeps_the_closed_result(env):
    rt, client, provider, workspace = env
    session = await _create(client, workspace)
    provider.script = [[ToolCall("c1", "bash", json.dumps({"command": "touch after_interrupt.txt"}))]]
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "go"})
    for _ in range(100):
        if rt.pending_approvals(session["id"]):
            break
        await asyncio.sleep(0.01)
    assert rt.pending_approvals(session["id"])
    assert (await client.post(f"/api/session/{session['id']}/interrupt")).status_code == 204
    await _idle(rt, session["id"])
    stored = await _messages(client, session["id"])
    assert [m["role"] for m in stored] == ["user", "assistant", "tool"]
    assert stored[-1]["parts"][0]["is_error"] is True
    assert "waiting for approval" in stored[-1]["parts"][0]["output"]
    assert "may have changed the workspace" not in stored[-1]["parts"][0]["output"]
    before_tools = [event for event in await _events(client, session["id"]) if event["type"] == "tool_start"]

    provider.script = [[Text("ok")]]
    assert (await client.post(f"/api/session/{session['id']}/continue", json={})).status_code == 202
    await _idle(rt, session["id"])
    assert _request_messages(provider.requests[-1]) == [{"role": m["role"], "parts": m["parts"]} for m in stored]
    assert [event for event in await _events(client, session["id"]) if event["type"] == "tool_start"] == before_tools
    assert not (workspace / "after_interrupt.txt").exists()


async def test_continuation_after_startup_recovery_keeps_unknown_tools_uncertain(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    db = tmp_path / "nexus.db"
    session_id, turn_id, _, _ = _seed_crash(db, workspace, started_tool=True)
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime("scripted/m", Store(db))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    try:
        stored = await _messages(client, session_id)
        assert [m["role"] for m in stored] == ["user", "assistant", "assistant", "tool", "user"]
        result = next(part for message in stored for part in message["parts"] if part["type"] == "tool_result")
        assert "unknown" in result["output"] and "may have changed the workspace" in result["output"]
        assert stored[-1]["parts"][0]["text"].startswith("[Nexus recovery: interrupted turn]")
        before_tools = [event for event in await _events(client, session_id) if event["type"] == "tool_start"]

        provider.script = [[Text("continued")]]
        response = await client.post(f"/api/session/{session_id}/continue", json={})
        assert response.status_code == 202, response.text
        assert response.json()["reconciled"] is None
        await _idle(rt, session_id)
        assert _request_messages(provider.requests[0]) == [
            {"role": m["role"], "parts": m["parts"]} for m in stored]
        assert [event for event in await _events(client, session_id)
                if event["type"] == "tool_start"] == before_tools
        assert not (workspace / "crashed.txt").exists()
        texts = _texts(await _messages(client, session_id))
        assert sum(text.startswith("[Nexus recovery: interrupted turn]") for text in texts) == 1
        assert not any(text.startswith(CONTINUATION_NOTICE) for text in texts)
        continued = rt.store.turns_for_session(session_id)[-1]
        assert continued["data"]["continuation"]["predecessor"]["turn_id"] == turn_id
        assert continued["data"]["continuation"]["predecessor"]["status"] == "interrupted"
    finally:
        await client.aclose()
        await rt.shutdown()


def test_crash_during_continuation_keeps_its_link(tmp_path, monkeypatch):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    db = tmp_path / "nexus.db"
    session_id, turn_id, previous_turn_id, _ = _seed_crash(db, workspace, continuation=True)
    rt = Runtime(Store(db), Bus(), "scripted/m")
    rt.recover()
    try:
        stored = rt.store.turn(turn_id)
        assert stored["data"]["origin"] == "continuation"
        assert stored["data"]["continuation"]["predecessor"]["turn_id"] == previous_turn_id
        assert stored["status"] == "interrupted"
        [recovered] = rt.recovered
        assert recovered["origin"] == "continuation"
        assert recovered["continuation"]["predecessor"]["turn_id"] == previous_turn_id
        events = rt.store.events(session_id)
        event = next(item for item in events if item["type"] == "turn.recovered")
        assert event["data"]["origin"] == "continuation"
        assert event["data"]["continuation"]["predecessor"]["turn_id"] == previous_turn_id
        notice = next(item for item in rt.store.messages(session_id)
                      if item.message.role == "user" and item.message.parts[0].text.startswith("[Nexus recovery:"))
        assert f"manual continuation of turn {previous_turn_id}" in notice.message.parts[0].text
        # Recovery reports the crash; it never continues the session by itself.
        assert rt.turns == {}
        assert len(rt.store.turns_for_session(session_id)) == 2
    finally:
        rt.store.close()


# -- tools -------------------------------------------------------------------


async def test_completed_tools_are_not_executed_again(env):
    rt, client, provider, workspace = env
    session = await _create(client, workspace)
    provider.script = [[ToolCall("w1", "write", json.dumps({"path": "kept.txt", "content": "first"}))],
                       [Text("written")]]
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "write it"})
    await _idle(rt, session["id"])
    stored = await _messages(client, session["id"])
    before_tools = [event for event in await _events(client, session["id"]) if event["type"] == "tool_start"]
    assert len(before_tools) == 1 and (workspace / "kept.txt").read_text() == "first"

    provider.script = [[Text("nothing to redo")]]
    assert (await client.post(f"/api/session/{session['id']}/continue", json={})).status_code == 202
    await _idle(rt, session["id"])
    assert [event for event in await _events(client, session["id"]) if event["type"] == "tool_start"] == before_tools
    assert (workspace / "kept.txt").read_text() == "first"
    assert _request_messages(provider.requests[-1]) == [{"role": m["role"], "parts": m["parts"]} for m in stored]
    assert any(part["type"] == "tool_result" for message in stored for part in message["parts"])


async def test_new_tool_calls_from_the_continued_model_run_normally(env):
    rt, client, provider, workspace = env
    session = await _create(client, workspace)
    provider.script = [[Text("seed")]]
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "hello"})
    await _idle(rt, session["id"])
    provider.script = [[ToolCall("w2", "write", json.dumps({"path": "new.txt", "content": "from continuation"}))],
                       [Text("created")]]
    assert (await client.post(f"/api/session/{session['id']}/continue", json={})).status_code == 202
    await _idle(rt, session["id"])
    assert (workspace / "new.txt").read_text() == "from continuation"
    messages = await _messages(client, session["id"])
    assert [m["role"] for m in messages][-3:] == ["assistant", "tool", "assistant"]
    assert messages[-2]["parts"][0]["call_id"] == "w2" and not messages[-2]["parts"][0]["is_error"]
    events = await _events(client, session["id"])
    assert [event["data"]["call"]["id"] for event in events if event["type"] == "tool_start"] == ["w2"]


async def test_current_settings_and_directory_apply(env):
    rt, client, provider, workspace = env
    session = await _create(client, workspace)
    provider.script = [[Text("seed")]]
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "hello"})
    await _idle(rt, session["id"])
    other = (env.tmp_path / "other")
    other.mkdir()
    assert (await client.patch(f"/api/session/{session['id']}", json={
        "model": "scripted/other", "effort": "high", "disabled_tools": ["bash"]})).status_code == 200
    moved = await client.post(f"/api/session/{session['id']}/directory", json={"directory": str(other)})
    assert moved.status_code == 200 and moved.json()["change"]["status"] == "applied"

    provider.script = [[ToolCall("w3", "write", json.dumps({"path": "here.txt", "content": "moved"}))],
                       [Text("done")]]
    assert (await client.post(f"/api/session/{session['id']}/continue", json={})).status_code == 202
    await _idle(rt, session["id"])
    request = provider.requests[-2]
    assert (request.model, request.reasoning_effort) == ("other", "high")
    assert "bash" not in [tool.name for tool in request.tools] and "write" in [tool.name for tool in request.tools]
    assert (other / "here.txt").read_text() == "moved"
    assert not (workspace / "here.txt").exists()


# -- errors and trees --------------------------------------------------------


async def test_continue_refuses_busy_archived_unknown_and_empty(env):
    rt, client, provider, workspace = env
    empty = await _create(client, workspace)
    response = await client.post(f"/api/session/{empty['id']}/continue", json={})
    assert response.status_code == 400
    assert "no stored messages" in response.json()["error"] and "send a prompt" in response.json()["error"]
    assert (await client.post("/api/session/ses_missing/continue", json={})).status_code == 404
    assert (await client.post(f"/api/session/{empty['id']}/continue", json={},
                              headers={"authorization": ""})).status_code == 401

    assert (await client.post(f"/api/session/{empty['id']}/archive")).status_code == 200
    response = await client.post(f"/api/session/{empty['id']}/continue", json={})
    assert response.status_code == 409 and "unarchive" in response.json()["error"]
    assert (await client.post(f"/api/session/{empty['id']}/unarchive")).status_code == 200

    session = await _create(client, workspace)
    gate = asyncio.Event()
    provider.gates = [gate]
    provider.script = [[Text("slow")]]
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "hold"})
    assert session["id"] in rt.turns
    response = await client.post(f"/api/session/{session['id']}/continue", json={})
    assert response.status_code == 409 and "already running" in response.json()["error"]
    gate.set()
    await _idle(rt, session["id"])


async def test_parent_and_child_continue_independently(env):
    rt, client, provider, workspace = env
    parent = await _create(client, workspace, title="parent")
    provider.script = [[Text("parent first")]]
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "parent prompt"})
    await _idle(rt, parent["id"])
    parent_stored = await _messages(client, parent["id"])

    child = await _child(client, parent["id"])
    provider.script = [[Text("child first")]]
    await client.post(f"/api/session/{child['id']}/prompt", json={"text": "child prompt"})
    await _idle(rt, child["id"])
    child_stored = await _messages(client, child["id"])

    provider.script = [[Text("child again")]]
    assert (await client.post(f"/api/session/{child['id']}/continue", json={})).status_code == 202
    await _idle(rt, child["id"])
    assert _request_messages(provider.requests[-1]) == [{"role": m["role"], "parts": m["parts"]} for m in child_stored]
    assert await _messages(client, parent["id"]) == parent_stored

    provider.script = [[Text("parent again")]]
    assert (await client.post(f"/api/session/{parent['id']}/continue", json={})).status_code == 202
    await _idle(rt, parent["id"])
    assert _request_messages(provider.requests[-1]) == [{"role": m["role"], "parts": m["parts"]} for m in parent_stored]
    for session in (child, parent):
        turns = rt.store.turns_for_session(session["id"])
        assert turns[-1]["data"]["origin"] == "continuation"
        assert turns[-1]["data"]["continuation"]["predecessor"]["turn_id"] == turns[-2]["id"]


# -- reconciliation ----------------------------------------------------------


def test_reconciliation_closes_saved_and_legacy_calls_once(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    db = tmp_path / "nexus.db"
    saved_id = _seed_dangling(db, workspace, legacy=False)
    legacy_id = _seed_dangling(db, workspace, legacy=True)
    store = Store(db)
    try:
        first = reconcile(store, saved_id)
        assert first is not None and first.calls[0]["outcome"] == "not_run"
        assert first.calls[0]["state"] == "saved"
        assert reconcile(store, saved_id) is None
        legacy = reconcile(store, legacy_id)
        assert legacy is not None and legacy.calls[0]["outcome"] == "legacy_unknown"
        assert reconcile(store, legacy_id) is None
        saved_messages = store.messages(saved_id)
        assert [m.message.role for m in saved_messages] == ["user", "assistant", "tool", "user"]
        assert [m for m in saved_messages if any(isinstance(part, Text) and part.text.startswith(
            CONTINUATION_NOTICE) for part in m.message.parts)]
        turns = store.turns_for_session(saved_id)
        assert turns and store.tools_for_turn(turns[-1]["id"])[0]["state"] == "inserted"

        legacy_messages = store.messages(legacy_id)
        assert [m.message.role for m in legacy_messages] == ["user", "assistant", "tool", "user"]
        assert [m for m in legacy_messages if any(isinstance(part, Text) and part.text.startswith(
            CONTINUATION_NOTICE) for part in m.message.parts)]
        assert store.turns_for_session(legacy_id) == []
    finally:
        store.close()


def test_unresolved_calls_match_results_in_conversation_order(tmp_path):
    store = Store(tmp_path / "nexus.db")
    try:
        project, _ = store.project_for_path(str(tmp_path))
        session = store.create_session(project, title="reused", agent="build")
        store.append_message(session.id, Message("user", (Text("start"),)))
        first = ToolCall("c1", "bash", '{"command": "true"}')
        store.append_message(session.id, Message("assistant", (first,)))
        store.append_message(session.id, Message("tool", (ToolResult("c1", "ok"),)))
        reused = ToolCall("c1", "bash", '{"command": "true"}')
        store.append_message(session.id, Message("assistant", (reused,)))
        [(index, call)] = unresolved_calls(store.messages(session.id))
        assert index == 3 and call.id == reused.id and call.arguments == reused.arguments
    finally:
        store.close()


def test_reconciliation_preserves_a_finished_result(tmp_path):
    store = Store(tmp_path / "nexus.db")
    try:
        project, _ = store.project_for_path(str(tmp_path))
        session = store.create_session(project, title="finished", agent="build")
        store.append_message(session.id, Message("user", (Text("go"),)))
        turn = store.start_turn(session.id, data={"origin": "prompt"})
        call = ToolCall("c1", "bash", '{"command": "echo ok"}')
        store.save_assistant_message(session.id, Message("assistant", (call,)), turn_id=turn["id"])
        store.mark_tool_finished(turn["id"], session.id, call, ToolResult("c1", "durable output"))
        store.finish_turn(turn["id"], "completed", steps=1)
        store.set_turn_recovery(turn["id"], "none")

        repaired = reconcile(store, session.id)
        assert repaired is not None and repaired.calls[0]["outcome"] == "restored"
        tool_message = store.messages(session.id)[-2]
        assert tool_message.message.parts[0].output == "durable output"
        assert tool_message.message.parts[0].is_error is False
        assert reconcile(store, session.id) is None
    finally:
        store.close()


async def test_continuation_reconciles_unresolved_context_before_sending(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    db = tmp_path / "nexus.db"
    session_id = _seed_dangling(db, workspace, legacy=False)
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    # No startup recovery: continuation itself must reconcile this one session.
    rt = Runtime(Store(db), Bus(), "scripted/m")
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    try:
        provider.script = [[Text("after repair")]]
        response = await client.post(f"/api/session/{session_id}/continue", json={})
        assert response.status_code == 202, response.text
        assert response.json()["reconciled"]["calls"][0]["outcome"] == "not_run"
        await _idle(rt, session_id)
        messages = await _messages(client, session_id)
        assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user", "assistant"]
        assert messages[2]["parts"][0]["is_error"] is True
        assert messages[3]["parts"][0]["text"].startswith(CONTINUATION_NOTICE)
        assert _request_messages(provider.requests[0]) == [
            {"role": m["role"], "parts": m["parts"]} for m in messages[:4]]
        assert not (workspace / "dangling.txt").exists()

        provider.script = [[Text("again")]]
        assert (await client.post(f"/api/session/{session_id}/continue", json={})).status_code == 202
        await _idle(rt, session_id)
        texts = _texts(await _messages(client, session_id))
        assert sum(text.startswith(CONTINUATION_NOTICE) for text in texts) == 1
    finally:
        await client.aclose()
        await rt.shutdown()


# -- the CLI -----------------------------------------------------------------


async def test_cli_slash_and_standalone_continue(env, monkeypatch):
    rt, _, provider, workspace = env
    session = await _create(env.client, workspace)
    provider.script = [[Text("seed")]]
    await env.client.post(f"/api/session/{session['id']}/prompt", json={"text": "hello"})
    await _idle(rt, session["id"])

    class CliClient:
        """REST through the ASGI app, with a finite stream that ends when the turn does."""

        def __init__(self):
            self.real = _cli_client(env.app)
            self.subscribed = False

        async def close(self):
            await self.real.close()

        async def call(self, method, path, body=None, **params):
            return await self.real.call(method, path, body, **params)

        async def events(self, session_id):
            self.subscribed = True
            yield {"type": "server.connected"}
            while session_id not in rt.turns:
                await asyncio.sleep(0)
            yield {"type": "session.status", "session_id": session_id,
                   "data": {"status": "busy", "origin": "continuation"}}
            while session_id in rt.turns:
                await asyncio.sleep(0.01)
            yield {"type": "session.status", "session_id": session_id, "data": {"status": "idle"}}

    slash = CliClient()
    provider.script = [[Text("from slash")]]
    await cli._chat_line(slash, session, "/continue", cli.Renderer(wire=False, wire_file=None, out=io.StringIO()))
    await _idle(rt, session["id"])
    assert slash.subscribed
    assert _texts(await _messages(env.client, session["id"]))[-1] == "from slash"
    await slash.close()

    standalone = CliClient()
    monkeypatch.setattr(cli.Client, "connect", classmethod(lambda cls: standalone))
    provider.script = [[Text("from command")]]
    args = argparse.Namespace(action="continue", id=session["id"], wire=False, wire_file=None)
    await cli._session(args)
    await _idle(rt, session["id"])
    assert standalone.subscribed
    assert _texts(await _messages(env.client, session["id"]))[-1] == "from command"


async def test_cli_subscribes_before_continue_and_never_repeats_an_ambiguous_post():
    class Fake:
        def __init__(self, fail=None):
            self.fail = fail
            self.subscribed = False
            self.calls = []

        async def events(self, session_id):
            self.subscribed = True
            yield {"type": "server.connected"}
            yield {"type": "session.status", "session_id": "ses", "data": {
                "status": "busy", "origin": "continuation"}}
            yield {"type": "session.status", "session_id": "ses", "data": {"status": "idle"}}

        async def call(self, method, path, body=None, **kwargs):
            assert self.subscribed, "the continuation POST happened before the event subscription"
            self.calls.append((method, path))
            if self.fail is not None:
                raise self.fail
            return {"status": "busy"}

    renderer = cli.Renderer(wire=False, wire_file=None, out=io.StringIO())
    fake = Fake()
    await asyncio.wait_for(cli._continue(fake, "ses", renderer), 2)
    assert fake.calls == [("POST", "/api/session/ses/continue")]

    failing = Fake(httpx.ConnectError("connection reset"))
    with pytest.raises(ValueError, match="not resubmitted as a new action"):
        await asyncio.wait_for(cli._continue(failing, "ses", renderer), 2)
    assert failing.calls == [("POST", "/api/session/ses/continue")]


@pytest.mark.parametrize("new_record", [False, True])
def test_reused_call_id_does_not_inherit_an_older_result(tmp_path, new_record):
    store = Store(tmp_path / "nexus.db")
    try:
        project, _ = store.project_for_path(str(tmp_path))
        session = store.create_session(project, title="reused evidence", agent="build")
        turn = store.start_turn(session.id)
        call = ToolCall("same", "bash", '{"command": "true"}')
        store.save_assistant_message(session.id, Message("assistant", (call,)), turn_id=turn["id"])
        store.save_tool_message(session.id, Message("tool", (ToolResult("same", "old output"),)), turn_id=turn["id"])
        if new_record:
            store.save_assistant_message(session.id, Message("assistant", (call,)), turn_id=turn["id"])
        else:
            store.append_message(session.id, Message("assistant", (call,)))
        repair = reconcile(store, session.id)
        assert repair.calls[0]["outcome"] == ("not_run" if new_record else "legacy_unknown")
        assert repair.tool_message.message.parts[0].is_error
        assert repair.tool_message.message.parts[0].output != "old output"
        assert reconcile(store, session.id) is None
    finally:
        store.close()


def test_reused_call_does_not_inherit_old_approval_denial(tmp_path):
    store = Store(tmp_path / "nexus.db")
    try:
        project, _ = store.project_for_path(str(tmp_path))
        session = store.create_session(project, title="reused approval", agent="build")
        bus = Bus()
        bus.attach(store)
        bus.publish("permission.asked", {"id": "old", "call_id": "same"}, session_id=session.id)
        bus.publish("permission.replied", {"id": "old", "allow": False}, session_id=session.id)
        turn = store.start_turn(session.id)
        call = ToolCall("same", "bash", '{"command": "true"}')
        store.save_assistant_message(session.id, Message("assistant", (call,)), turn_id=turn["id"])
        store.mark_tool_started(turn["id"], session.id, call)
        repair = reconcile(store, session.id)
        assert repair.calls[0]["outcome"] == "unknown"
        assert "may have changed" in repair.tool_message.message.parts[0].output
    finally:
        store.close()


async def test_cli_ignores_previous_turn_idle_before_continuation_busy():
    class Fake:
        delivered = False

        async def events(self, session_id):
            yield {"type": "server.connected"}
            yield {"type": "session.status", "session_id": "ses", "data": {
                "status": "idle", "turn_id": "old"}}
            yield {"type": "session.status", "session_id": "ses", "data": {
                "status": "busy", "turn_id": "new", "origin": "continuation"}}
            self.delivered = True
            yield {"type": "session.status", "session_id": "ses", "data": {
                "status": "idle", "turn_id": "new"}}

        async def call(self, *args, **kwargs):
            return {"status": "busy", "turn_id": "new"}

    client = Fake()
    await cli._continue(client, "ses", cli.Renderer(wire=False, wire_file=None, out=io.StringIO()))
    assert client.delivered
