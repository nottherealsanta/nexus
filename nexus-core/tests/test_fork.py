"""Fork sub agents: `session` with `agent: "fork"` copies the calling session's request prefix."""

import asyncio
import json
import sqlite3

import httpx
import pytest
from conftest import remembering

from nexus import agents as agent_files
from nexus import runtime as runtime_module
from nexus.codec import message_to_json
from nexus.llmhub.types import Done, PartDone, Text, ToolCall
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


class Scripted:
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def body(self, req):
        return {"instructions": req.instructions, "tools": [t.name for t in req.tools],
                "messages": [message_to_json(m) for m in req.messages]}

    async def stream(self, req):
        self.requests.append(req)
        parts = self.replies.pop(0)
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


def _call(call_id, name="fork", **args):
    args.pop("agent", None) if name == "fork" else None
    return ToolCall(call_id, name, json.dumps(args))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "README.md").write_text("hello\n")
    provider = Scripted([])
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    monkeypatch.setattr(runtime_module, "connected", lambda: ["codex"])
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "scripted/main"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    return rt, client, provider, workspace, tmp_path


async def _idle(rt):
    for _ in range(300):
        if not rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"still running: {list(rt.turns)}")


async def _parent(client, workspace):
    return (await client.post("/api/session", json={"directory": str(workspace), "agent": "build",
                                                    "effort": "high"})).json()


def _outputs(rt, session_id):
    return [p.output for m in rt.store.messages(session_id) if m.message.role == "tool" for p in m.message.parts]


async def test_fork_repeats_the_parents_request_prefix(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [
        [_call("c0", "ls", path=".")],
        [Text("forking"), _call("c1", agent="fork", prompt="check the readme")],
        [Text("fork report")],
        [Text("parent done")],
    ]
    parent = await _parent(client, workspace)
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
    await _idle(rt)

    [child] = (await client.get(f"/api/session/{parent['id']}/children")).json()
    assert child["fork"] == {"parent_id": parent["id"], "inherited": 3}
    assert (child["parent_id"], child["parent_call_id"], child["launched_by"], child["agent"]) == (
        parent["id"], "c1", "agent", "build")
    assert child["title"] == "check the readme"
    assert (await client.get(f"/api/session/{parent['id']}")).json()["fork"] is None

    calling, forked = provider.requests[1], provider.requests[2]
    # Same prefix byte for byte: instructions, tool definitions (`fork` included), model, effort, cache key.
    assert forked.instructions == calling.instructions
    assert forked.tools == calling.tools and "fork" in [t.name for t in forked.tools]
    assert (forked.model, forked.reasoning_effort) == (calling.model, calling.reasoning_effort) == ("main", "high")
    assert forked.conversation_id == calling.conversation_id == parent["id"]
    assert [message_to_json(m) for m in forked.messages[:3]] == [message_to_json(m) for m in calling.messages]
    task = forked.messages[3]
    assert task.role == "user" and len(forked.messages) == 4
    assert task.parts[0].text == runtime_module.FORK_MARKER.format(parent=parent["id"]) + "check the readme"

    # The message API returns the inherited messages first, in order, then the fork's own.
    parent_messages = (await client.get(f"/api/session/{parent['id']}/message")).json()
    fork_messages = (await client.get(f"/api/session/{child['id']}/message")).json()
    assert [m["parts"] for m in fork_messages[:3]] == [m["parts"] for m in parent_messages[:3]]
    assert fork_messages[3]["parts"][0]["text"].startswith("[Nexus fork] You are a fork of session ")
    assert fork_messages[-1]["parts"][0]["text"] == "fork report"

    output = next(o for o in _outputs(rt, parent["id"]) if o.startswith("Sub agent"))
    assert output.startswith(f"Sub agent {child['id']} (fork) finished.\n\nfork report")
    # The fork lists exactly the parent's tools as they were when it was taken.
    assert child["tools"] == [t.name for t in calling.tools]


async def test_fork_events_and_restart(env):
    rt, client, provider, workspace, tmp_path = env
    provider.replies = [[_call("c1", agent="fork", prompt="look", wait=False)], [Text("ok")], [Text("fork done")],
                        [Text("thanks")]]
    parent = await _parent(client, workspace)
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
    await _idle(rt)
    [child] = rt.store.children(parent["id"])
    created = [e["data"] for e in rt.store.events(child.id, limit=5000) if e["type"] == "session.created"]
    assert created and created[0]["fork"] == {"parent_id": parent["id"], "inherited": 1}
    # wait: false delivers the report as a notification, as for any child.
    notice = [m for m in rt.store.messages(parent["id"]) if m.message.role == "user"
              and m.message.parts[0].text.startswith("[Notification: sub agent finished]")]
    assert notice and "(fork) finished" in notice[0].message.parts[0].text

    # The fork record survives reopening the database.
    rt.store.close()
    reopened = Store(tmp_path / "nexus.db")
    assert reopened.session(child.id).fork_info() == {"parent_id": parent["id"], "inherited": 1}
    assert reopened.session(parent["id"]).fork_info() is None
    reopened.close()


async def test_a_fork_cannot_start_or_message_sessions(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [
        [_call("c1", agent="fork", prompt="try to recurse")],
        [_call("f1", agent="fork", prompt="deeper"), _call("f2", agent="quick", prompt="other")],
        [Text("could not")],
        [Text("parent done")],
    ]
    parent = await _parent(client, workspace)
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
    await _idle(rt)
    [child] = rt.store.children(parent["id"])
    assert rt.store.children(child.id) == []
    refused = [p for m in rt.store.messages(child.id)[1:] if m.message.role == "tool" for p in m.message.parts]
    assert len(refused) == 2 and all(p.is_error and "a fork cannot launch subagents or forks" in p.output for p in refused)
    with pytest.raises(runtime_module.ToolError, match="a fork cannot message other sessions"):
        rt._message_child(child.id, {"session": parent["id"], "message": "hi"})


async def test_fork_refused_when_the_parent_context_is_nearly_full(env):
    rt, client, provider, workspace, _ = env

    async def tiny(ref):
        return 2_000

    rt.window_for = tiny
    provider.replies = [[_call("c1", agent="fork", prompt="x" * 8_000)], [Text("ok")]]
    parent = await _parent(client, workspace)
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go " + "word " * 2_000})
    await _idle(rt)
    assert rt.store.children(parent["id"]) == []
    [output] = _outputs(rt, parent["id"])
    assert "cannot fork" in output and "named sub agent" in output


async def test_a_fork_keeps_its_agent(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [[_call("c1", agent="fork", prompt="look")], [Text("done")], [Text("ok")]]
    parent = await _parent(client, workspace)
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
    await _idle(rt)
    [child] = rt.store.children(parent["id"])
    with pytest.raises(ValueError, match="a fork keeps its parent's agent"):
        rt.update_session(child.id, agent="orchestrate")


def test_no_agent_file_may_be_named_fork(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    folder = tmp_path / "proj" / ".agents" / "agents"
    folder.mkdir(parents=True)
    (folder / "fork.md").write_text("---\ndescription: mine\n---\nHi.\n")
    found = agent_files.load(str(tmp_path / "proj"))
    assert "fork" not in found.agents
    assert any("reserved" in p["error"] for p in found.problems if p["path"].endswith("fork.md"))


def test_fork_column_is_added_to_an_existing_database(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    path = tmp_path / "nexus.db"
    Store(path).close()
    with sqlite3.connect(path) as db:
        db.execute("alter table session drop column fork")
    store = Store(path)
    assert "fork" in {r["name"] for r in store.db.execute("pragma table_info(session)")}
    store.close()
