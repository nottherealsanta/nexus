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


async def test_info_reports_the_api_contract_clients_check(setup):
    _, client, _, _ = setup
    info = (await client.get("/api/info")).json()
    assert info["name"] == "nexus" and info["api_version"] == {"major": 1, "minor": 1}
    assert set(info) >= {"version", "pid", "home", "url", "log", "retention", "retry", "idempotency"}


async def test_projects_are_unique_by_path_and_own_sessions(setup):
    _, client, _, workspace = setup
    first = (await client.post("/api/project", json={"path": str(workspace)})).json()
    again = (await client.post("/api/project", json={"path": str(workspace / ".")})).json()
    assert first["id"] == again["id"] and first["name"] == "proj"
    session = (await client.post("/api/session", json={"project": first["id"]})).json()
    assert (session["directory"], session["agent"], session["model"], session["model_source"], session["status"], session["permission"]) == (
        str(workspace.resolve()), "build", "scripted/m", "default", "idle", "allow")
    listed = (await client.get("/api/session", params={"directory": str(workspace)})).json()
    assert [s["id"] for s in listed] == [session["id"]]
    assert (await client.post("/api/project", json={"path": str(workspace / "missing")})).status_code == 400
    assert (await client.get("/api/session/ses_nope")).status_code == 404


async def test_write_in_workspace_needs_no_permission_and_saves_messages(setup):
    rt, client, provider, workspace = setup
    provider.replies = [[ToolCall("c1", "write", '{"path": "x.txt", "content": "hi"}')], [Text("done")]]
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    events = []

    async def collect():
        async for event in rt.bus.subscribe():
            events.append(event)
            if event["type"] == "session.status" and event["data"]["status"] == "idle":
                return

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0)
    assert (await client.post(f"/api/session/{session['id']}/prompt", json={"text": "make x.txt"})).status_code == 202
    await asyncio.wait_for(collector, 5)

    assert (workspace / "x.txt").read_text() == "hi"
    types = [e["type"] for e in events]
    assert types[:2] == ["session.updated", "session.status"]  # title from the first prompt, then busy
    # Inside the session directory, write (and edit) never asks for approval.
    assert "permission.asked" not in types and "tool_end" in types and types[-1] == "session.status"
    messages = (await client.get(f"/api/session/{session['id']}/message")).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1]["parts"][0] == {"type": "tool_call", "id": "c1", "name": "write", "arguments": '{"path": "x.txt", "content": "hi"}', "origin": None, "raw": None}
    assert (await client.get(f"/api/session/{session['id']}")).json()["title"] == "make x.txt"
    context = (await client.get(f"/api/session/{session['id']}/context")).json()
    assert (context["model"], context["messages"], context["tools"]) == ("m", 4, ["read", "bash", "bash_output", "bash_kill", "edit", "write", "ask", "code", "expose_workspace_tools"])
    assert context["instructions"].startswith("You are an AI agent running in Nexus") and "# Environment" in context["instructions"]


async def test_interrupt_closes_a_waiting_tool_call(setup):
    rt, client, provider, workspace = setup
    provider.replies = [[ToolCall("c1", "bash", '{"command": "true"}')]]
    session = (await client.post("/api/session", json={"directory": str(workspace), "permission": "ask"})).json()
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
    events = (await client.get(f"/api/session/{session['id']}/event")).json()["events"]
    replies = [e["data"] for e in events if e["type"] == "permission.replied"]
    assert len(replies) == 1 and replies[0]["outcome"] == "cancelled" and replies[0]["allow"] is False


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
    assert (context["model"], context["tools"], context["effort"]) == ("big", ["read", "expose_workspace_tools"], "high")
    instructions = context["instructions"]
    assert instructions.startswith("You review code.\n\n# Tools\nThese are the only tools in this session:\n- `read`: ")
    assert "`bash`" not in instructions and "`write`" not in instructions

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
    # Questions are rendered from the stored events and the pending endpoint.
    assert "question.asked" in response.text and "question.replied" in response.text
    assert "/api/session/${id}/question" in response.text


