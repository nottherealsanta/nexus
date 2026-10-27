"""Durable mutation request ids, replay, conflicts and revisions (Steps 11.2-11.3)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from nexus import runtime as runtime_module
from nexus.bus import Bus
from nexus.llmhub.types import Done, PartDone, Text
from nexus.runtime import Runtime
from nexus.server import _fingerprint, build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


class Scripted:
    name = "scripted"

    def __init__(self) -> None:
        self.replies = []
        self.gates = []
        self.requests = []

    def body(self, req):
        return {"model": req.model, "messages": len(req.messages)}

    async def stream(self, req):
        self.requests.append(req)
        gate = self.gates.pop(0) if self.gates else None
        if gate is not None:
            await gate.wait()
        for part in self.replies.pop(0):
            yield PartDone(part)
        yield Done("end_turn")


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime("scripted/m", Store(tmp_path / "nexus.db"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return rt, client, provider, workspace


async def _idle(rt, session_id):
    for _ in range(300):
        if session_id not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("turn did not finish")


async def _session(client, workspace):
    return (await client.post("/api/session", json={"directory": str(workspace)})).json()


async def test_same_key_and_body_replays_prompt_without_a_second_turn(daemon):
    rt, client, provider, workspace = daemon
    provider.replies = [[Text("once")]]
    session = await _session(client, workspace)
    key = "prompt-key-1"
    first = await client.post(f"/api/session/{session['id']}/prompt", json={"text": "hello"},
                              headers={"Idempotency-Key": key})
    assert first.status_code == 202
    turn_id = first.json()["turn_id"]
    await _idle(rt, session["id"])
    second = await client.post(f"/api/session/{session['id']}/prompt", json={"text": "hello"},
                               headers={"Idempotency-Key": key})
    assert second.status_code == 202 and second.headers["idempotent-replayed"] == "true"
    assert second.json()["turn_id"] == turn_id
    assert len(provider.requests) == 1
    assert len(rt.store.turns_for_session(session["id"])) == 1
    messages = (await client.get(f"/api/session/{session['id']}/message")).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]

    changed = await client.post(f"/api/session/{session['id']}/prompt", json={"text": "different"},
                                headers={"Idempotency-Key": key})
    assert changed.status_code == 409
    assert changed.json()["code"] == "idempotency_key_reused"
    assert len(provider.requests) == 1


async def test_keyed_continue_replays_its_original_acceptance(daemon):
    rt, client, provider, workspace = daemon
    provider.replies = [[Text("seed")], [Text("continued")]]
    session = await _session(client, workspace)
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "seed"})
    await _idle(rt, session["id"])
    key = "continue-key"
    first = await client.post(f"/api/session/{session['id']}/continue", json={},
                              headers={"Idempotency-Key": key})
    assert first.status_code == 202 and first.json()["origin"] == "continuation"
    await _idle(rt, session["id"])
    second = await client.post(f"/api/session/{session['id']}/continue", json={},
                               headers={"Idempotency-Key": key})
    assert second.status_code == 202 and second.headers["idempotent-replayed"] == "true"
    assert second.json() == first.json()
    assert len(provider.requests) == 2  # seed + one continuation
    assert len(rt.store.turns_for_session(session["id"])) == 2


async def test_a_crash_between_acceptance_and_task_creation_replays_visibly(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    db = tmp_path / "nexus.db"
    store = Store(db)
    (tmp_path / "proj").mkdir(exist_ok=True)
    project, _ = store.project_for_path(str(tmp_path / "proj"))
    session = store.create_session(project, title="crash", agent="build")
    key = "crash-key"
    path = f"/api/session/{session.id}/prompt"
    request = Request({"type": "http", "method": "POST", "path": path, "query_string": b"", "headers": []})
    fingerprint = _fingerprint(request, json.dumps({"text": "hello"}).encode())
    turn = store.start_turn(session.id, data={"origin": "prompt"}, mutation={
        "key": key, "fingerprint": fingerprint, "operation": "prompt", "target": path,
        "state": "accepted", "status": 202,
        "body": {"status": "busy", "idempotency_key": key},
    })
    store.close()

    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = Runtime(Store(db), Bus(), "scripted/m")
    rt.recover()
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    try:
        replay = await client.post(path, json={"text": "hello"}, headers={"Idempotency-Key": key})
        assert replay.status_code == 202 and replay.headers["idempotent-replayed"] == "true"
        assert replay.json()["turn_id"] == turn["id"]
        assert provider.requests == []
        messages = (await client.get(f"/api/session/{session.id}/message")).json()
        assert len(messages) == 1 and messages[0]["role"] == "user"
        assert messages[0]["parts"][0]["text"].startswith("[Nexus recovery: interrupted turn]")
        assert "hello" not in messages[0]["parts"][0]["text"]  # no invented user turn
        turns = rt.store.turns_for_session(session.id)
        assert len(turns) == 1 and turns[0]["status"] == "interrupted"
        assert any(event["type"] == "turn.recovered"
                   for event in (await client.get(f"/api/session/{session.id}/event")).json()["events"])
    finally:
        await client.aclose()
        await rt.shutdown()


async def test_known_key_is_resolved_before_a_deleted_target(daemon):
    rt, client, _provider, workspace = daemon
    key = "create-key"
    body = {"directory": str(workspace)}
    first = await client.post("/api/session", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 200
    session = first.json()
    second = await client.post("/api/session", json=body, headers={"Idempotency-Key": key})
    assert second.headers["idempotent-replayed"] == "true" and second.json()["id"] == session["id"]
    assert len(rt.store.sessions()) == 1
    assert (await client.delete(f"/api/session/{session['id']}")).status_code == 204
    again = await client.post("/api/session", json=body, headers={"Idempotency-Key": key})
    assert again.status_code == 200 and again.json()["id"] == session["id"]
    assert (await client.get(f"/api/session/{session['id']}")).status_code == 404
    assert rt.store.sessions() == []


async def test_replaying_a_keyed_delete_does_not_require_the_target(daemon):
    _, client, _, workspace = daemon
    session = await _session(client, workspace)
    key = "delete-key"
    assert (await client.delete(f"/api/session/{session['id']}",
                                headers={"Idempotency-Key": key})).status_code == 204
    replay = await client.delete(f"/api/session/{session['id']}", headers={"Idempotency-Key": key})
    assert replay.status_code == 204 and replay.headers["idempotent-replayed"] == "true"
    other = await client.delete(f"/api/session/{session['id']}", headers={"Idempotency-Key": "another-key"})
    assert other.status_code == 404


async def test_a_failed_keyed_prompt_does_not_consume_the_key(daemon):
    rt, client, provider, workspace = daemon
    gate = asyncio.Event()
    provider.replies = [[Text("first")], [Text("second")]]
    provider.gates = [gate]
    session = await _session(client, workspace)
    key = "retry-after-busy"
    assert (await client.post(f"/api/session/{session['id']}/prompt", json={"text": "one"})).status_code == 202
    for _ in range(100):
        if session["id"] in rt.turns:
            break
        await asyncio.sleep(0.01)
    busy = await client.post(f"/api/session/{session['id']}/prompt", json={"text": "two"},
                             headers={"Idempotency-Key": key})
    assert busy.status_code == 409
    gate.set()
    await _idle(rt, session["id"])
    accepted = await client.post(f"/api/session/{session['id']}/prompt", json={"text": "two"},
                                 headers={"Idempotency-Key": key})
    assert accepted.status_code == 202 and "idempotent-replayed" not in accepted.headers
    await _idle(rt, session["id"])
    assert len(rt.store.turns_for_session(session["id"])) == 2


async def test_concurrent_identical_prompts_share_one_acceptance(daemon):
    rt, client, provider, workspace = daemon
    provider.replies = [[Text("once")]]
    session = await _session(client, workspace)
    key = "concurrent-key"
    body = {"text": "hello"}
    first, second = await asyncio.gather(
        client.post(f"/api/session/{session['id']}/prompt", json=body, headers={"Idempotency-Key": key}),
        client.post(f"/api/session/{session['id']}/prompt", json=body, headers={"Idempotency-Key": key}))
    assert {first.status_code, second.status_code} == {202}
    assert first.json()["turn_id"] == second.json()["turn_id"]
    assert {first.headers.get("idempotent-replayed"), second.headers.get("idempotent-replayed")} == {None, "true"}
    await _idle(rt, session["id"])
    assert len(provider.requests) == 1
    assert len(rt.store.turns_for_session(session["id"])) == 1
    assert [m["role"] for m in (await client.get(f"/api/session/{session['id']}/message")).json()] == [
        "user", "assistant"]


async def test_a_pending_external_effect_is_unknown_not_repeated(daemon):
    rt, client, _, workspace = daemon
    session = await _session(client, workspace)
    path = f"/api/session/{session['id']}/worktrees"
    body = {"action": "create", "path": str(workspace / "wt")}
    request = Request({"type": "http", "method": "POST", "path": path, "query_string": b"", "headers": []})
    fingerprint = _fingerprint(request, json.dumps(body).encode())
    key = "worktree-key"
    rt.begin_external_mutation(key, fingerprint, f"POST {path}", path)
    response = await client.post(path, json=body, headers={"Idempotency-Key": key})
    assert response.status_code == 409 and response.json()["code"] == "mutation_outcome_unknown"
    assert not (workspace / "wt").exists()
    # A restart converts the interrupted intent into the same stable, expiring
    # outcome; it never runs the Git operation again.
    assert rt.store.resolve_pending_mutations() == 1
    replay = await client.post(path, json=body, headers={"Idempotency-Key": key})
    assert replay.status_code == 409 and replay.headers["idempotent-replayed"] == "true"
    assert replay.json()["code"] == "mutation_outcome_unknown"
    assert not (workspace / "wt").exists()


async def test_an_expired_key_is_not_replayed_and_a_large_result_keeps_its_id(daemon):
    rt, _, _, _ = daemon
    rt.store.record_mutation("expired-key", "fp", "op", "/x", status=204, lifetime=-1)
    assert rt.replay_mutation("expired-key", "fp") is None
    entry = rt.store.record_mutation(
        "large-key", "fp2", "op", "/x", status=200, resource="ses_big",
        body={"id": "ses_big", "pad": "x" * (70 * 1024)}, lifetime=1000)
    assert entry["body"]["bounded"] is True and entry["body"]["id"] == "ses_big"
    assert entry["body"]["resource"] == "ses_big"


async def test_session_and_project_revisions_reject_stale_updates(daemon):
    _, client, _, workspace = daemon
    project = (await client.post("/api/project", json={"path": str(workspace)})).json()
    session = (await client.post("/api/session", json={"project": project["id"]})).json()
    assert session["revision"] == 1
    updated = await client.patch(f"/api/session/{session['id']}", json={"title": "new"},
                                 headers={"If-Match": "1"})
    assert updated.status_code == 200 and updated.json()["revision"] == 2
    stale = await client.patch(f"/api/session/{session['id']}", json={"title": "stale"},
                               headers={"If-Match": "1"})
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    assert stale.json()["revision"] == 2
    assert (await client.get(f"/api/session/{session['id']}")).json()["title"] == "new"

    key = "revision-replay"
    keyed = await client.post(f"/api/session/{session['id']}/model", json={"model": "scripted/a"},
                              headers={"If-Match": "2", "Idempotency-Key": key})
    assert keyed.status_code == 200 and keyed.json()["revision"] == 3
    later = await client.patch(f"/api/session/{session['id']}", json={"title": "later"},
                               headers={"If-Match": "3"})
    assert later.status_code == 200 and later.json()["revision"] == 4
    replay = await client.post(f"/api/session/{session['id']}/model", json={"model": "scripted/a"},
                               headers={"If-Match": "2", "Idempotency-Key": key})
    assert replay.status_code == 200 and replay.headers["idempotent-replayed"] == "true"
    assert replay.json()["revision"] == 3
    assert (await client.get(f"/api/session/{session['id']}")).json()["revision"] == 4

    renamed = await client.patch(f"/api/project/{project['id']}", json={"name": "renamed"},
                                 headers={"If-Match": "1"})
    assert renamed.status_code == 200 and renamed.json()["revision"] == 2
    stale_project = await client.patch(f"/api/project/{project['id']}", json={"name": "again"},
                                       headers={"If-Match": "1"})
    assert stale_project.status_code == 409 and stale_project.json()["code"] == "revision_conflict"


async def test_crash_after_create_before_response_journal_never_creates_again(daemon, monkeypatch):
    rt, client, _, workspace = daemon
    original = rt.finish_external_mutation

    def crash(*args, **kwargs):
        raise RuntimeError("crash before response journal")

    monkeypatch.setattr(rt, "finish_external_mutation", crash)
    with pytest.raises(RuntimeError, match="response journal"):
        await client.post("/api/session", json={"directory": str(workspace)},
                          headers={"Idempotency-Key": "crash-create"})
    assert len(rt.store.sessions()) == 1
    monkeypatch.setattr(rt, "finish_external_mutation", original)
    rt.store.resolve_pending_mutations()
    replay = await client.post("/api/session", json={"directory": str(workspace)},
                               headers={"Idempotency-Key": "crash-create"})
    assert replay.status_code == 409
    assert replay.json()["code"] == "mutation_outcome_unknown"
    assert len(rt.store.sessions()) == 1


async def test_precondition_is_part_of_the_keyed_request(daemon):
    _, client, _, workspace = daemon
    session = await _session(client, workspace)
    path = f"/api/session/{session['id']}"
    body = {"title": "changed"}
    first = await client.patch(path, json=body, headers={"Idempotency-Key": "revision-key", "If-Match": "1"})
    assert first.status_code == 200
    other = await client.patch(path, json=body, headers={"Idempotency-Key": "revision-key", "If-Match": "2"})
    assert other.status_code == 409 and other.json()["code"] == "idempotency_key_reused"
