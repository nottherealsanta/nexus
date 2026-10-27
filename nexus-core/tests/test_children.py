import asyncio
import json

import httpx
import pytest

from nexus import runtime as runtime_module
from nexus.llmhub.types import Done, PartDone, Text, ToolCall
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


class Scripted:
    """Replays scripted replies; `gate` (if set) must open before each reply."""

    name = "scripted"

    def __init__(self, replies, gate=None):
        self.replies = list(replies)
        self.gate = gate
        self.requests = []

    def body(self, req):
        return {"tools": [t.name for t in req.tools]}

    async def stream(self, req):
        self.requests.append(req)
        if self.gate is not None:
            await self.gate.wait()
        parts = self.replies.pop(0)
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


def _call(call_id, **args):
    return ToolCall(call_id, "session", json.dumps(args))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "proj"
    (workspace / ".agents" / "agents").mkdir(parents=True)
    # The child agent prefers its own model, so parent and child replies never interleave.
    (workspace / ".agents" / "agents" / "helper.md").write_text(
        "---\ndescription: Helps\nmodel: scripted/child\ncontrol: [session]\n---\nYou help.\n")
    providers = {"parent": Scripted([]), "child": Scripted([])}
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (providers[ref.partition("/")[2]], ref.partition("/")[2]))
    rt = make_runtime("scripted/parent", Store(tmp_path / "nexus.db"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"authorization": f"Bearer {TOKEN}"})
    return rt, client, providers, workspace


async def _idle(rt):
    for _ in range(300):
        if not rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"still running: {list(rt.turns)}")


async def _texts(client, session_id):
    messages = (await client.get(f"/api/session/{session_id}/message")).json()
    return [(m["role"], p.get("text") or p.get("output")) for m in messages for p in m["parts"]]


async def test_blocking_child_answers_the_parent_tool_call(env):
    rt, client, providers, workspace = env
    providers["parent"].replies = [[_call("c1", agent="helper", prompt="find the answer", title="Find it")], [Text("parent done")]]
    providers["child"].replies = [[Text("42")]]
    parent = (await client.post("/api/session", json={"directory": str(workspace), "permission": "allow", "agent": "orchestrate"})).json()
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
    await _idle(rt)

    [child] = (await client.get(f"/api/session/{parent['id']}/children")).json()
    assert (child["parent_id"], child["parent_call_id"], child["launched_by"], child["agent"], child["title"]) == (
        parent["id"], "c1", "agent", "helper", "Find it")
    assert (child["directory"], child["permission"]) == (parent["directory"], "allow")
    tool_output = next(t for role, t in await _texts(client, parent["id"]) if role == "tool")
    assert tool_output.startswith(f"Child session {child['id']} (agent helper) finished.") and tool_output.endswith("42")
    # A child never gets the session tool, even when its agent lists it.
    assert [t.name for t in providers["child"].requests[0].tools] == ["read", "bash", "bash_output", "bash_kill", "edit", "write", "code"]
    tree = (await client.get(f"/api/session/{child['id']}/tree")).json()
    assert tree["id"] == parent["id"] and [c["id"] for c in tree["children"]] == [child["id"]]


async def test_non_blocking_child_wakes_the_idle_parent(env):
    rt, client, providers, workspace = env
    gate = asyncio.Event()
    providers["child"] = Scripted([[Text("found it")]], gate=gate)
    providers["parent"].replies = [[_call("c1", agent="helper", prompt="look", wait=False)], [Text("waiting")], [Text("thanks")]]
    parent = (await client.post("/api/session", json={"directory": str(workspace), "agent": "orchestrate"})).json()
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
    for _ in range(100):
        if parent["id"] not in rt.turns:
            break
        await asyncio.sleep(0.01)
    assert parent["id"] not in rt.turns and rt.turns  # the parent is idle, the child still runs

    gate.set()
    await _idle(rt)
    texts = await _texts(client, parent["id"])
    assert next(t for role, t in texts if role == "tool").startswith("Started child session")
    notice = [t for role, t in texts if role == "user" and t.startswith("[Notification: child session finished]")]
    assert notice and notice[0].endswith("found it")
    assert texts[-1] == ("assistant", "thanks")