async def test_code_nested_write_needs_no_approval_and_kernel_api(setup):
    rt, client, provider, workspace = setup
    code = 'await write(path="x.txt", content="hi")\nawait bash(command="sleep 0.5")\nx = 41\nx'
    provider.replies = [[ToolCall("code1", "code", json.dumps({"code": code}))], [Text("done")]]
    session = (await client.post("/api/session", json={"directory": str(workspace), "permission": "ask"})).json()
    sid = session["id"]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "go"})
    for _ in range(100):
        pending = rt.pending_approvals(sid)
        if pending:
            break
        await asyncio.sleep(0.01)
    # The nested write needed no approval; the nested bash still asks.
    assert len(pending) == 1 and pending[0]["tool"] == "bash"
    assert (workspace / "x.txt").read_text() == "hi"
    kid = (await client.get("/api/kernel")).json()[0]["id"]
    assert (await client.delete(f"/api/kernel/{kid}")).status_code == 409
    await client.post(f"/api/session/{sid}/permission/{pending[0]['id']}", json={"reply": "allow"})
    await _wait_idle(rt, sid)
    events = (await client.get(f"/api/session/{sid}/event")).json()["events"]
    assert [e["data"]["tool"] for e in events if e["type"] == "permission.asked"] == ["bash"]
    calls = [e["data"] for e in events if e["type"] == "code.call"]
    write_call = next(call for call in calls if call["tool"] == "write")
    assert write_call["parent_call_id"] == "code1" and write_call["output"].startswith("created")
    details = (await client.get(f"/api/kernel/{kid}")).json()
    assert details["cell_count"] == 1 and details["variables"][0]["name"] == "x"
    await client.delete(f"/api/session/{sid}")
    assert (await client.get(f"/api/kernel/{kid}")).status_code == 200
    assert (await client.delete(f"/api/kernel/{kid}")).status_code == 204
    assert (await client.get(f"/api/kernel/{kid}")).status_code == 404
    await rt.shutdown()


async def test_code_child_shares_tree_kernel(setup):
    rt, client, provider, workspace = setup
    parent = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    provider.replies = [[ToolCall("c1", "code", '{"code": "x = 41"}')], [Text("ok")]]
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "set x"})
    await _wait_idle(rt, parent["id"])
    child = (await client.post("/api/session", json={"parent": parent["id"], "agent": "build"})).json()
    provider.replies = [[ToolCall("c2", "code", '{"code": "x + 1"}')], [Text("42")]]
    await client.post(f"/api/session/{child['id']}/prompt", json={"text": "read x"})
    await _wait_idle(rt, child["id"])
    [kernel] = (await client.get("/api/kernel")).json()
    assert kernel["cell_count"] == 2 and kernel["cells"][1]["value"] == "42"
    assert set(kernel["users"]) == {parent["id"], child["id"]}
    await rt.shutdown()


async def test_code_in_a_child_uses_the_child_approval_and_events(setup):
    rt, client, provider, workspace = setup
    parent = (await client.post("/api/session", json={"directory": str(workspace), "permission": "ask"})).json()
    child = (await client.post("/api/session", json={"parent": parent["id"], "agent": "build", "permission": "ask"})).json()
    provider.replies = [[ToolCall("cc1", "code", json.dumps({"code": 'await bash(command="printf kid > child.txt")'}))],
                        [Text("done")]]
    await client.post(f"/api/session/{child['id']}/prompt", json={"text": "go"})
    for _ in range(100):
        pending = rt.pending_approvals(child["id"])
        if pending:
            break
        await asyncio.sleep(0.01)
    [request] = pending
    assert request["session_id"] == child["id"] and request["tool"] == "bash"
    assert request["call_id"] == "cc1:1:1"
    assert not rt.pending_approvals(parent["id"])
    await client.post(f"/api/session/{child['id']}/permission/{request['id']}", json={"reply": "allow"})
    await _wait_idle(rt, child["id"])
    assert (workspace / "child.txt").read_text() == "kid"
    events = (await client.get(f"/api/session/{child['id']}/event")).json()["events"]
    call = next(e["data"] for e in events if e["type"] == "code.call")
    cell = next(e["data"] for e in events if e["type"] == "code.cell")
    assert call["parent_call_id"] == "cc1" and cell["session_id"] == child["id"]
    [kernel] = (await client.get("/api/kernel")).json()
    assert kernel["users"] == [child["id"]] and kernel["created_by"] == child["id"]
    await rt.shutdown()


