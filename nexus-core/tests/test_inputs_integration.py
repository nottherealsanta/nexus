"""Durable steer/queue integration through the Runtime and HTTP API (PLAN B)."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from conftest import remembering

from nexus import runtime as runtime_module
from nexus.llmhub.types import Done, Request, Text
from nexus.mock.provider import MockProvider
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "input-test-token"
PNG = (Path(__file__).parent / "fixtures/clipboard.png").read_bytes()
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


class RecordedMock(MockProvider):
    """A real scenario provider with request capture and an optional final-event gate."""

    def __init__(self, scenario: str):
        super().__init__(scenario)
        self.requests: list[Request] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.pause_before_done = False
        self._paused = False

    async def stream(self, req: Request):
        self.requests.append(req)
        async for event in super().stream(req):
            if self.pause_before_done and not self._paused and isinstance(event, Done):
                self._paused = True
                self.entered.set()
                await self.release.wait()
            yield event

    def open_gate(self) -> None:
        self.release.set()


def _user_texts(req: Request) -> list[str]:
    return ["".join(part.text for part in msg.parts if isinstance(part, Text)) for msg in req.messages if msg.role == "user"]


@pytest.fixture
async def setup_inputs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("NEXUS10_HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = RecordedMock("hello")
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    runtime = make_runtime(remembering(Store(tmp_path / "nexus.db"), "mock/hello"))
    app = build_app(runtime, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"authorization": f"Bearer {TOKEN}"},
    )
    yield runtime, client, provider, workspace
    await client.aclose()
    await runtime.shutdown()
    runtime.store.close()


async def _session(client: httpx.AsyncClient, workspace, *, model="mock/hello") -> str:
    response = await client.post("/api/session", json={"directory": str(workspace), "model": model, "permission": "allow"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _wait_until(predicate, message: str, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(message)


async def _messages(client, session_id: str):
    response = await client.get(f"/api/session/{session_id}/message")
    assert response.status_code == 200, response.text
    return response.json()


async def _inputs(client, session_id: str):
    response = await client.get(f"/api/session/{session_id}/input")
    assert response.status_code == 200, response.text
    return response.json()


async def _events(client, session_id: str):
    response = await client.get(f"/api/session/{session_id}/event")
    assert response.status_code == 200, response.text
    return response.json()["events"]


async def test_steer_during_tool_call_is_after_result_before_next_request(setup_inputs, monkeypatch):
    runtime, client, _provider, workspace = setup_inputs
    session_id = await _session(client, workspace, model="mock/tool_marathon")
    # Use A2's actual scenario provider and pause its first real workspace write.
    marathon = RecordedMock("tool_marathon")
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (marathon, ref.partition("/")[2]))
    entered, release = asyncio.Event(), asyncio.Event()
    original_registry = runtime._registry

    def registry():
        tools = original_registry()
        writer = tools["write"]

        async def blocked_write(args, ctx):
            entered.set()
            await release.wait()
            return await writer.run(args, ctx)

        tools["write"] = replace(writer, run=blocked_write)
        return tools

    monkeypatch.setattr(runtime, "_registry", registry)
    started = await client.post(f"/api/session/{session_id}/prompt", json={"text": "Run the tool marathon."})
    assert started.status_code == 202
    await asyncio.wait_for(entered.wait(), 3)
    assert (await client.post(f"/api/session/{session_id}/prompt", json={"text": "ordinary send"})).status_code == 409
    steered = await client.post(f"/api/session/{session_id}/prompt", json={"text": "also check the result", "mode": "steer"})
    second = await client.post(f"/api/session/{session_id}/prompt", json={"text": "then report the count", "mode": "steer"})
    assert steered.status_code == second.status_code == 202
    input_ids = [steered.json()["input_id"], second.json()["input_id"]]
    assert [item["id"] for item in (await _inputs(client, session_id))["inputs"]] == input_ids
    release.set()
    await _wait_until(lambda: session_id not in runtime.turns, "marathon turn did not finish", timeout=20)

    request = next(req for req in marathon.requests if "also check the result" in _user_texts(req))
    roles = [msg.role for msg in request.messages[-4:]]
    assert roles == ["assistant", "tool", "user", "user"]
    assert _user_texts(request)[-2:] == ["also check the result", "then report the count"]
    messages = await _messages(client, session_id)
    delivered = [m for m in messages if m.get("delivery") == "steer"]
    assert [m["parts"][0]["text"] for m in delivered] == ["also check the result", "then report the count"]
    events = await _events(client, session_id)
    delivered_events = [e["data"] for e in events if e["type"] == "input.delivered"]
    assert [e["id"] for e in delivered_events] == input_ids
    assert [e["message_id"] for e in delivered_events] == [m["id"] for m in delivered]
    assert {e["turn_id"] for e in delivered_events} == {started.json()["turn_id"]}


async def test_steer_during_final_answer_adds_another_model_step(setup_inputs):
    runtime, client, provider, workspace = setup_inputs
    session_id = await _session(client, workspace)
    provider.pause_before_done = True
    started = await client.post(f"/api/session/{session_id}/prompt", json={"text": "Say hello."})
    assert started.status_code == 202
    await asyncio.wait_for(provider.entered.wait(), 3)
    steered = await client.post(f"/api/session/{session_id}/prompt", json={"text": "Add one more detail", "mode": "steer"})
    assert steered.status_code == 202
    provider.open_gate()
    await _wait_until(lambda: session_id not in runtime.turns, "steered final-answer turn did not finish")
    assert len(provider.requests) == 2
    assert _user_texts(provider.requests[1])[-1] == "Add one more detail"
    delivered = [m for m in await _messages(client, session_id) if m.get("delivery") == "steer"]
    assert [m["parts"][0]["text"] for m in delivered] == ["Add one more detail"]


async def test_queued_inputs_start_fifo_as_separate_turns(setup_inputs):
    runtime, client, provider, workspace = setup_inputs
    session_id = await _session(client, workspace)
    provider.pause_before_done = True
    first = await client.post(f"/api/session/{session_id}/prompt", json={"text": "first"})
    assert first.status_code == 202
    await asyncio.wait_for(provider.entered.wait(), 3)
    one = await client.post(f"/api/session/{session_id}/prompt", json={"text": "queued one", "mode": "queue"})
    two = await client.post(f"/api/session/{session_id}/prompt", json={"text": "queued two", "mode": "queue"})
    assert one.status_code == two.status_code == 202
    provider.open_gate()
    await _wait_until(lambda: session_id not in runtime.turns and len(provider.requests) >= 3, "queued turns did not drain")
    assert _user_texts(provider.requests[1])[-1] == "queued one"
    assert _user_texts(provider.requests[2])[-1] == "queued two"
    messages = await _messages(client, session_id)
    assert [m["parts"][0]["text"] for m in messages if m.get("delivery") == "queued"] == ["queued one", "queued two"]
    assert (await _inputs(client, session_id))["inputs"] == []


async def test_interrupt_holds_queue_until_explicit_send_next(setup_inputs):
    runtime, client, provider, workspace = setup_inputs
    session_id = await _session(client, workspace)
    provider.pause_before_done = True
    started = await client.post(f"/api/session/{session_id}/prompt", json={"text": "hold here"})
    assert started.status_code == 202
    await asyncio.wait_for(provider.entered.wait(), 3)
    queued = await client.post(f"/api/session/{session_id}/prompt", json={"text": "run after interrupt", "mode": "queue"})
    assert queued.status_code == 202
    interrupted = await client.post(f"/api/session/{session_id}/interrupt")
    assert interrupted.status_code == 204
    await _wait_until(lambda: session_id not in runtime.turns, "interrupted turn did not stop")
    pending = await _inputs(client, session_id)
    assert pending["held"] is True and pending["inputs"][0]["held"] is True
    assert len(provider.requests) == 1
    released = await client.post(f"/api/session/{session_id}/input/next", json={})
    assert released.status_code in (200, 202), released.text
    await _wait_until(lambda: session_id not in runtime.turns and len(provider.requests) >= 2, "explicitly released queue did not run")
    messages = await _messages(client, session_id)
    queued_messages = [m for m in messages if m.get("delivery") == "queued"]
    assert [m["parts"][0]["text"] for m in queued_messages] == ["run after interrupt"]


async def test_recovery_keeps_pending_input_and_converts_steer_to_held_queue(setup_inputs):
    runtime, client, _provider, workspace = setup_inputs
    session_id = await _session(client, workspace)
    # Represent a turn accepted by the old daemon but not yet recovered by its replacement.
    turn = runtime.store.start_turn(session_id, data={"origin": "prompt"})
    item = runtime.inputs.add(session_id, "steer", [{"type": "text", "text": "survive restart"}])
    db_path = runtime.store.path
    runtime.store.close()

    recovered_store = Store(db_path)
    replacement = make_runtime(remembering(recovered_store, "mock/hello"))
    outcomes = replacement.recovered
    pending = replacement.inputs.list(session_id)
    assert pending[0]["id"] == item["id"] and pending[0]["mode"] == "queue"
    assert replacement.inputs.held(session_id) is True
    recovered_turn = next(t for t in recovered_store.turns_for_session(session_id) if t["id"] == turn["id"])
    assert recovered_turn["status"] == "interrupted"
    assert session_id not in replacement.turns
    assert outcomes and outcomes[0]["notice"]
    notice = next(m for m in recovered_store.messages(session_id) if m.id == outcomes[0]["notice"])
    notice_text = "".join(part.text for part in notice.message.parts if isinstance(part, Text))
    assert "Pending input is held after restart" in notice_text
    assert "undelivered steering was moved to the queue" in notice_text
    recovered_store.close()
    await client.aclose()


async def test_removing_pending_input_returns_all_parts(setup_inputs):
    runtime, client, _, workspace = setup_inputs
    session_id = await _session(client, workspace)
    provider = runtime_module.provider_for("mock/hello")[0]
    provider.pause_before_done = True
    started = await client.post(f"/api/session/{session_id}/prompt", json={"text": "busy"})
    assert started.status_code == 202
    await asyncio.wait_for(provider.entered.wait(), 3)
    image = {"name": "sample.png", "data_url": PNG_URL}
    queued = await client.post(f"/api/session/{session_id}/prompt", json={"text": "inspect this", "images": [image], "mode": "queue"})
    assert queued.status_code == 202
    iid = queued.json()["input_id"]
    removed = await client.delete(f"/api/session/{session_id}/input/{iid}")
    assert removed.status_code == 200, removed.text
    parts = removed.json()["parts"]
    assert parts[0]["type"] == "text" and parts[0]["text"] == "inspect this"
    assert parts[1]["type"] == "text" and parts[1]["text"] == "[Image A]"
    assert parts[2]["type"] == "image" and parts[2]["data_url"] == PNG_URL
    assert (await _inputs(client, session_id))["inputs"] == []
    provider.open_gate()
    await _wait_until(lambda: session_id not in runtime.turns, "original turn did not finish")
    assert not any("inspect this" in text for req in provider.requests for text in _user_texts(req))


async def test_reserved_input_cannot_be_edited_and_recovers_after_restart(tmp_path, monkeypatch):
    from nexus.server import make_runtime
    from nexus.store import Store

    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    store = Store(tmp_path / "state.db")
    runtime = make_runtime(remembering(store, "codex/model"))
    project = runtime.open_project(str(tmp_path))
    session = runtime.create_session(project, title=None, model=None, permission=None)
    item = runtime.inputs.add(session.id, "queue", [{"type": "text", "text": "next"}])
    store.start_turn(session.id, input_id=item["id"])
    from nexus.inputs import InputBusy

    with pytest.raises(InputBusy):
        runtime.inputs.remove(session.id, item["id"])
    store.close()
    recovered = make_runtime(remembering(Store(tmp_path / "state.db"), "codex/model"))
    assert recovered.inputs.held(session.id)
    assert recovered.inputs.remove(session.id, item["id"])["id"] == item["id"]


async def test_keyed_queued_acceptance_replays_one_input_while_busy(setup_inputs):
    runtime, client, provider, workspace = setup_inputs
    session_id = await _session(client, workspace)
    provider.pause_before_done = True
    started = await client.post(f"/api/session/{session_id}/prompt", json={"text": "busy"})
    await asyncio.wait_for(provider.entered.wait(), 3)

    body = {"text": "run this later", "mode": "queue"}
    headers = {"Idempotency-Key": "queued-once"}
    accepted = await client.post(f"/api/session/{session_id}/prompt", json=body, headers=headers)
    replayed = await client.post(f"/api/session/{session_id}/prompt", json=body, headers=headers)
    assert accepted.status_code == replayed.status_code == 202
    assert replayed.headers.get("idempotent-replayed") == "true"
    assert replayed.json()["input_id"] == accepted.json()["input_id"]
    pending = await _inputs(client, session_id)
    assert [item["id"] for item in pending["inputs"]] == [accepted.json()["input_id"]]
    added = [event for event in await _events(client, session_id) if event["type"] == "input.added"]
    assert [event["data"]["id"] for event in added] == [accepted.json()["input_id"]]

    provider.open_gate()
    await _wait_until(lambda: session_id not in runtime.turns and len(provider.requests) >= 2, "accepted queue did not run")
    assert _user_texts(provider.requests[1])[-1] == "run this later"
    assert started.status_code == 202


async def test_input_move_and_patch_promote_edit_text_and_preserve_fifo(setup_inputs):
    runtime, client, provider, workspace = setup_inputs
    session_id = await _session(client, workspace)
    provider.pause_before_done = True
    started = await client.post(f"/api/session/{session_id}/prompt", json={"text": "start"})
    await asyncio.wait_for(provider.entered.wait(), 3)
    queued = []
    for text in ("first queued", "second queued", "third queued"):
        response = await client.post(f"/api/session/{session_id}/prompt", json={"text": text, "mode": "queue"})
        assert response.status_code == 202
        queued.append(response.json()["input_id"])

    moved = await client.post(f"/api/session/{session_id}/input/{queued[2]}/move", json={"before": queued[0]})
    assert moved.status_code == 200, moved.text
    assert [item["id"] for item in moved.json()["inputs"]] == [queued[2], queued[0], queued[1]]

    patched = await client.patch(f"/api/session/{session_id}/input/{queued[2]}", json={"text": "edited priority", "mode": "steer"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["mode"] == "steer"
    assert patched.json()["parts"][0]["text"] == "edited priority"
    assert [item["id"] for item in (await _inputs(client, session_id))["inputs"]] == [queued[2], queued[0], queued[1]]

    provider.open_gate()
    await _wait_until(lambda: session_id not in runtime.turns and len(provider.requests) >= 4, "promoted and queued inputs did not run")
    assert _user_texts(provider.requests[1])[-1] == "edited priority"
    assert _user_texts(provider.requests[2])[-1] == "first queued"
    assert _user_texts(provider.requests[3])[-1] == "second queued"
    messages = await _messages(client, session_id)
    assert [m["parts"][0]["text"] for m in messages if m.get("delivery") == "steer"] == ["edited priority"]
    assert [m["parts"][0]["text"] for m in messages if m.get("delivery") == "queued"] == ["first queued", "second queued"]
    assert started.status_code == 202


async def _interrupt_with_queued_input(client, provider, workspace, text: str) -> tuple[str, str]:
    session_id = await _session(client, workspace)
    provider.entered = asyncio.Event()
    provider.release = asyncio.Event()
    provider.pause_before_done = True
    provider._paused = False
    started = await client.post(f"/api/session/{session_id}/prompt", json={"text": "interrupt me"})
    assert started.status_code == 202
    await asyncio.wait_for(provider.entered.wait(), 3)
    queued = await client.post(f"/api/session/{session_id}/prompt", json={"text": text, "mode": "queue"})
    assert queued.status_code == 202
    stopped = await client.post(f"/api/session/{session_id}/interrupt")
    assert stopped.status_code == 204
    return session_id, queued.json()["input_id"]


async def test_archive_and_delete_refuse_pending_inputs_unless_drop_is_explicit(setup_inputs):
    runtime, client, provider, workspace = setup_inputs
    session_id, input_id = await _interrupt_with_queued_input(client, provider, workspace, "keep pending")
    blocked_archive = await client.post(f"/api/session/{session_id}/archive", json={})
    blocked_delete = await client.request("DELETE", f"/api/session/{session_id}", json={})
    assert blocked_archive.status_code == blocked_delete.status_code == 409

    archived = await client.post(f"/api/session/{session_id}/archive", json={"drop_inputs": True})
    assert archived.status_code == 200, archived.text
    assert (await _inputs(client, session_id))["inputs"] == []
    removed = [event["data"]["id"] for event in await _events(client, session_id) if event["type"] == "input.removed"]
    assert removed == [input_id]

    second_id, second_input = await _interrupt_with_queued_input(client, provider, workspace, "drop on delete")
    deleted = await client.request("DELETE", f"/api/session/{second_id}", json={"drop_inputs": True})
    assert deleted.status_code == 204
    assert (await client.get(f"/api/session/{second_id}")).status_code == 404
    assert runtime.store.session(second_id) is None
    assert second_input != input_id


async def test_recovery_does_not_duplicate_delivered_event_after_publish_crash(setup_inputs):
    runtime, client, _, workspace = setup_inputs
    session_id = await _session(client, workspace)
    turn = runtime.store.start_turn(session_id, data={"origin": "prompt"})
    item = runtime.inputs.add(session_id, "queue", [{"type": "text", "text": "already delivered"}])
    stored = runtime.inputs.deliver(session_id, item["id"], turn["id"])
    # Crash window: the durable event was appended, but event_id was not written back to input.
    runtime.bus.publish(
        "input.delivered",
        {**item, "state": "delivered", "message_id": stored.id, "turn_id": turn["id"]},
        session_id=session_id,
    )
    db_path = runtime.store.path
    runtime.store.close()

    first = make_runtime(remembering(Store(db_path), "mock/hello"))
    delivered = [event for event in first.store.events(session_id) if event["type"] == "input.delivered"]
    assert len(delivered) == 1
    row = first.store.db.execute("select event_id from pending_input where id = ?", (item["id"],)).fetchone()
    assert row["event_id"] is not None
    first.store.close()

    second = make_runtime(remembering(Store(db_path), "mock/hello"))
    delivered_again = [event for event in second.store.events(session_id) if event["type"] == "input.delivered"]
    assert len(delivered_again) == 1
    second.store.close()
