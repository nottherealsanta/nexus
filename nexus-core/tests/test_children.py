import asyncio
import json

import httpx
import pytest
from conftest import remembering

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
    monkeypatch.setattr(runtime_module, "connected", lambda: ["codex"])
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "scripted/parent"))
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
    assert tool_output.startswith(f"Sub agent {child['id']} (agent helper) finished.\n\n42\n\n[Nexus notice: `message_session` is now available]")
    # A child never gets the session tool, even when its agent lists it.
    assert [t.name for t in providers["child"].requests[0].tools] == ["read", "grep", "glob", "ls", "bash", "bash_output", "bash_kill", "edit", "patch", "write", "web_search", "web_fetch", "ask", "code", "expose_workspace_tools", "escalate"]
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
    assert parent["id"] not in rt.turns and rt.turns  # the parent turn ended, but the child still runs
    assert (await client.get(f"/api/session/{parent['id']}")).json()["status"] == "busy"
    statuses = [e["data"] for e in rt.store.events(parent["id"], limit=5000) if e["type"] == "session.status"]
    assert statuses[-1]["status"] == "busy" and statuses[-1]["turn_finished"] is True
    assert all(s["status"] == "busy" for s in statuses)

    gate.set()
    await _idle(rt)
    assert (await client.get(f"/api/session/{parent['id']}")).json()["status"] == "idle"
    texts = await _texts(client, parent["id"])
    assert next(t for role, t in texts if role == "tool").startswith("Started sub agent")
    notice = [t for role, t in texts if role == "user" and t.startswith("[Notification: sub agent finished]")]
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


def test_child_model_pool_is_chosen_once_and_explicit_model_wins(env, monkeypatch):
    from nexus.config import ConfigFile

    rt, _, _, workspace = env
    config = ConfigFile()
    config.patch(config.read()["revision"], {"agents.models": {"helper": ["codex/parent", "codex/child"]}}, [])
    seen = []
    monkeypatch.setattr(runtime_module.secrets, "choice", lambda pool: seen.append(list(pool)) or pool[-1])
    project = rt.open_project(str(workspace))
    parent = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=None, permission=None, agent="helper", parent=parent)
    assert seen == [["codex/parent", "codex/child"]]
    assert child.model == "codex/child"
    assert rt.session_json(rt.session(child.id))["model"] == "codex/child"
    explicit = rt.create_session(project, title=None, model="codex/parent", permission=None, agent="helper", parent=parent)
    assert explicit.model == "codex/parent" and len(seen) == 1
    assert parent.model == "scripted/parent"  # a root session stores the model it starts with


def test_child_model_pool_skips_disconnected_providers_visibly(env, monkeypatch):
    from nexus.config import ConfigFile

    rt, _, _, workspace = env
    config = ConfigFile()
    config.patch(config.read()["revision"], {"agents.models": {"helper": ["groq/off", "codex/child"]},
                                               "agents.fallbacks": {"helper": ["nvidia/off", "codex/parent"]}}, [])
    seen = []
    monkeypatch.setattr(runtime_module.secrets, "choice", lambda pool: seen.append(list(pool)) or pool[0])
    project = rt.open_project(str(workspace))
    parent = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=None, permission=None, agent="helper", parent=parent)
    assert seen == [["codex/child"]] and child.model == "codex/child"
    summary = rt.session_json(rt.session(child.id))["snapshot"]
    assert summary["model_pick"] == {"pool": ["groq/off", "codex/child"], "eligible": ["codex/child"],
                                     "chosen": "codex/child", "skipped": ["groq/off"]}
    assert summary["model_fallbacks"] == {"primary": "codex/child", "models": ["codex/parent"], "skipped": ["nvidia/off"]}


def test_child_pool_with_no_connected_provider_uses_the_agent_model(env):
    from nexus.config import ConfigFile

    rt, _, _, workspace = env
    config = ConfigFile()
    config.patch(config.read()["revision"], {"agents.models": {"helper": ["groq/off"]}}, [])
    project = rt.open_project(str(workspace))
    parent = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=None, permission=None, agent="helper", parent=parent)
    assert child.model == ""
    data = rt.session_json(rt.session(child.id))
    assert (data["model"], data["model_source"]) == ("scripted/child", "agent")
    pick = data["snapshot"]["model_pick"]
    assert pick["chosen"] is None and pick["skipped"] == ["groq/off"] and "agent file" in pick["note"]