async def test_saved_tool_reference_stops_working_when_the_session_disables_it(setup):
    rt, client, provider, workspace = setup
    session = (await client.post("/api/session", json={"directory": str(workspace), "permission": "allow"})).json()
    sid = session["id"]
    provider.replies = [[ToolCall("s1", "code", '{"code": "saved = write"}')], [Text("ok")]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "save it"})
    await _wait_idle(rt, sid)
    await client.patch(f"/api/session/{sid}", json={"disabled_tools": ["write"]})
    provider.replies = [[ToolCall("s2", "code", json.dumps({
        "code": 'try:\n    await saved(path="forbidden", content="no")\nexcept RuntimeError as e:\n    print(e)'}))],
        [Text("done")]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "use it"})
    await _wait_idle(rt, sid)
    messages = (await client.get(f"/api/session/{sid}/message")).json()
    outputs = [p["output"] for m in messages for p in m["parts"] if p["type"] == "tool_result" and "kernel" in p["output"]]
    assert ("unavailable" in outputs[-1] or "not defined" in outputs[-1]) and "write" in outputs[-1]
    assert not (workspace / "forbidden").exists()
    await rt.shutdown()


async def test_disabled_tools_roundtrip_and_context(setup):
    rt, client, _, workspace = setup
    session = (await client.post('/api/session', json={
        'directory': str(workspace), 'disabled_tools': ['bash', 'write', 'bash']})).json()
    sid = session['id']
    assert session['disabled_tools'] == ['bash', 'write']
    assert session['tools'] == ['read', 'edit', 'ask', 'code', 'expose_workspace_tools']
    assert rt.store.session(sid).disabled() == {'bash', 'write'}
    context = (await client.get(f'/api/session/{sid}/context')).json()
    assert context['tools'] == session['tools']
    assert '`bash`' not in context['instructions'] and '`write`' not in context['instructions']
    patched = (await client.patch(f'/api/session/{sid}', json={'disabled_tools': []})).json()
    assert patched['disabled_tools'] == [] and 'bash' in patched['tools']
    assert (await client.get(f'/api/session/{sid}')).json()['tools'] == patched['tools']
    one_off = (await client.patch(f'/api/session/{sid}', json={'disabled_tools': ['bash_output']})).json()
    assert 'bash_output' not in one_off['tools'] and 'bash' in one_off['tools'] and 'bash_kill' in one_off['tools']
    await client.patch(f'/api/session/{sid}', json={'disabled_tools': []})
    for names in (['missing'], 'bash', [1]):
        for method, url in ((client.post, '/api/session'), (client.patch, f'/api/session/{sid}')):
            response = await method(url, json={'directory': str(workspace), 'disabled_tools': names})
            assert response.status_code == 400
            if names == ['missing']:
                assert 'known:' in response.json()['error']


async def test_disabled_tool_cannot_run_inside_code(setup):
    rt, client, provider, workspace = setup
    session = (await client.post('/api/session', json={
        'directory': str(workspace), 'permission': 'allow', 'disabled_tools': ['write']})).json()
    provider.replies = [[ToolCall('c1', 'code', json.dumps({'code': 'await write(path="forbidden", content="no")'}))],
                        [Text('done')]]
    try:
        await client.post(f"/api/session/{session['id']}/prompt", json={'text': 'go'})
        await _wait_idle(rt, session['id'])
        messages = (await client.get(f"/api/session/{session['id']}/message")).json()
        output = next(p['output'] for m in messages for p in m['parts'] if p['type'] == 'tool_result')
        assert 'write' in output and 'Error' in output
        assert not (workspace / 'forbidden').exists()
    finally:
        await rt.shutdown()


