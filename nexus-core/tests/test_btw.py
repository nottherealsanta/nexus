import asyncio
import json
from types import SimpleNamespace

import pytest
import test_fork
from test_fork import _call, _idle, _outputs, _parent

from nexus.llmhub.types import Text, ToolCall
from nexus.runtime import BTW_MARKER, Conflict
from nexus.tools import ToolError

env = test_fork.env


async def test_btw_is_hidden_and_keeps_the_parent_prefix_and_context(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [[Text("parent answer")], [_call("read", "read", path="README.md")], [Text("hello")]]
    parent = await _parent(client, workspace)
    sid = parent["id"]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "hello"})
    await _idle(rt)
    before = rt.context(sid)
    messages = rt.store.messages(sid)
    r = await client.post(f"/api/session/{sid}/btw", json={"question": "what does the readme say?"})
    assert r.status_code == 200, r.text
    child = rt.session(r.json()["session"])
    await _idle(rt)
    assert child.kind == "btw" and child.parent_id is None
    assert child.directory == parent["directory"]
    assert child.id not in {s["id"] for s in (await client.get("/api/session")).json()}
    assert rt.store.messages(sid) == messages
    assert rt.context(sid) == before
    base, side = provider.requests[0], provider.requests[1]
    assert side.instructions == base.instructions
    assert side.tools == base.tools
    assert side.model == base.model and side.reasoning_effort == base.reasoning_effort
    assert side.conversation_id == base.conversation_id
    inherited = tuple(m.message for m in messages)
    assert side.messages[:len(inherited)] == inherited
    assert side.messages[-1].parts[0].text.startswith(BTW_MARKER)
    assert "hello" in _outputs(rt, child.id)[0]
    assert sid not in rt.notify


