"""Handoff, automatic titles and /btw (PLAN K1-K3), offline with a scripted provider."""
import asyncio

import httpx
import pytest

from nexus import oneshot
from nexus import runtime as runtime_module
from nexus.llmhub.types import Done, Message, PartDone, ProviderError, Text, TextDelta, ToolCall, ToolResult, Usage
from nexus.runtime import Runtime
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


def _last_user(req) -> str:
    return "".join(p.text for p in req.messages[-1].parts if isinstance(p, Text))


class Fake:
    name = "fake"

    def __init__(self):
        self.requests = []
        self.input_tokens = 10

    def body(self, req):
        return {"model": req.model, "messages": len(req.messages), "tools": [t.name for t in req.tools]}

    async def stream(self, req):
        self.requests.append(req)
        last = _last_user(req)
        if last.startswith("Write a title"):
            reply = '"Fix The Login Bug."'
        elif "handoff now" in last or "Write that handoff" in last:
            reply = "## Goal\nFix login\n## Next steps\nRun the tests"
        elif "side question" in last:
            reply = "It is blue."
        else:
            reply = "done"
        yield TextDelta(reply)
        yield PartDone(Text(reply))
        if "side question" in last:  # a tool call here must be ignored, not run
            yield PartDone(ToolCall("x1", "bash", '{"command": "echo no"}'))
        yield Usage(input_tokens=self.input_tokens, output_tokens=5)
        yield Done("end_turn")


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    provider = Fake()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime("fake/m", Store(tmp_path / "nexus.db"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    yield rt, client, provider, workspace
    await client.aclose()
    await rt.shutdown()
    rt.store.close()


async def _session(client, workspace):
    response = await client.post("/api/session", json={"directory": str(workspace)})
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _turn(rt, client, sid, text="please fix the login bug"):
    assert (await client.post(f"/api/session/{sid}/prompt", json={"text": text})).status_code == 202
    for _ in range(500):
        if sid not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("turn did not finish")


async def _until(predicate, what):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(what)


def test_settled_cuts_at_the_last_complete_tool_call():
    call = Message("assistant", (ToolCall("a", "read", "{}"), ToolCall("b", "read", "{}")))
    both = Message("tool", (ToolResult("a", "x"), ToolResult("b", "y")))
    user = Message("user", (Text("hi"),))
    assert oneshot.settled([user, call, both]) == [user, call, both]
    assert oneshot.settled([user, call]) == [user]
    assert oneshot.settled([user, call, Message("tool", (ToolResult("a", "x"),))]) == [user]
    assert oneshot.settled([]) == []


def test_clean_title():
    assert oneshot.clean_title('Title: "Fix The Login Bug."\nmore') == "Fix The Login Bug"
    assert oneshot.clean_title("# One two three four five six seven eight nine ten") == "One two three four five six seven eight"
    assert oneshot.clean_title("") == ""


async def test_btw_answers_without_changing_the_context(env):
    rt, client, provider, workspace = env
    sid = await _session(client, workspace)
    await _turn(rt, client, sid)
    before = (await client.get(f"/api/session/{sid}/message")).json()
    provider.requests.clear()
    response = await client.post(f"/api/session/{sid}/btw", json={"question": "what colour is the sky?"})
    assert response.status_code == 200, response.text
    assert response.json()["answer"] == "It is blue."
    request = provider.requests[0]
    assert _last_user(request).endswith("Question: what colour is the sky?")
    assert [m.role for m in request.messages] == ["user", "assistant", "user"]
    assert (await client.get(f"/api/session/{sid}/message")).json() == before
    events = (await client.get(f"/api/session/{sid}/event")).json()["events"]
    assert [e["data"]["kind"] for e in events if e["type"] == "oneshot"] == ["btw"]
    assert (await client.post(f"/api/session/{sid}/btw", json={"question": " "})).status_code == 400


async def test_handoff_starts_a_continued_root_session(env):
    rt, client, _provider, workspace = env
    sid = await _session(client, workspace)
    assert (await client.patch(f"/api/session/{sid}", json={"agent": "orchestrate"})).status_code == 200
    await _turn(rt, client, sid)
    before = (await client.get(f"/api/session/{sid}/message")).json()
    response = await client.post(f"/api/session/{sid}/handoff")
    assert response.status_code == 201, response.text
    data = response.json()
    new = data["session"]
    assert new["id"] != sid and new["continued_from"] == sid and new["parent_id"] is None
    assert new["agent"] == "orchestrate" and new["directory"] == str(workspace)
    messages = (await client.get(f"/api/session/{new['id']}/message")).json()
    assert len(messages) == 1 and messages[0]["delivery"] == "handoff"
    assert messages[0]["parts"][0]["text"].startswith(f"[Handoff from session {sid}]")
    assert "## Next steps" in messages[0]["parts"][0]["text"]
    assert (await client.get(f"/api/session/{sid}/message")).json() == before  # sent context is never edited
    events = (await client.get(f"/api/session/{sid}/event")).json()["events"]
    assert [e["data"]["to"] for e in events if e["type"] == "session.handoff"] == [new["id"]]
    assert (await client.get(f"/api/session/{new['id']}")).json()["continued_from"] == sid
    assert (await client.get(f"/api/session/{sid}")).json()["continued_to"] == new["id"]
    assert new["continued_to"] is None
    assert (await client.get(f"/api/session/{new['id']}/turn")).json()["floor"] is None  # the summary is not a lost turn
    await _turn(rt, client, new["id"], "carry on")
    assert (await client.get(f"/api/session/{new['id']}/turn")).json()["floor"] is None


async def test_handoff_refusals(env):
    rt, client, _provider, workspace = env
    sid = await _session(client, workspace)
    assert (await client.post(f"/api/session/{sid}/handoff")).status_code == 400  # nothing to hand off
    rt.turns[sid] = asyncio.ensure_future(asyncio.sleep(5))
    assert (await client.post(f"/api/session/{sid}/handoff")).status_code == 409  # busy
    rt.turns.pop(sid).cancel()


async def test_first_prompt_gets_a_generated_title(env, monkeypatch):
    rt, client, provider, workspace = env
    monkeypatch.setattr(Runtime, "background_requests", True)
    sid = await _session(client, workspace)
    await _turn(rt, client, sid)
    await _until(lambda: rt.session(sid).title == "Fix The Login Bug", "title was not generated")
    title_request = next(r for r in provider.requests if _last_user(r).startswith("Write a title"))
    assert title_request.tools == ()  # the title call carries no tools


async def test_renamed_session_keeps_its_name(env, monkeypatch):
    rt, client, provider, workspace = env
    monkeypatch.setattr(Runtime, "background_requests", True)
    sid = await _session(client, workspace)
    gate = asyncio.Event()
    original = provider.stream

    async def slow(req):
        if _last_user(req).startswith("Write a title"):
            await gate.wait()
        async for event in original(req):
            yield event

    monkeypatch.setattr(provider, "stream", slow)
    await _turn(rt, client, sid)
    await client.patch(f"/api/session/{sid}", json={"title": "Mine"})
    gate.set()
    await asyncio.sleep(0.2)
    assert rt.session(sid).title == "Mine"


async def test_auto_handoff_after_a_turn_that_fills_the_window(env, monkeypatch):
    rt, client, provider, workspace = env
    monkeypatch.setattr(Runtime, "background_requests", True)

    async def window(model):
        return 100

    rt.window_for = window
    provider.input_tokens = 85
    sid = await _session(client, workspace)
    await client.patch(f"/api/session/{sid}", json={"title": "Keep"})
    await _turn(rt, client, sid)
    await _until(lambda: len(rt.store.sessions()) == 2, "no handoff session appeared")
    new = next(s for s in rt.store.sessions() if s.id != sid)
    assert new.continued_from == sid
    assert [m.delivery for m in rt.store.messages(new.id)] == ["handoff"]

    provider.input_tokens = 40  # under the threshold: no further handoff from the new session
    await _turn(rt, client, new.id)
    await asyncio.sleep(0.2)
    assert len(rt.store.sessions()) == 2


async def test_auto_handoff_can_be_turned_off(env, monkeypatch):
    rt, client, provider, workspace = env
    monkeypatch.setattr(Runtime, "background_requests", True)
    await client.patch("/api/config", json={"expected_revision": (await client.get("/api/config")).json()["revision"],
                                            "set": {"context.handoff_percent": 0}})

    async def window(model):
        return 100

    rt.window_for = window
    provider.input_tokens = 99
    sid = await _session(client, workspace)
    await client.patch(f"/api/session/{sid}", json={"title": "Keep"})
    await _turn(rt, client, sid)
    await asyncio.sleep(0.2)
    assert len(rt.store.sessions()) == 1


async def _set_title_models(client, refs):
    revision = (await client.get("/api/config")).json()["revision"]
    response = await client.patch("/api/config", json={"expected_revision": revision, "set": {"context.title_models": refs}})
    assert response.status_code == 200, response.text


async def test_title_falls_back_through_the_model_list(env, monkeypatch):
    rt, client, provider, workspace = env
    monkeypatch.setattr(Runtime, "background_requests", True)

    class Broken(Fake):
        async def stream(self, req):
            raise ProviderError("down", status=500)
            yield

    broken = Broken()
    monkeypatch.setattr(runtime_module, "provider_for",
                        lambda ref: ((broken if ref.startswith("copilot/") else provider), ref.partition("/")[2]))
    await _set_title_models(client, ["copilot/gpt-6-luna", "codex/gpt-6-luna"])
    sid = await _session(client, workspace)
    await _turn(rt, client, sid)
    await _until(lambda: rt.session(sid).title == "Fix The Login Bug", "fallback model was not used")
    events = (await client.get(f"/api/session/{sid}/event")).json()["events"]
    assert [e["data"]["model"] for e in events if e["type"] == "oneshot"] == ["codex/gpt-6-luna"]


async def test_title_keeps_first_words_when_every_model_fails(env, monkeypatch):
    rt, client, provider, workspace = env
    monkeypatch.setattr(Runtime, "background_requests", True)

    class Broken(Fake):
        async def stream(self, req):
            raise ProviderError("down", status=500)
            yield

    broken = Broken()
    monkeypatch.setattr(runtime_module, "provider_for",
                        lambda ref: ((broken if ref.startswith("copilot/") else provider), ref.partition("/")[2]))
    await _set_title_models(client, ["copilot/gpt-6-luna", "copilot/other"])
    sid = await _session(client, workspace)
    await _turn(rt, client, sid, "please fix the login bug")
    await asyncio.sleep(0.2)
    assert rt.session(sid).title == "please fix the login bug"


async def test_deleting_the_original_clears_the_link_back(env):
    rt, client, _provider, workspace = env
    sid = await _session(client, workspace)
    await _turn(rt, client, sid)
    new = (await client.post(f"/api/session/{sid}/handoff")).json()["session"]
    assert (await client.delete(f"/api/session/{sid}")).status_code in (200, 204)
    assert (await client.get(f"/api/session/{new['id']}")).json()["continued_from"] is None


async def test_draft_context_matches_the_first_request_and_stores_nothing(env):
    rt, client, provider, workspace = env
    before = (await client.get("/api/session")).json()
    draft = await client.post("/api/draft/context", json={"directory": str(workspace), "agent": "build"})
    assert draft.status_code == 200, draft.text
    data = draft.json()
    assert data["messages"] == 0 and data["exact"] is False and data["used"] > 0
    assert data["model"] == "fake/m" and data["agent"] == "build"
    assert data["snapshot"]["agent"]["name"] == "build"
    assert data["parts"]["instructions"] > 0 and data["parts"]["tools"] > 0
    assert (await client.get("/api/session")).json() == before  # nothing was created

    off = await client.post("/api/draft/context", json={"directory": str(workspace), "disabled_tools": ["bash"]})
    assert "bash" in data["tools"] and "bash" not in off.json()["tools"]
    assert off.json()["used"] < data["used"]

    sid = await _session(client, workspace)
    await _turn(rt, client, sid)
    first = provider.requests[0]
    assert first.instructions and list(data["tools"]) == [t.name for t in first.tools]