async def test_no_tools_and_disabled_direct_call(setup):
    rt, client, provider, workspace = setup
    names = ['read', 'bash', 'edit', 'write', 'code', 'session', 'expose_workspace_tools']
    session = (await client.post('/api/session', json={
        'directory': str(workspace), 'agent': 'orchestrate', 'disabled_tools': names})).json()
    sid = session['id']
    assert session['tools'] == []
    context = (await client.get(f'/api/session/{sid}/context')).json()
    assert context['tools'] == [] and 'No tools are available' in context['instructions']
    assert all(f'- `{name}`:' not in context['instructions'] for name in names)
    provider.replies = [[ToolCall('c1', 'bash', '{"command": "touch forbidden"}')], [Text('done')]]
    await client.post(f'/api/session/{sid}/prompt', json={'text': 'go'})
    await _wait_idle(rt, sid)
    messages = (await client.get(f'/api/session/{sid}/message')).json()
    result = next(p for m in messages for p in m['parts'] if p['type'] == 'tool_result')
    assert result['is_error'] and not (workspace / 'forbidden').exists()
    assert not rt.pending_approvals(sid)


async def test_background_bash_notice_output_api_and_session_delete(setup):
    rt, client, provider, workspace = setup
    session = (await client.post('/api/session', json={'directory': str(workspace), 'permission': 'allow'})).json()
    sid = session['id']
    provider.replies = [
        [ToolCall('b1', 'bash', json.dumps({'command': 'printf hello; sleep 0.3; printf world', 'background': True}))],
        [Text('started')],
        [Text('noticed')],
    ]
    await client.post(f'/api/session/{sid}/prompt', json={'text': 'start one'})
    await _wait_idle(rt, sid)
    [job] = (await client.get('/api/bash')).json()
    jid = job['id']
    for _ in range(200):
        current = (await client.get(f'/api/bash/{jid}')).json()
        if current['status'] != 'running':
            break
        await asyncio.sleep(0.02)
    assert current['status'] == 'done' and current['output'] == 'helloworld'

    # The finish notice is context, and wakes an idle session.
    for _ in range(200):
        messages = (await client.get(f'/api/session/{sid}/message')).json()
        if any('→ Background Shell Done:' in (p.get('text') or '') for m in messages for p in m['parts']):
            break
        await asyncio.sleep(0.02)
    await _wait_idle(rt, sid)
    messages = (await client.get(f'/api/session/{sid}/message')).json()
    notice = next(p['text'] for m in messages for p in m['parts'] if '→ Background Shell Done:' in (p.get('text') or ''))
    assert jid in notice and 'helloworld' in notice

    # bash_output is a task tool and reads the retained output.
    provider.replies = [[ToolCall('o1', 'bash_output', json.dumps({'job_id': jid}))], [Text('read')]]
    await client.post(f'/api/session/{sid}/prompt', json={'text': 'read it'})
    await _wait_idle(rt, sid)
    messages = (await client.get(f'/api/session/{sid}/message')).json()
    result = next(p['output'] for m in messages for p in m['parts'] if p['type'] == 'tool_result' and 'helloworld' in p['output'])
    assert jid in result and 'done' in result

    # Deleting the session stops its running job; the job record survives for inspection.
    provider.replies = [[ToolCall('k1', 'bash', json.dumps({'command': 'sleep 5', 'background': True}))], [Text('started')]]
    await client.post(f'/api/session/{sid}/prompt', json={'text': 'start a long one'})
    await _wait_idle(rt, sid)
    [running] = [j for j in (await client.get('/api/bash')).json() if j['status'] == 'running']
    await client.delete(f'/api/session/{sid}')
    current = (await client.get(f"/api/bash/{running['id']}")).json()
    assert current['status'] == 'killed'
    await rt.shutdown()