async def test_agent_list_reports_subagent_models(env):
    from nexus.config import ConfigFile

    _, client, _, workspace = env
    config = ConfigFile()
    config.patch(config.read()["revision"], {"agents.models": {"helper": ["codex/child"]}}, [])
    agents = (await client.get("/api/agent", params={"directory": str(workspace)})).json()["agents"]
    by_name = {a["name"]: a for a in agents}
    assert by_name["helper"]["subagent"] == {"pool": ["codex/child"], "fallbacks": [],
                                             "effective": {"model": "scripted/child", "source": "agent"}}
    assert by_name["quick"]["subagent"]["effective"] == {"model": "", "source": "parent"}
    assert "subagent" not in by_name["orchestrate"]


async def test_child_fallback_is_recorded_and_partial_output_is_not_retried(env):
    from nexus.config import ConfigFile
    from nexus.llmhub.types import ProviderError, TextDelta

    rt, client, providers, workspace = env
    config = ConfigFile()
    config.patch(config.read()["revision"], {"agents.models": {"helper": ["codex/child"]},
                                               "agents.fallbacks": {"helper": ["codex/parent"]}}, [])

    class Failing(Scripted):
        def __init__(self, partial):
            super().__init__([])
            self.partial = partial

        async def stream(self, req):
            if self.partial:
                yield TextDelta("partial")
            raise ProviderError("unavailable", status=400, phase="response")

    providers["child"] = Failing(False)
    providers["parent"] = Scripted([[Text("fallback answer")]])
    project = rt.open_project(str(workspace))
    parent = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=None, permission=None, agent="helper", parent=parent)
    assert rt.snapshot_for(child)["model_fallbacks"]["models"] == ["codex/parent"]
    turn_id = rt.prompt(child.id, "go")["turn_id"]
    await _idle(rt)
    requests = rt.store.requests_for_turn(turn_id)
    assert [(r["model"], r["status"]) for r in requests] == [("codex/child", "failed"), ("codex/parent", "completed")]
    assert rt.session(child.id).model == "codex/parent"
    messages = await _texts(client, child.id)
    assert any("model fallback" in text for _, text in messages if text)
    assert messages[-1] == ("assistant", "fallback answer")
    assert any("model fallback" in part.text for msg in providers["parent"].requests[0].messages for part in msg.parts if isinstance(part, Text))

    providers["child"] = Failing(True)
    child2 = rt.create_session(project, title=None, model=None, permission=None, agent="helper", parent=parent)
    rt.prompt(child2.id, "go")
    await _idle(rt)
    assert rt.session(child2.id).model == "codex/child"
    assert len(providers["parent"].requests) == 1


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
    assert bad_child.status_code == 400 and "cannot run as a sub agent" in bad_child.json()["error"]
    assert (await client.post(f"/api/session/{build['id']}/agent", json={"agent": "plan"})).status_code == 400


async def test_dynamic_child_guidance_and_default_root(env):
    rt, client, _, workspace = env
    (workspace / '.agents/agents/build.md').write_text('---\ndescription: Custom builder\nmode: subagent\n---\nBuild things.')
    parent = (await client.post('/api/session', json={'directory': str(workspace)})).json()
    assert parent['agent'] == 'orchestrate'
    assert 'build' not in [a.name for a in rt.agents(str(workspace)).allowed(child=False)]
    tool = rt._session_tool(rt.session(parent['id']))
    assert 'build' in tool.spec.parameters['properties']['agent']['enum']
    assert '`helper`: Helps' in tool.spec.description and '`build`: Custom builder' in tool.spec.description

    patched = (await client.patch(f"/api/session/{parent['id']}", json={'disabled_tools': ['session']})).json()
    assert 'session' not in patched['tools']
    # The starting prompt is frozen; the switch is forward guidance and a request-schema change instead.
    assert 'Agents:' not in rt._agent(rt.session(parent['id'])).instructions
    messages = (await client.get(f"/api/session/{parent['id']}/message")).json()
    notice = next(p['text'] for m in messages for p in m['parts'] if 'tool availability changed' in (p.get('text') or ''))
    assert '`session`' in notice and 'starting system prompt is frozen' in notice


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


def _message(call_id, **args):
    return ToolCall(call_id, "message_session", json.dumps(args))


async def _start_parent(client, workspace):
    return (await client.post("/api/session", json={"directory": str(workspace), "agent": "orchestrate"})).json()


async def _prompt_and_wait(rt, client, session_id, text="go"):
    await client.post(f"/api/session/{session_id}/prompt", json={"text": text})
    await _idle(rt)