async def test_interrupting_the_parent_stops_a_waiting_child(env):
    rt, client, providers, workspace = env
    providers["child"] = Scripted([[Text("never")]], gate=asyncio.Event())
    providers["parent"].replies = [[_call("c1", agent="helper", prompt="slow")]]
    parent = (await client.post("/api/session", json={"directory": str(workspace), "agent": "orchestrate"})).json()
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
    for _ in range(100):
        if len(rt.turns) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(rt.turns) == 2
    await client.post(f"/api/session/{parent['id']}/interrupt")
    await _idle(rt)
    tool_output = next(t for role, t in await _texts(client, parent["id"]) if role == "tool")
    assert "interrupted" in tool_output


async def test_events_for_a_session_include_its_children(env):
    rt, _, _, workspace = env
    project = rt.open_project(str(workspace))
    parent = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=None, permission=None, parent=parent)
    other = rt.create_session(project, title=None, model=None, permission=None)
    assert rt.in_tree(child.id, parent.id) and rt.in_tree(parent.id, parent.id)
    assert not rt.in_tree(other.id, parent.id) and not rt.in_tree(parent.id, child.id)


async def test_only_orchestrate_offers_the_tool_and_only_child_agents_are_listed(env):
    rt, client, _, workspace = env
    build = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    assert "session" not in (await client.get(f"/api/session/{build['id']}/context")).json()["tools"]
    orchestrate = rt.session((await client.post("/api/session", json={"directory": str(workspace), "agent": "orchestrate"})).json()["id"])
    tool = rt._session_tool(orchestrate)
    assert tool.spec.parameters["properties"]["agent"]["enum"] == ["build", "plan", "quick", "scout", "helper"]
    # Root sessions may only run primary/all agents; children only subagent/all.
    assert (await client.post("/api/session", json={"directory": str(workspace), "agent": "scout"})).status_code == 400
    bad_child = await client.post("/api/session", json={"parent": orchestrate.id, "agent": "orchestrate"})
    assert bad_child.status_code == 400 and "cannot run as a child" in bad_child.json()["error"]
    assert (await client.post(f"/api/session/{build['id']}/agent", json={"agent": "plan"})).status_code == 400


async def test_dynamic_child_guidance_and_default_root(env):
    rt, client, _, workspace = env
    (workspace / '.agents/agents/build.md').write_text('---\ndescription: Custom builder\nmode: subagent\n---\nBuild things.')
    parent = (await client.post('/api/session', json={'directory': str(workspace)})).json()
    assert parent['agent'] == 'orchestrate'
    assert 'build' not in [a.name for a in rt.agents(str(workspace)).allowed(child=False)]
    tool = rt._session_tool(rt.session(parent['id']))
    assert 'build' in tool.spec.parameters['properties']['agent']['enum']
    assert '`helper`: Helps' in tool.guidance and '`build`: Custom builder' in tool.guidance
    assert tool.guidance in rt._agent(rt.session(parent['id'])).instructions
    patched = (await client.patch(f"/api/session/{parent['id']}", json={'disabled_tools': ['session']})).json()
    assert 'session' not in patched['tools']
    assert 'Child agents:' not in rt._agent(rt.session(parent['id'])).instructions
    assert '- `session`:' not in rt._agent(rt.session(parent['id'])).instructions


async def test_children_inherit_disabled_tools(env):
    rt, client, providers, workspace = env
    parent = (await client.post('/api/session', json={
        'directory': str(workspace), 'agent': 'orchestrate', 'disabled_tools': ['bash', 'write']})).json()
    user_child = (await client.post('/api/session', json={'parent': parent['id'], 'agent': 'helper'})).json()
    assert user_child['disabled_tools'] == ['bash', 'write']
    providers['parent'].replies = [[_call('c1', agent='helper', prompt='help')], [Text('done')]]
    providers['child'].replies = [[Text('helped')]]
    await client.post(f"/api/session/{parent['id']}/prompt", json={'text': 'go'})
    await _idle(rt)
    children = (await client.get(f"/api/session/{parent['id']}/children")).json()
    assert len(children) == 2
    assert all(c['disabled_tools'] == ['bash', 'write'] for c in children)
    assert all('bash' not in c['tools'] and 'write' not in c['tools'] for c in children)