async def test_bash_kill_tool_stops_a_job(setup):
    rt, client, provider, workspace = setup
    session = (await client.post('/api/session', json={'directory': str(workspace), 'permission': 'allow'})).json()
    sid = session['id']
    provider.replies = [[ToolCall('b1', 'bash', json.dumps({'command': 'sleep 5', 'background': True}))], [Text('started')]]
    await client.post(f'/api/session/{sid}/prompt', json={'text': 'start a long one'})
    await _wait_idle(rt, sid)
    [job] = [j for j in (await client.get('/api/bash')).json() if j['status'] == 'running']
    provider.replies = [[ToolCall('k1', 'bash_kill', json.dumps({'job_id': job['id']}))], [Text('killed')], [Text('noticed')]]
    await client.post(f'/api/session/{sid}/prompt', json={'text': 'stop it'})
    await _wait_idle(rt, sid)
    current = (await client.get(f"/api/bash/{job['id']}")).json()
    assert current['status'] == 'killed'
    for _ in range(100):
        messages = (await client.get(f'/api/session/{sid}/message')).json()
        if any('→ Background Shell Done:' in (p.get('text') or '') and 'killed' in (p.get('text') or '') for m in messages for p in m['parts']):
            break
        await asyncio.sleep(0.02)
    await _wait_idle(rt, sid)
    messages = (await client.get(f'/api/session/{sid}/message')).json()
    outputs = [p['output'] for m in messages for p in m['parts'] if p['type'] == 'tool_result' and job['id'] in p['output']]
    assert 'killed' in outputs[-1]
    await rt.shutdown()


async def test_code_cell_can_start_and_read_a_background_job(setup):
    rt, client, provider, workspace = setup
    session = (await client.post('/api/session', json={'directory': str(workspace), 'permission': 'allow'})).json()
    sid = session['id']
    code = ('import asyncio\n'
            'started = await bash(command="printf out; sleep 0.2", background=True)\n'
            'job_id = started.split()[3]\n'
            'await asyncio.sleep(0.4)\n'
            'await bash_output(job_id=job_id)\n')
    provider.replies = [[ToolCall('j1', 'code', json.dumps({'code': code}))], [Text('done')], [Text('noticed')]]
    await client.post(f'/api/session/{sid}/prompt', json={'text': 'go'})
    await _wait_idle(rt, sid)
    messages = (await client.get(f'/api/session/{sid}/message')).json()
    result = next(p['output'] for m in messages for p in m['parts'] if p['type'] == 'tool_result' and 'job_' in p['output'])
    assert 'out' in result and 'done' in result
    [job] = (await client.get('/api/bash')).json()
    assert job['status'] == 'done' and job['session_id'] == sid
    await rt.shutdown()


async def test_bash_job_tools_never_ask(setup):
    rt, _, _, _ = setup
    for name in ("bash_output", "bash_kill"):
        assert await rt._approve("ses_missing", ToolCall("c", name, "{}"), {}) is True


def test_disabled_tools_persist_and_migrate(tmp_path):
    path = tmp_path / 'migration.db'
    store = Store(path)
    project, _ = store.project_for_path(str(tmp_path))
    session = store.create_session(project, title='Persist', agent='build', disabled_tools=['bash'])
    store.close()
    store = Store(path)
    assert store.session(session.id).disabled() == {'bash'}
    # Simulate the immediately preceding schema, preserving an existing session.
    store.db.execute('alter table session drop column disabled_tools')
    store.close()
    store = Store(path)
    assert store.session(session.id).disabled() == set()
    store.close()