async def test_first_session_call_exposes_message_session_once(env):
    rt, client, providers, workspace = env
    providers["parent"].replies = [
        [_call("c1", agent="helper", prompt="one")], [_call("c2", agent="helper", prompt="two")], [Text("done")]]
    providers["child"].replies = [[Text("1")], [Text("2")]]
    parent = await _start_parent(client, workspace)
    assert "message_session" not in parent["tools"]
    session_tool = rt._agent(rt.session(parent["id"])).tools["session"]
    assert "message_session" in session_tool.spec.description and "Launch a subagent" in session_tool.spec.description
    await _prompt_and_wait(rt, client, parent["id"])

    requests = providers["parent"].requests
    assert "message_session" not in [t.name for t in requests[0].tools]
    assert [t.name for t in requests[1].tools][-1] == "message_session"
    assert "message_session" in [t.name for t in requests[2].tools]
    tool_outputs = [t for role, t in await _texts(client, parent["id"]) if role == "tool"]
    assert "[Nexus notice: `message_session` is now available]" in tool_outputs[0]
    assert "message_session" not in tool_outputs[1]
    events = (await client.get(f"/api/session/{parent['id']}/event", params={"limit": 5000})).json()["events"]
    assert len([e for e in events if e["type"] == "session.message_tool"]) == 1
    snapshot = (await client.get(f"/api/session/{parent['id']}/snapshot")).json()
    assert snapshot["messaging"] == {"activated": True} and "message_session" in snapshot["tool_definitions"]
    # Children never get it, and it can be switched off on its own.
    child = (await client.get(f"/api/session/{parent['id']}/children")).json()[0]
    assert "message_session" not in child["tools"]
    patched = (await client.patch(f"/api/session/{parent['id']}", json={"disabled_tools": ["message_session"]})).json()
    assert "message_session" not in patched["tools"] and "session" in patched["tools"]


async def test_message_session_follows_up_an_idle_child_and_rejects_others(env):
    rt, client, providers, workspace = env
    parent = await _start_parent(client, workspace)
    stranger = await _start_parent(client, workspace)
    providers["parent"].replies = [[_call("c1", agent="helper", prompt="first")], [Text("first done")]]
    providers["child"].replies = [[Text("answer one")], [Text("answer two")]]
    await _prompt_and_wait(rt, client, parent["id"])
    [child] = (await client.get(f"/api/session/{parent['id']}/children")).json()

    providers["parent"].replies = [
        [_message("m1", session="ses_nope", message="x"), _message("m2", session=stranger["id"], message="x"),
         _message("m3", session=child["id"], message="now the second thing")],
        [Text("waiting")], [Text("got it")]]
    await _prompt_and_wait(rt, client, parent["id"], "follow up")

    parent_texts = await _texts(client, parent["id"])
    tool_outputs = [t for role, t in parent_texts if role == "tool"][1:]
    assert tool_outputs[0].startswith("ses_nope is not a sub agent of this session")
    assert tool_outputs[1].startswith(f"{stranger['id']} is not a sub agent of this session")
    assert "was idle; started a new turn" in tool_outputs[2]
    assert any(role == "user" and t.startswith("[Notification: sub agent finished]") and t.endswith("answer two")
               for role, t in parent_texts)
    child_texts = await _texts(client, child["id"])
    assert ("user", "now the second thing") in child_texts and child_texts[-1] == ("assistant", "answer two")


async def test_message_session_steers_a_working_child(env):
    rt, client, providers, workspace = env
    gate = asyncio.Event()
    providers["child"] = Scripted([[Text("first")], [Text("after steer")]], gate=gate)
    parent = await _start_parent(client, workspace)
    providers["parent"].replies = [[_call("c1", agent="helper", prompt="work", wait=False)], [Text("started")]]
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "go"})
    for _ in range(300):
        if parent["id"] not in rt.turns and rt.turns:
            break
        await asyncio.sleep(0.01)
    [child] = (await client.get(f"/api/session/{parent['id']}/children")).json()
    assert child["status"] == "busy"

    result = rt._message_child(parent["id"], {"session": child["id"], "message": "change course"})
    assert "which is working" in result
    providers["parent"].replies = [[Text("noted 1")], [Text("noted 2")]]
    gate.set()
    await _idle(rt)

    child_texts = await _texts(client, child["id"])
    assert ("user", "change course") in child_texts
    assert child_texts[-1] == ("assistant", "after steer")
    notices = [t for role, t in await _texts(client, parent["id"]) if role == "user" and t.startswith("[Notification")]
    assert notices and notices[-1].endswith("after steer")


def _escalate(call_id, **args):
    return ToolCall(call_id, "escalate", json.dumps(args))


