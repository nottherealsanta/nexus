import asyncio

import httpx
import pytest

from nexus import runtime as runtime_module
from nexus.llmhub.types import Done, PartDone, Text, ToolCall
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


class Scripted:
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)

    def body(self, req):
        return {"model": req.model, "messages": len(req.messages), "tools": [t.name for t in req.tools],
                "instructions": req.instructions, "effort": req.reasoning_effort}

    async def stream(self, req):
        parts = self.replies.pop(0)
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    provider = Scripted([])
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime("scripted/m", Store(tmp_path / "nexus.db"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"authorization": f"Bearer {TOKEN}"})
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return rt, client, provider, workspace


async def _wait_idle(rt, session_id):
    for _ in range(200):
        if session_id not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("turn did not finish")


async def test_token_is_required(setup):
    _, client, _, _ = setup
    response = await client.get("/api/info", headers={"authorization": "Bearer wrong"})
    assert response.status_code == 401


async def test_projects_are_unique_by_path_and_own_sessions(setup):
    _, client, _, workspace = setup
    first = (await client.post("/api/project", json={"path": str(workspace)})).json()
    again = (await client.post("/api/project", json={"path": str(workspace / ".")})).json()
    assert first["id"] == again["id"] and first["name"] == "proj"
    session = (await client.post("/api/session", json={"project": first["id"]})).json()
    assert (session["directory"], session["agent"], session["model"], session["model_source"], session["status"], session["permission"]) == (
        str(workspace.resolve()), "build", "scripted/m", "default", "idle", "ask")
    listed = (await client.get("/api/session", params={"directory": str(workspace)})).json()
    assert [s["id"] for s in listed] == [session["id"]]
    assert (await client.post("/api/project", json={"path": str(workspace / "missing")})).status_code == 400
    assert (await client.get("/api/session/ses_nope")).status_code == 404


async def test_prompt_asks_permission_and_saves_messages(setup):
    rt, client, provider, workspace = setup
    provider.replies = [[ToolCall("c1", "write", '{"path": "x.txt", "content": "hi"}')], [Text("done")]]
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    events = []

    async def collect():
        async for event in rt.bus.subscribe():
            events.append(event)
            if event["type"] == "permission.asked":
                await client.post(f"/api/session/{session['id']}/permission/{event['data']['id']}", json={"reply": "allow"})
            if event["type"] == "session.status" and event["data"]["status"] == "idle":
                return

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0)
    assert (await client.post(f"/api/session/{session['id']}/prompt", json={"text": "make x.txt"})).status_code == 202
    await asyncio.wait_for(collector, 5)

    assert (workspace / "x.txt").read_text() == "hi"
    types = [e["type"] for e in events]
    assert types[:2] == ["session.updated", "session.status"]  # title from the first prompt, then busy
    assert "permission.asked" in types and "tool_end" in types and types[-1] == "session.status"
    messages = (await client.get(f"/api/session/{session['id']}/message")).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1]["parts"][0] == {"type": "tool_call", "id": "c1", "name": "write", "arguments": '{"path": "x.txt", "content": "hi"}', "origin": None, "raw": None}
    assert (await client.get(f"/api/session/{session['id']}")).json()["title"] == "make x.txt"
    context = (await client.get(f"/api/session/{session['id']}/context")).json()
    assert (context["model"], context["messages"], context["tools"]) == ("m", 4, ["read", "bash", "edit", "write"])
    assert context["instructions"].startswith("You are an AI agent running in Nexus") and "# Environment" in context["instructions"]


async def test_interrupt_closes_a_waiting_tool_call(setup):
    rt, client, provider, workspace = setup
    provider.replies = [[ToolCall("c1", "bash", '{"command": "true"}')]]
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "go"})
    for _ in range(100):
        if rt.pending_approvals(session["id"]):
            break
        await asyncio.sleep(0.01)
    assert (await client.post(f"/api/session/{session['id']}/prompt", json={"text": "again"})).status_code == 409
    assert (await client.post(f"/api/session/{session['id']}/interrupt")).status_code == 204
    await _wait_idle(rt, session["id"])
    messages = (await client.get(f"/api/session/{session['id']}/message")).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "tool"]
    assert messages[2]["parts"][0]["is_error"] is True


async def test_project_agent_sets_prompt_tools_model_and_effort(setup):
    _, client, _, workspace = setup
    (workspace / ".agents" / "agents").mkdir(parents=True)
    (workspace / ".agents" / "agents" / "review.md").write_text(
        "---\ndescription: Reviews code\nmodel: scripted/big\neffort: high\ntools: [read]\nsteps: 3\n---\nYou review code.\n")
    (workspace / ".agents" / "agents" / "broken.md").write_text("no frontmatter here")
    found = (await client.get("/api/agent", params={"directory": str(workspace)})).json()
    assert {a["name"]: a["source"] for a in found["agents"]} == {
        "build": "builtin", "orchestrate": "builtin", "plan": "builtin", "quick": "builtin", "scout": "builtin", "review": "project"}
    assert found["problems"][0]["path"].endswith("broken.md") and "frontmatter" in found["problems"][0]["error"]

    session = (await client.post("/api/session", json={"directory": str(workspace), "agent": "review"})).json()
    assert (session["model"], session["model_source"], session["effort"], session["effort_source"]) == ("scripted/big", "agent", "high", "agent")
    context = (await client.get(f"/api/session/{session['id']}/context")).json()
    assert (context["model"], context["tools"], context["effort"]) == ("big", ["read"], "high")
    assert context["instructions"].startswith("You review code.") and "Tools: read" in context["instructions"]

    session = (await client.post(f"/api/session/{session['id']}/model", json={"model": "scripted/small"})).json()
    assert (session["model"], session["model_source"]) == ("scripted/small", "session")
    session = (await client.post(f"/api/session/{session['id']}/model", json={"model": ""})).json()
    assert session["model_source"] == "agent"
    assert (await client.post(f"/api/session/{session['id']}/agent", json={"agent": "nope"})).status_code == 400
    assert (await client.post("/api/session", json={"directory": str(workspace), "agent": "nope"})).status_code == 400


async def test_session_events_are_stored_and_paged(setup):
    rt, client, provider, workspace = setup
    provider.replies = [[Text("hi")]]
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "hello"})
    await _wait_idle(rt, session["id"])
    page = (await client.get(f"/api/session/{session['id']}/event")).json()
    types = [e["type"] for e in page["events"]]
    assert types[0] == "session.created" and types[-1] == "session.status" and "part_done" in types and not page["more"]
    assert page["counts"]["message.added"] == 2
    first = (await client.get(f"/api/session/{session['id']}/event", params={"limit": 2})).json()
    rest = (await client.get(f"/api/session/{session['id']}/event", params={"after": first["events"][-1]["id"]})).json()
    assert first["more"] and [e["id"] for e in first["events"] + rest["events"]] == [e["id"] for e in page["events"]]
    only = (await client.get(f"/api/session/{session['id']}/event", params={"type": "done,usage"})).json()
    assert {e["type"] for e in only["events"]} == {"done"}
    await client.delete(f"/api/session/{session['id']}")
    assert rt.store.events(session["id"]) == []


async def test_inspector_page_is_static_and_locked_down(setup):
    _, client, _, _ = setup
    response = await client.get("/inspector", headers={"authorization": ""})
    assert response.status_code == 200 and "Session Inspector" in response.text
    assert TOKEN not in response.text
    assert "connect-src 'self'" in response.headers["content-security-policy"]