async def test_project_file_browsing_confines_paths_and_rejects_binary(setup, tmp_path):
    _, client, _, workspace = setup
    (workspace / "hello.txt").write_text("hello", encoding="utf-8")
    (workspace / "binary").write_bytes(b"a\0b")
    (workspace / "escape").symlink_to(tmp_path, target_is_directory=True)
    project = (await client.post("/api/project", json={"path": str(workspace)})).json()
    base = f"/api/project/{project['id']}"
    listing = (await client.get(base + "/files")).json()
    assert {e["name"]: e["kind"] for e in listing["entries"]}["escape"] == "symlink"
    file = (await client.get(base + "/file", params={"path": "hello.txt"})).json()
    assert file["content"] == "hello" and file["revision"].startswith("sha256:")
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    other = (await client.post("/api/project", json={"path": str(other_workspace)})).json()
    other_session = (await client.post("/api/session", json={"project": other["id"]})).json()
    assert (await client.get(base + "/files", params={"session": other_session["id"]})).status_code == 400
    for path in ("../nexus.db", "escape/nexus.db", "binary", "/etc/passwd"):
        assert (await client.get(base + "/file", params={"path": path})).status_code == 400


async def test_project_listing_git_names_and_special_files(setup):
    _, client, _, workspace = setup
    process = await asyncio.create_subprocess_exec("git", "init", "-q", str(workspace))
    assert await process.wait() == 0
    (workspace / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (workspace / "ignored").mkdir()
    (workspace / "ignored" / "a").write_text("x", encoding="utf-8")
    (workspace / "nested").mkdir()
    (workspace / "nested" / "line\nname").write_text("x", encoding="utf-8")
    (workspace / "fifo").unlink(missing_ok=True)
    import os
    os.mkfifo(workspace / "fifo")
    project = (await client.post("/api/project", json={"path": str(workspace)})).json()
    base = f"/api/project/{project['id']}"
    result = (await client.get(base + "/files")).json()
    entries = {entry["name"]: entry for entry in result["entries"]}
    assert entries["nested"]["git_status"] == "??"
    assert entries["ignored"]["ignored"] and entries[".git"]["ignored"]
    nested = (await client.get(base + "/files", params={"path": "nested"})).json()
    assert nested["entries"][0]["name"] == "line\nname"
    assert nested["entries"][0]["git_status"] == "??"
    from nexus.files import list_directory
    subroot = await list_directory(workspace / "nested", "")
    assert subroot["entries"][0]["git_status"] == "??"
    assert (await client.get(base + "/file", params={"path": "fifo"})).status_code == 400


async def _file_search(client, workspace, query=None, **params):
    project = (await client.post("/api/project", json={"path": str(workspace)})).json()
    if query is not None:
        params["q"] = query
    return await client.get(f"/api/project/{project['id']}/files/search", params=params)


async def test_file_search_ranks_basename_prefix_then_substring_then_path(setup):
    _, client, _, workspace = setup
    for rel in ("configs/readme.md", "deep/a/b/xconfig.txt", "src/myconfig.py",
                "src/config_loader.py", "config.py"):
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        (workspace / rel).write_text("x", encoding="utf-8")
    response = await _file_search(client, workspace, "CONFIG")
    assert response.status_code == 200
    assert response.json() == {"query": "CONFIG", "paths": [
        "config.py", "src/config_loader.py", "src/myconfig.py",
        "deep/a/b/xconfig.txt", "configs/readme.md"]}


async def test_file_search_directories_have_slash_and_rank_ahead_of_files(setup):
    _, client, _, workspace = setup
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("x", encoding="utf-8")
    (workspace / "src.txt").write_text("x", encoding="utf-8")
    plain = (await _file_search(client, workspace, "src")).json()["paths"]
    assert plain == ["src.txt", "src/a.py"]
    with_dirs = (await _file_search(client, workspace, "src", directories="true")).json()
    assert with_dirs["paths"] == ["src/", "src.txt", "src/a.py"]


async def test_file_search_excludes_hidden_symlinked_and_ignored_entries(setup, tmp_path):
    _, client, _, workspace = setup
    process = await asyncio.create_subprocess_exec("git", "init", "-q", str(workspace))
    assert await process.wait() == 0
    (workspace / ".gitignore").write_text("ignored.log\nignored/\n", encoding="utf-8")
    (workspace / "visible.py").write_text("x", encoding="utf-8")
    (workspace / "new.py").write_text("x", encoding="utf-8")
    (workspace / ".hidden.py").write_text("x", encoding="utf-8")
    (workspace / ".dotdir").mkdir()
    (workspace / ".dotdir" / "x.py").write_text("x", encoding="utf-8")
    (workspace / "ignored.log").write_text("x", encoding="utf-8")
    (workspace / "ignored").mkdir()
    (workspace / "ignored" / "y.py").write_text("x", encoding="utf-8")
    (workspace / "link.py").symlink_to(workspace / "visible.py")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.py").write_text("x", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    response = await _file_search(client, workspace, "", directories="1")
    assert response.json()["paths"] == ["new.py", "visible.py"]
    assert (await _file_search(client, workspace, "leak")).json()["paths"] == []


async def test_file_search_session_root_is_relative_to_that_directory(setup):
    _, client, _, workspace = setup
    process = await asyncio.create_subprocess_exec("git", "init", "-q", str(workspace))
    assert await process.wait() == 0
    (workspace / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (workspace / "top.py").write_text("x", encoding="utf-8")
    sub = workspace / "sub"
    sub.mkdir()
    (sub / "a.py").write_text("x", encoding="utf-8")
    (sub / "build.log").write_text("x", encoding="utf-8")
    project = (await client.post("/api/project", json={"path": str(workspace)})).json()
    session = (await client.post("/api/session", json={"project": project["id"]})).json()
    moved = await client.post(f"/api/session/{session['id']}/directory", json={"directory": str(sub)})
    assert moved.status_code == 200, moved.text
    base = f"/api/project/{project['id']}/files/search"
    scoped = await client.get(base, params={"q": "", "session": session["id"]})
    assert scoped.json()["paths"] == ["a.py"]
    whole = await client.get(base, params={"q": "", "limit": 10})
    assert whole.json()["paths"] == ["top.py", "sub/a.py"]


async def test_file_search_limit_is_capped_and_zero_is_empty(setup):
    _, client, _, workspace = setup
    for index in range(120):
        (workspace / f"f{index:03}.txt").write_text("x", encoding="utf-8")
    assert len((await _file_search(client, workspace, "f", limit=1000)).json()["paths"]) == 100
    assert len((await _file_search(client, workspace, "f")).json()["paths"]) == 30
    assert (await _file_search(client, workspace, "f", limit=0)).json()["paths"] == []


async def test_file_search_rejects_invalid_queries_and_limits(setup):
    _, client, _, workspace = setup
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    for query in ("/abs", "\\back", "../up", "a/../b", "bad\x00", "x" * 257):
        response = await _file_search(client, workspace, query)
        assert response.status_code == 400 and "error" in response.json(), query
    for limit in ("-1", "many", ""):
        response = await _file_search(client, workspace, "a", limit=limit)
        assert response.status_code == 400, limit


async def test_file_search_empty_query_lists_first_visible_paths(setup):
    _, client, _, workspace = setup
    (workspace / "b.txt").write_text("x", encoding="utf-8")
    (workspace / "a").mkdir()
    (workspace / "a" / "z.txt").write_text("x", encoding="utf-8")
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    response = await _file_search(client, workspace)
    assert response.json() == {"query": "", "paths": ["a.txt", "b.txt", "a/z.txt"]}


async def test_session_git_status_and_diff(setup):
    _, client, _, workspace = setup
    for args in (("init", "-q"), ("config", "user.email", "test@example.org"),
                 ("config", "user.name", "Test")):
        process = await asyncio.create_subprocess_exec("git", *args, cwd=workspace)
        assert await process.wait() == 0
    (workspace / "a.txt").write_text("before\n", encoding="utf-8")
    for args in (("add", "a.txt"), ("commit", "-qm", "initial")):
        process = await asyncio.create_subprocess_exec("git", *args, cwd=workspace)
        assert await process.wait() == 0
    (workspace / "a.txt").write_text("after\n", encoding="utf-8")
    project = (await client.post("/api/project", json={"path": str(workspace)})).json()
    session = (await client.post("/api/session", json={"project": project["id"]})).json()
    base = f"/api/session/{session['id']}/git"
    status = (await client.get(base)).json()
    assert status["changes"][0]["path"] == "a.txt"
    assert status["changes"][0]["added"] == 1 and status["changes"][0]["removed"] == 1
    diff = (await client.get(base + "/diff", params={"path": "a.txt"})).json()
    assert "+after" in diff["unstaged"] and diff["staged"] == ""
    assert (await client.get(base + "/diff", params={"path": "../a.txt"})).status_code == 400


async def test_default_permission_runs_bash_without_a_wait(setup):
    rt, client, provider, workspace = setup
    provider.replies = [[ToolCall("b1", "bash", '{"command": "printf allowed"}')], [Text("done")]]
    session = (await client.post('/api/session', json={'directory': str(workspace)})).json()
    await client.post(f"/api/session/{session['id']}/prompt", json={'text': 'go'})
    await _wait_idle(rt, session['id'])
    assert session['permission'] == 'allow'
    events = rt.store.events(session['id'])
    assert not any(e['type'] == 'permission.asked' for e in events)
    assert any(e['type'] == 'tool_end' and 'allowed' in e['data']['result']['output'] for e in events)


async def test_image_only_prompt_is_durable_and_replayed(setup):
    from test_images import URL

    rt, client, provider, workspace = setup
    provider.replies = [[Text('a gradient')]]
    session = (await client.post('/api/session', json={'directory': str(workspace)})).json()
    sid = session['id']
    invalid = await client.post(f'/api/session/{sid}/prompt', json={'images': [{'data_url': 'bad'}]})
    assert invalid.status_code == 400 and not rt.store.messages(sid)
    response = await client.post(f'/api/session/{sid}/prompt', json={'images': [{'name': 'shot.png', 'data_url': URL}]})
    assert response.status_code == 202
    await _wait_idle(rt, sid)
    messages = (await client.get(f'/api/session/{sid}/message')).json()
    assert messages[0]['parts'][1]['text'] == '[Image A]'
    assert messages[0]['parts'][2] == {'type': 'image', 'name': 'shot.png', 'data_url': URL}
    assert rt.store.messages(sid)[0].message.parts[2].data_url == URL
    assert (await client.get(f'/api/session/{sid}')).json()['title'] == 'Image'


async def test_image_labels_are_stored_in_attachment_order(setup):
    from test_images import URL

    rt, client, provider, workspace = setup
    provider.replies = [[Text('done')]]
    sid = (await client.post('/api/session', json={'directory': str(workspace)})).json()['id']
    result = await client.post(f'/api/session/{sid}/prompt', json={
        'text': 'Compare [Image A] and [Image B]',
        'images': [{'name': 'first.png', 'data_url': URL}, {'name': 'second.png', 'data_url': URL}],
    })
    assert result.status_code == 202
    await _wait_idle(rt, sid)
    parts = rt.store.messages(sid)[0].message.parts
    assert [(parts[1].text, parts[2].name), (parts[3].text, parts[4].name)] == [
        ('[Image A]', 'first.png'), ('[Image B]', 'second.png')]


def test_existing_permissions_migrate_once_and_explicit_ask_survives(tmp_path):
    db = tmp_path / 'permissions.db'
    store = Store(db)
    project, _ = store.project_for_path(str(tmp_path))
    parent = store.create_session(project, title='parent', agent='build', permission='ask')
    child = store.create_session(project, title='child', agent='build', permission='ask', parent=parent)
    with store.db:
        store.db.execute("delete from migration where name = 'default_tool_allow_v1'")
    store.close()

    store = Store(db)
    assert store.session(parent.id).permission == 'allow'
    assert store.session(child.id).permission == 'allow'
    assert store.session(parent.id).revision == parent.revision + 1
    store.update_session(parent.id, permission='ask')
    store.close()

    store = Store(db)
    assert store.session(parent.id).permission == 'ask'
    assert store.session(child.id).permission == 'allow'
    store.close()