async def test_escalate_is_offered_to_children_only_and_is_a_control_tool(env):
    rt, client, _providers, workspace = env
    parent = await _start_parent(client, workspace)
    child = (await client.post("/api/session", json={"parent": parent["id"], "agent": "helper"})).json()
    assert "escalate" not in parent["tools"] and child["tools"][-1] == "escalate"
    child_prompt = rt.store.snapshot(child["id"])["system_prompt"]
    assert "`escalate`" not in child_prompt
    assert "escalate" in rt.store.snapshot(child["id"])["tool_definitions"]
    assert "escalate" not in rt.store.snapshot(parent["id"])["tool_definitions"]
    # It can be switched off per session like any tool.
    off = (await client.patch(f"/api/session/{child['id']}", json={"disabled_tools": ["escalate"]})).json()
    assert "escalate" not in off["tools"]
    # An agent file cannot grant it to a root session.
    (workspace / ".agents" / "agents" / "sneaky.md").write_text(
        "---\ndescription: x\ntools: [read, escalate]\n---\nBody.\n")
    response = await client.post("/api/session", json={"directory": str(workspace), "agent": "sneaky"})
    assert response.status_code >= 400


async def test_blocking_parent_reads_the_childs_escalation(env):
    rt, client, providers, workspace = env
    providers["parent"].replies = [[_call("c1", agent="helper", prompt="deploy it")], [Text("asking the user")]]
    providers["child"].replies = [
        [_escalate("e1", needed="  the deploy   key ", why="the tool was denied")],
        [Text("Built it; could not deploy.")]]
    parent = await _start_parent(client, workspace)
    await _prompt_and_wait(rt, client, parent["id"])
    [child] = (await client.get(f"/api/session/{parent['id']}/children")).json()
    tool_output = next(t for role, t in await _texts(client, parent["id"]) if role == "tool")
    assert "finished.\n\nBuilt it; could not deploy." in tool_output
    assert "Attention: the child reported it is blocked." in tool_output
    assert "- the deploy key (why: the tool was denied)" in tool_output and "message_session" in tool_output
    # The child's own timeline shows the call and its result, and an event says it escalated.
    child_texts = await _texts(client, child["id"])
    assert any("Escalation recorded" in (t or "") and "grants nothing" in t for role, t in child_texts if role == "tool")
    events = (await client.get(f"/api/session/{child['id']}/event")).json()["events"]
    [escalated] = [e["data"] for e in events if e["type"] == "session.escalated"]
    assert (escalated["needed"], escalated["why"], escalated["parent_id"]) == ("the deploy key", "the tool was denied", parent["id"])
    assert not rt.escalations  # carried once, then cleared


async def test_non_blocking_escalation_arrives_in_the_notification(env):
    rt, client, providers, workspace = env
    providers["parent"].replies = [[_call("c1", agent="helper", prompt="look", wait=False)], [Text("waiting")], [Text("ok")]]
    providers["child"].replies = [[_escalate("e1", needed="a decision", why="two options")], [Text("stopped")]]
    parent = await _start_parent(client, workspace)
    await _prompt_and_wait(rt, client, parent["id"])
    notice = next(t for role, t in await _texts(client, parent["id"]) if role == "user" and t.startswith("[Notification"))
    assert "stopped" in notice and "- a decision (why: two options)" in notice


async def test_escalate_refuses_bad_arguments_and_is_limited(env):
    rt, client, providers, workspace = env
    calls = [_escalate("bad", needed="", why="x")] + [_escalate(f"e{i}", needed=f"n{i}", why="w") for i in range(6)]
    providers["parent"].replies = [[_call("c1", agent="helper", prompt="go")], [Text("done")]]
    providers["child"].replies = [calls, [Text("report")]]
    parent = await _start_parent(client, workspace)
    await _prompt_and_wait(rt, client, parent["id"])
    [child] = (await client.get(f"/api/session/{parent['id']}/children")).json()
    outputs = [t for role, t in await _texts(client, child["id"]) if role == "tool"]
    assert "non-empty 'needed' and 'why'" in outputs[0]
    assert sum("Escalation recorded" in o for o in outputs) == 5 and "escalation limit reached (5)" in outputs[-1]
    tool_output = next(t for role, t in await _texts(client, parent["id"]) if role == "tool")
    assert tool_output.count("\n- ") == 5


async def test_a_child_without_the_flag_keeps_working_and_never_gains_escalate(env):
    rt, client, providers, workspace = env
    parent = await _start_parent(client, workspace)
    child = (await client.post("/api/session", json={"parent": parent["id"], "agent": "helper"})).json()
    snapshot = rt.store.snapshot(child["id"])
    snapshot.pop("escalation")
    snapshot["tool_definitions"].pop("escalate")
    snapshot["tools"].remove("escalate")
    rt.store.set_snapshot(child["id"], snapshot)
    assert "escalate" not in (await client.get(f"/api/session/{child['id']}")).json()["tools"]
    providers["child"].replies = [[Text("fine")]]
    await _prompt_and_wait(rt, client, child["id"])
    assert (await _texts(client, child["id"]))[-1] == ("assistant", "fine")
