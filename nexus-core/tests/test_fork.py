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
    assert fork_messages[3]["parts"][0]["text"].startswith("[Nexus fork] You are in a forked session of ")
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


async def test_a_fork_is_read_only(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [
        [_call("c1", agent="fork", prompt="try to write")],
        [_call("f1", "write", path="new.txt", content="x"),
         _call("f2", "edit", path="README.md", old_string="hello", new_string="bye"),
         _call("f3", "read", path="README.md")],
        [Text("reported")],
        [Text("parent done")],
    ]
    parent = await _parent(client, workspace)
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
    await _idle(rt)
    [child] = rt.store.children(parent["id"])
    parts = [p for m in rt.store.messages(child.id)[1:] if m.message.role == "tool" for p in m.message.parts]
    refused = [p for p in parts if p.is_error]
    assert len(refused) == 2 and all("a fork is read-only" in p.output for p in refused)
    assert any(not p.is_error and "hello" in p.output for p in parts)  # reading still works
    assert not (workspace / "new.txt").exists() and (workspace / "README.md").read_text() == "hello\n"


async def test_a_fork_has_a_small_step_budget(env, monkeypatch):
    rt, client, provider, workspace, _ = env
    monkeypatch.setattr(runtime_module, "FORK_MAX_STEPS", 3)
    provider.replies = [
        [_call("c1", agent="fork", prompt="loop")],
        [_call("f1", "ls", path=".")], [_call("f2", "ls", path=".")], [Text("out of steps")],
        [Text("parent done")],
    ]
    parent = await _parent(client, workspace)
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
    await _idle(rt)
    [child] = rt.store.children(parent["id"])
    texts = [p.text for m in rt.store.messages(child.id) if m.message.role == "user" for p in m.message.parts
             if isinstance(p, Text)]
    assert any(t.startswith("[Nexus notice: step limit]") for t in texts)
    assert provider.replies == []


async def test_fork_kernel_is_private_and_cancellation_does_not_destroy_parent(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [[_call("setup", "code", code="x = 41")], [Text("ready")]]
    parent = await _parent(client, workspace)
    try:
        await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "set up"})
        await _idle(rt)
        parent_kernel = rt.kernels.main[parent["id"]]
        provider.replies = [
            [_call("fork", prompt="inspect")],
            [_call("borrow", "code", kernel_id=parent_kernel, code="x = 99")],
            [_call("undefined", "code", code="print(x)")],
            [_call("own", "code", code="x = 999")],
            [_call("timeout", "code", code="await bash(command='sleep 5')", timeout_s=0.05)],
            [Text("partial report")],
            [_call("verify", "code", code="print(x)")],
            [Text("parent done")],
        ]
        await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "check"})
        await _idle(rt)
        [child] = rt.store.children(parent["id"])
        outputs = _outputs(rt, child.id)
        assert any("cannot borrow" in o for o in outputs)
        assert any("NameError" in o for o in outputs)
        assert any("kernel discarded" in o for o in outputs)
        assert "41" in _outputs(rt, parent["id"])[-1]
        assert parent_kernel in rt.kernels.items and child.id not in rt.kernels.main
    finally:
        await rt.kernels.shutdown()


async def test_fork_total_call_budget_counts_nested_calls(env, monkeypatch):
    rt, client, provider, workspace, _ = env
    monkeypatch.setattr(runtime_module, "FORK_MAX_CALLS", 3)
    provider.replies = [
        [_call("fork", prompt="inspect")],
        [_call("batch", "code", code="for _ in range(3):\n print(await read(path='README.md'))")],
        [Text("partial report")], [Text("parent done")],
    ]
    parent = await _parent(client, workspace)
    try:
        await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
        await _idle(rt)
        [child] = rt.store.children(parent["id"])
        assert "3-call tool budget" in _outputs(rt, child.id)[0]
        child_final = provider.requests[2]
        assert not child_final.tools
        assert any("honest partial report" in p.text for m in child_final.messages for p in m.parts if isinstance(p, Text))
    finally:
        await rt.kernels.shutdown()


async def test_fork_refuses_job_control_and_unknown_extension_effects(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [[_call("fork", prompt="inspect")], [Text("report")], [Text("done")]]
    parent = await _parent(client, workspace)
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
    await _idle(rt)
    [child] = rt.store.children(parent["id"])
    for name, args in [("bash_kill", {"job_id": "parent-job"}), ("bash", {"command": "true", "background": True})]:
        with pytest.raises(runtime_module.ToolError, match="refused"):
            await rt._approve(child.id, ToolCall("check", name, json.dumps(args)), args)
    snapshot = rt.snapshot_for(child)
    snapshot["extensions"] = {"custom": {"extension": {}}}
    rt.store.set_snapshot(child.id, snapshot)
    with pytest.raises(runtime_module.ToolError, match="READ_ONLY"):
        await rt._approve(child.id, ToolCall("custom", "custom", "{}"), {})


async def test_declared_read_only_extensions_work_directly_and_nested_from_frozen_source(env):
    rt, client, provider, workspace, _ = env
    folder = workspace / ".agents" / "tools"
    folder.mkdir(parents=True)
    path = folder / "custom.py"
    path.write_text('DESCRIPTION = "read only"\nREAD_ONLY = True\ndef run(args):\n return "frozen read result"\n')
    parent = await _parent(client, workspace)
    path.write_text('DESCRIPTION = "changed"\nREAD_ONLY = False\ndef run(args):\n return "changed result"\n')
    provider.replies = [
        [_call("fork", prompt="inspect")],
        [_call("direct", "custom")],
        [_call("nested", "code", code="print(await custom())")],
        [Text("report")], [Text("done")],
    ]
    try:
        await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
        await _idle(rt)
        [child] = rt.store.children(parent["id"])
        outputs = _outputs(rt, child.id)
        assert len(outputs) == 2 and all("frozen read result" in output for output in outputs)
        assert all("changed result" not in output for output in outputs)
    finally:
        await rt.kernels.shutdown()


async def test_fork_deadline_stops_nested_work_once_and_preserves_parent(env, monkeypatch):
    rt, client, provider, workspace, _ = env
    parent = await _parent(client, workspace)
    provider.replies = [[_call("setup", "code", code="x=41")], [Text("ready")]]
    try:
        await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "setup"})
        await _idle(rt)
        parent_kernel = rt.kernels.main[parent["id"]]
        monkeypatch.setattr(runtime_module, "FORK_MAX_SECONDS", 0.15)
        provider.replies = [
            [_call("fork", prompt="inspect")],
            [_call("cell", "code", code="await bash(command='sleep 5')")],
            [Text("partial report")], [Text("parent done")],
        ]
        await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
        await _idle(rt)
        [child] = rt.store.children(parent["id"])
        records = [e["data"] for e in rt.store.events(child.id, limit=5000) if e["type"] == "code.call"]
        assert len(records) == 1 and records[0]["error"]
        assert child.id not in rt.kernels.main and parent_kernel in rt.kernels.items
        assert not provider.requests[-2].tools
    finally:
        await rt.kernels.shutdown()