async def test_btw_refuses_the_eleventh_call_in_one_batch(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [[_call(str(i), "ls", path=".") for i in range(12)], [Text("done")]]
    parent = await _parent(client, workspace)
    r = await client.post(f"/api/session/{parent['id']}/btw", json={"question": "look around"})
    await _idle(rt)
    outputs = _outputs(rt, r.json()["session"])
    assert len(outputs) == 12
    assert all("README.md" in out for out in outputs[:10])
    assert all("10-call" in out and "Answer now" in out for out in outputs[10:])


@pytest.mark.parametrize("name,args", [
    ("edit", {"path": "README.md", "old_string": "hello", "new_string": "bad"}),
    ("write", {"path": "README.md", "content": "bad"}),
    ("fork", {"prompt": "bad"}),
    ("session", {"agent": "quick", "prompt": "bad"}),
    ("message_session", {"session": "bad", "message": "bad"}),
    ("expose_workspace_tools", {}),
    ("bash", {"command": "touch bad"}),
    ("ask", {"questions": []}),
])
async def test_btw_hard_refusals(env, name, args):
    rt, client, provider, workspace, _ = env
    provider.replies = [[Text("done")]]
    parent = await _parent(client, workspace)
    r = await client.post(f"/api/session/{parent['id']}/btw", json={"question": "hello"})
    await _idle(rt)
    child = rt.session(r.json()["session"])
    with pytest.raises(ToolError, match="refused"):
        await rt._approve(child.id, ToolCall("bad", name, json.dumps(args)), args)
    assert (workspace / "README.md").read_text() == "hello\n"
    assert rt.store.messages(parent["id"]) == []


async def test_btw_nested_code_calls_share_the_budget(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [[_call("code", "code", code='for i in range(12):\n    print(await ls(path="."))')], [Text("done")]]
    parent = await _parent(client, workspace)
    r = await client.post(f"/api/session/{parent['id']}/btw", json={"question": "check"})
    await _idle(rt)
    child = rt.session(r.json()["session"])
    assert child.fork_info()["calls"] == 10
    assert "10-call" in _outputs(rt, child.id)[0]


async def test_btw_validation(env):
    rt, client, _, workspace, _ = env
    parent = await _parent(client, workspace)
    assert (await client.post(f"/api/session/{parent['id']}/btw", json={"question": " "})).status_code == 400
    assert rt.store.sessions(kind="btw") == []


async def test_btw_while_parent_waits_uses_only_the_settled_prefix(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [[_call("pending", "bash", command="echo waiting")], [Text("side answer")]]
    parent = await _parent(client, workspace)
    sid = parent["id"]
    rt.update_session(sid, permission="ask")
    await client.post(f"/api/session/{sid}/prompt", json={"text": "start"})
    for _ in range(100):
        if rt.approvals:
            break
        await asyncio.sleep(0.01)
    assert rt.approvals
    before = rt.store.messages(sid)
    r = await client.post(f"/api/session/{sid}/btw", json={"question": "quick question"})
    child_id = r.json()["session"]
    await rt.turns[child_id]
    assert rt.store.messages(sid) == before
    assert len(provider.requests[1].messages) == 2  # user prompt and side-question marker, no pending call
    assert sid in rt.turns
    assert rt.store.children(sid) == []
    await rt.interrupt(sid)


async def test_btw_can_be_cancelled_without_interrupting_the_parent(env, monkeypatch):
    rt, client, provider, workspace, _ = env
    gate = asyncio.Event()
    async def stream(req):
        await gate.wait()
        yield Text("unreachable")
    monkeypatch.setattr(provider, "stream", stream)
    parent = await _parent(client, workspace)
    rt.update_session(parent["id"], permission="ask")
    r = await client.post(f"/api/session/{parent['id']}/btw", json={"question": "where?"})
    child_id = r.json()["session"]
    await asyncio.sleep(0.01)
    assert (await client.post(f"/api/session/{child_id}/interrupt")).status_code == 204
    assert child_id not in rt.turns
    assert rt.store.messages(parent["id"]) == []


async def test_btw_checkpoint_has_canonical_context_and_correlates_the_run(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [[_call("read1", "read", path="README.md")], [Text("answer")]]
    parent = await _parent(client, workspace)
    r = await client.post(f"/api/session/{parent['id']}/btw", json={"question": "read it", "request_id": "ui-token"})
    assert r.json()["request_id"] == "ui-token"
    assert json.loads(r.json()["inherited_context"])["messages"] == []
    await _idle(rt)
    child = r.json()["session"]
    state = (await client.get(f"/api/session/{child}/btw")).json()
    assert not state["running"] and state["calls"] == 1
    assert [m["role"] for m in state["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert state["messages"][0]["parts"][0]["text"].startswith(BTW_MARKER)
    assert state["messages"][2]["parts"][0]["output"]
    assert (await client.get(f"/api/session/{parent['id']}/btw")).status_code == 400
    assert (await client.get(f"/api/session/{child}")).json()["kind"] == "btw"
    assert (await client.get("/api/search", params={"q": "answer"})).json()["hits"] == []


@pytest.mark.parametrize("read_only", [False, True])
async def test_btw_mcp_requires_a_read_only_annotation(env, monkeypatch, read_only):
    rt, client, provider, workspace, _ = env
    provider.replies = [[Text("done")]]
    parent = await _parent(client, workspace)
    r = await client.post(f"/api/session/{parent['id']}/btw", json={"question": "hello"})
    await _idle(rt)
    child = r.json()["session"]
    scope = SimpleNamespace(approval=lambda target: ("mcp__server__tool", read_only))
    monkeypatch.setattr(rt, "_mcp_scope", lambda session, server: (scope, "global"))
    args = {"tool": "server/tool", "arguments": {}}
    if read_only:
        assert await rt._approve(child, ToolCall("mcp1", "mcp_call", json.dumps(args)), args)
    else:
        with pytest.raises(ToolError, match="read-only"):
            await rt._approve(child, ToolCall("mcp1", "mcp_call", json.dumps(args)), args)


async def test_deleting_source_session_cleans_up_side_runs(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [[Text("done")]]
    parent = await _parent(client, workspace)
    r = await client.post(f"/api/session/{parent['id']}/btw", json={"question": "hello"})
    await _idle(rt)
    child = r.json()["session"]
    await rt.delete_session(parent["id"])
    assert rt.store.session(child) is None


async def test_project_removal_checks_side_runs_and_deletion_interrupts_them(env, monkeypatch):
    rt, client, provider, workspace, _ = env
    gate = asyncio.Event()
    async def stream(req):
        await gate.wait()
        yield Text("unreachable")
    monkeypatch.setattr(provider, "stream", stream)
    parent = await _parent(client, workspace)
    r = await client.post(f"/api/session/{parent['id']}/btw", json={"question": "hello"})
    await asyncio.sleep(0.01)
    with pytest.raises(Conflict, match="running session"):
        rt.remove_project(parent["project_id"])
    await rt.delete_project(parent["project_id"])
    assert r.json()["session"] not in rt.turns
    assert rt.store.session(r.json()["session"]) is None


async def test_btw_allows_a_safe_shell_command_but_no_command_hooks(env):
    rt, client, provider, workspace, _ = env
    provider.replies = [[_call("pwd1", "bash", command="pwd")], [Text("done")]]
    parent = await _parent(client, workspace)
    rt.update_session(parent["id"], permission="ask")
    r = await client.post(f"/api/session/{parent['id']}/btw", json={"question": "where?"})
    await _idle(rt)
    child = rt.session(r.json()["session"])
    assert str(workspace) in _outputs(rt, child.id)[0]
    assert rt._hook_runner(child, workspace) is None
