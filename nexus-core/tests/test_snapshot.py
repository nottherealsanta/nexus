"""Session snapshots: the frozen agent, prompt, tools and instruction file.

These tests use scripted providers and temporary homes/workspaces. A live
provider is not needed: the snapshot boundary is checked on the request objects
the runtime hands to `provider.stream()` and on the HTTP `/context` endpoint.
"""
import asyncio
import json
from datetime import datetime as real_datetime

import httpx
import pytest

from nexus import agents as agents_module
from nexus import runtime as runtime_module
from nexus.llmhub.types import Done, Message, PartDone, Text, ToolCall
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


class Scripted:
    name = "scripted"

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.requests = []
        self.gates = []

    def body(self, req):
        return {
            "model": req.model,
            "instructions": req.instructions,
            "messages": len(req.messages),
            "effort": req.reasoning_effort,
            "tools": [{"name": t.name, "description": t.description, "parameters": t.parameters}
                      for t in req.tools],
        }

    async def stream(self, req):
        self.requests.append(req)
        gate = self.gates.pop(0) if self.gates else None
        if gate is not None:
            await gate.wait()
        parts = self.replies.pop(0)
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    store = Store(tmp_path / "nexus.db")
    rt = make_runtime("scripted/m", store)
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    workspace = tmp_path / "proj"
    (workspace / ".agents" / "agents").mkdir(parents=True)
    return rt, client, provider, workspace, store


async def _idle(rt, session_id=None):
    for _ in range(300):
        if session_id is None:
            if not rt.turns:
                return
        elif session_id not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"still running: {list(rt.turns)}")


def _agent_file(directory, name, body, *, description="Test agent", mode="all", tools=None):
    lines = ["---", f"description: {description}", f"mode: {mode}"]
    if tools is not None:
        lines.append(f"tools: [{', '.join(tools)}]")
    lines += ["---", body, ""]
    (directory / f"{name}.md").write_text("\n".join(lines))


async def _context(client, session_id):
    return (await client.get(f"/api/session/{session_id}/context")).json()


async def test_agent_changes_affect_new_sessions_only(env):
    rt, client, provider, workspace, _ = env
    files = workspace / ".agents" / "agents"
    _agent_file(files, "build", "Project build one.\n\n{{tools}}", description="One", tools=["read"])

    first = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    before = await _context(client, first["id"])
    assert "Project build one." in before["instructions"]
    assert [t["name"] for t in before["tools"]] == ["read", "expose_workspace_tools"]

    _agent_file(files, "build", "Project build two.\n\n{{tools}}", description="Two", tools=["read", "write"])
    unchanged = await _context(client, first["id"])
    assert unchanged["instructions"] == before["instructions"]
    assert unchanged["tools"] == before["tools"]

    second = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    after = await _context(client, second["id"])
    assert "Project build two." in after["instructions"] and "Project build one." not in after["instructions"]
    assert [t["name"] for t in after["tools"]] == ["read", "write", "expose_workspace_tools"]

    # A deleted override no longer applies to new sessions, but the existing session keeps its snapshot.
    (files / "build.md").unlink()
    third = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    builtin = await _context(client, third["id"])
    assert "You are an AI agent running in Nexus" in builtin["instructions"]
    assert "Project build two." not in builtin["instructions"]
    assert (await _context(client, first["id"]))["instructions"] == before["instructions"]

    # The real outgoing request uses the stored prompt and tool schemas too.
    provider.replies = [[Text("ok")]]
    await client.post(f"/api/session/{first['id']}/prompt", json={"text": "go"})
    await _idle(rt, first["id"])
    request = provider.requests[0]
    assert request.instructions == before["instructions"]
    assert [t.name for t in request.tools] == ["read", "expose_workspace_tools"]
    await rt.shutdown()


async def test_agents_md_changes_affect_new_sessions_only_and_absence_is_explicit(env):
    rt, client, _, workspace, _ = env
    (workspace / "AGENTS.md").write_text("Use tabs in this workspace.\n")
    first = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    before = await _context(client, first["id"])
    assert "# Instructions file" in before["instructions"]
    assert "Use tabs in this workspace." in before["instructions"]
    assert before["instructions"].index("# Instructions file") > before["instructions"].index("# Environment")

    (workspace / "AGENTS.md").write_text("Use spaces now.\n")
    assert (await _context(client, first["id"]))["instructions"] == before["instructions"]
    second = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    assert "Use spaces now." in (await _context(client, second["id"]))["instructions"]

    (workspace / "AGENTS.md").unlink()
    third = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    missing = await _context(client, third["id"])
    assert "Status: not found" in missing["instructions"]
    assert "No instructions file was loaded" in missing["instructions"]
    assert (await client.get(f"/api/session/{third['id']}")).json()["snapshot"]["instructions"]["status"] == "missing"

    # A directory in place of AGENTS.md is reported, not silently treated as absent.
    (workspace / "AGENTS.md").mkdir()
    unreadable = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    body = await _context(client, unreadable["id"])
    assert "Status: unreadable" in body["instructions"] and "No instructions were loaded" in body["instructions"]
    await rt.shutdown()


async def test_instruction_file_size_limit_is_announced(env):
    rt, client, _, workspace, _ = env
    (workspace / "AGENTS.md").write_text("x" * (256 * 1024 + 1))
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    body = await _context(client, session["id"])
    assert "Status: too large" in body["instructions"]
    assert "No instructions were loaded" in body["instructions"] and "limit" in body["instructions"]
    assert body["instructions"].count("x" * 100) == 0
    await rt.shutdown()


async def test_restart_reuses_the_snapshot_even_after_file_changes(env):
    rt, client, _, workspace, _ = env
    files = workspace / ".agents" / "agents"
    _agent_file(files, "build", "Restart prompt one.\n\n{{tools}}", tools=["read"])
    (workspace / "AGENTS.md").write_text("Restart instructions one.\n")
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    before = await _context(client, session["id"])

    _agent_file(files, "build", "Restart prompt two.\n\n{{tools}}", tools=["read", "write"])
    (workspace / "AGENTS.md").write_text("Restart instructions two.\n")
    await rt.shutdown()
    rt.store.close()

    store = Store(rt.store.path)
    restarted = make_runtime("scripted/m", store)
    app = build_app(restarted, token=TOKEN, url="http://test")
    client2 = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                headers={"authorization": f"Bearer {TOKEN}"})
    after = (await client2.get(f"/api/session/{session['id']}/context")).json()
    assert after == before
    assert (await client2.get(f"/api/session/{session['id']}")).json()["snapshot"]["origin"] == "created"
    await restarted.shutdown()
    await client2.aclose()


async def test_date_rollover_leaves_the_frozen_prompt_alone(env, monkeypatch):
    rt, client, _, workspace, _ = env
    files = workspace / ".agents" / "agents"
    _agent_file(files, "build", "Date test.\n\n{{tools}}")

    class FakeDateTime:
        current = real_datetime(2030, 1, 1, 12)

        @classmethod
        def now(cls):
            return cls.current

    monkeypatch.setattr(agents_module, "datetime", FakeDateTime)
    first = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    before = await _context(client, first["id"])
    assert "Today: 2030-01-01" in before["instructions"]

    FakeDateTime.current = real_datetime(2031, 2, 2, 12)
    assert (await _context(client, first["id"]))["instructions"] == before["instructions"]
    second = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    assert "Today: 2031-02-02" in (await _context(client, second["id"]))["instructions"]
    await rt.shutdown()


async def test_root_and_child_snapshots_are_independent(env):
    rt, client, provider, workspace, _ = env
    files = workspace / ".agents" / "agents"
    _agent_file(files, "helper", "Help one.\n\n{{tools}}", description="Helper one", mode="subagent")
    (workspace / "AGENTS.md").write_text("Parent instructions.\n")

    parent = (await client.post("/api/session", json={
        "directory": str(workspace), "agent": "orchestrate", "permission": "allow"})).json()
    child = (await client.post("/api/session", json={"parent": parent["id"], "agent": "helper"})).json()
    parent_before = await _context(client, parent["id"])
    child_before = await _context(client, child["id"])
    assert "`helper`: Helper one" in next(t for t in parent_before["tools"] if t["name"] == "session")["description"]
    assert "Help one." in child_before["instructions"]

    _agent_file(files, "helper", "Help two.\n\n{{tools}}", description="Helper two", mode="subagent")
    _agent_file(files, "extra", "Extra.\n\n{{tools}}", description="Extra agent", mode="subagent")

    # Both existing snapshots stay exactly as they were.
    assert (await _context(client, parent["id"])) == parent_before
    assert (await _context(client, child["id"])) == child_before

    # A new parent picks up the description change and the added agent; a new child gets the new prompt.
    parent2 = (await client.post("/api/session", json={
        "directory": str(workspace), "agent": "orchestrate", "permission": "allow"})).json()
    child2 = (await client.post("/api/session", json={"parent": parent2["id"], "agent": "helper"})).json()
    session_tool = rt.store.snapshot(parent2["id"])["tool_definitions"]["session"]["description"]
    assert "`helper`: Helper two" in session_tool and "`extra`: Extra agent" in session_tool
    assert "Help two." in (await _context(client, child2["id"]))["instructions"]

    # A parent that names a deleted child agent gets a clear error and no silent substitute.
    (files / "helper.md").unlink()
    children_before = (await client.get(f"/api/session/{parent['id']}/children")).json()
    provider.replies = [[ToolCall("c1", "session", json.dumps({"agent": "helper", "prompt": "work"}))], [Text("done")]]
    await client.post(f"/api/session/{parent['id']}/prompt", json={"text": "delegate"})
    await _idle(rt, parent["id"])
    messages = (await client.get(f"/api/session/{parent['id']}/message")).json()
    result = next(p["output"] for m in messages for p in m["parts"] if p["type"] == "tool_result")
    assert "unknown agent 'helper'" in result and "available:" in result
    assert (await client.get(f"/api/session/{parent['id']}/children")).json() == children_before
    await rt.shutdown()


async def test_unknown_agent_tool_fails_at_creation_with_a_clear_error(env):
    rt, client, _, workspace, _ = env
    _agent_file(workspace / ".agents" / "agents", "bad", "Bad.\n\n{{tools}}", tools=["nope"])
    response = await client.post("/api/session", json={"directory": str(workspace), "agent": "bad"})
    assert response.status_code == 400
    assert "unknown tools: nope" in response.json()["error"] and "available:" in response.json()["error"]
    assert (await client.get("/api/session", params={"directory": str(workspace)})).json() == []
    await rt.shutdown()


async def test_agent_switch_is_the_deliberate_prompt_replacement(env):
    rt, client, _, workspace, store = env
    files = workspace / ".agents" / "agents"
    _agent_file(files, "build", "Build one.\n\n{{tools}}")
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    sid = session["id"]
    assert "Build one." in (await _context(client, sid))["instructions"]

    _agent_file(files, "build", "Build two.\n\n{{tools}}")
    switched = (await client.post(f"/api/session/{sid}/agent", json={"agent": "orchestrate"})).json()
    assert switched["agent"] == "orchestrate" and switched["snapshot"]["origin"] == "agent-switch"
    orchestrate = await _context(client, sid)
    assert "You are the orchestrator" in orchestrate["instructions"]
    assert "Build two." not in orchestrate["instructions"]

    back = (await client.post(f"/api/session/{sid}/agent", json={"agent": "build"})).json()
    assert back["snapshot"]["origin"] == "agent-switch"
    rebuilt = await _context(client, sid)
    assert "Build two." in rebuilt["instructions"]  # the explicit switch reloads current files
    assert store.snapshot(sid)["system_prompt"] == rebuilt["instructions"]
    messages = (await client.get(f"/api/session/{sid}/message")).json()
    notices = [p["text"] for m in messages for p in m["parts"] if "agent changed" in (p.get("text") or "")]
    assert notices == []  # nothing was sent yet: the new snapshot is the starting context
    store.append_message(sid, Message("user", (Text("hello"),)))
    await client.post(f"/api/session/{sid}/agent", json={"agent": "orchestrate"})
    messages = (await client.get(f"/api/session/{sid}/message")).json()
    notices = [p["text"] for m in messages for p in m["parts"] if "agent changed" in (p.get("text") or "")]
    assert len(notices) == 1
    await rt.shutdown()


async def test_disabled_tools_are_absent_from_prompt_schema_and_nested_code(env):
    rt, client, provider, workspace, store = env
    session = (await client.post("/api/session", json={
        "directory": str(workspace), "permission": "allow", "disabled_tools": ["bash", "write"]})).json()
    sid = session["id"]
    context = await _context(client, sid)
    names = [t["name"] for t in context["tools"]]
    assert "bash" not in names and "write" not in names
    assert "`bash`" not in context["instructions"] and "`write`" not in context["instructions"]
    snapshot = store.snapshot(sid)
    assert "bash" not in snapshot["tool_definitions"] and "write" not in snapshot["tool_definitions"]

    provider.replies = [[ToolCall("c1", "code", json.dumps({
        "code": 'try:\n    await write(path="forbidden", content="no")\nexcept Exception as e:\n    print(type(e).__name__, e)'}))],
        [Text("done")]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "try"})
    await _idle(rt, sid)
    messages = (await client.get(f"/api/session/{sid}/message")).json()
    output = next(p["output"] for m in messages for p in m["parts"] if p["type"] == "tool_result")
    assert "write" in output and "Error" in output
    assert not (workspace / "forbidden").exists()

    # Clearing a switch adds the schema and forward guidance, without rewriting the frozen prompt.
    enabled = (await client.patch(f"/api/session/{sid}", json={"disabled_tools": []})).json()
    assert "write" in enabled["tools"] and "write" in store.snapshot(sid)["tool_definitions"]
    context = await _context(client, sid)
    assert "`write`" not in context["instructions"]  # the starting prompt stays frozen
    messages = (await client.get(f"/api/session/{sid}/message")).json()
    notice = next(p["text"] for m in messages for p in m["parts"] if "tool availability changed" in (p.get("text") or ""))
    assert "Now available:" in notice and "`write`:" in notice
    assert "write" in [t["name"] for t in context["tools"]]
    await rt.shutdown()


async def test_explicit_updates_apply_to_subsequent_calls_only(env):
    rt, client, provider, workspace, _ = env
    files = workspace / ".agents" / "agents"
    _agent_file(files, "build", "Running prompt.\n\n{{tools}}")
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    sid = session["id"]

    gate = asyncio.Event()
    provider.gates = [gate]
    provider.replies = [[Text("first")], [Text("second")]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "first"})
    for _ in range(200):
        if provider.requests:
            break
        await asyncio.sleep(0.01)
    assert provider.requests

    # A running call keeps the configuration it started with.
    _agent_file(files, "build", "Changed on disk.\n\n{{tools}}", tools=["read"])
    patched = (await client.patch(f"/api/session/{sid}", json={
        "model": "scripted/other", "disabled_tools": ["write"]})).json()
    assert patched["model"] == "scripted/other" and "write" not in patched["tools"]
    assert "write" not in rt.store.snapshot(sid)["tool_definitions"]
    assert provider.requests[0].model == "m"
    assert "Running prompt." in provider.requests[0].instructions
    assert "write" in [t.name for t in provider.requests[0].tools]

    gate.set()
    await _idle(rt, sid)
    assert "write" in [t.name for t in provider.requests[0].tools]  # still the first call

    await client.post(f"/api/session/{sid}/prompt", json={"text": "second"})
    await _idle(rt, sid)
    second = provider.requests[1]
    assert second.model == "other"
    assert "write" not in [t.name for t in second.tools]
    assert "Running prompt." in second.instructions and "Changed on disk." not in second.instructions
    messages = (await client.get(f"/api/session/{sid}/message")).json()
    notice = next(p["text"] for m in messages for p in m["parts"] if "tool availability changed" in (p.get("text") or ""))
    assert "`write`" in notice and "starting system prompt is frozen" in notice
    await rt.shutdown()


async def test_old_schema_migration_preserves_messages_and_is_stable(env):
    rt, client, provider, workspace, store = env
    files = workspace / ".agents" / "agents"
    _agent_file(files, "build", "Before upgrade.\n\n{{tools}}")
    (workspace / "AGENTS.md").write_text("Before upgrade instructions.\n")
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    sid = session["id"]
    provider.replies = [[Text("hello")]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "hi"})
    await _idle(rt, sid)
    assert len((await client.get(f"/api/session/{sid}/message")).json()) == 2

    # Simulate the schema from before session snapshots existed.
    path = store.path
    await rt.shutdown()
    with store.db:
        store.db.execute("drop table session_snapshot")
    store.close()

    _agent_file(files, "build", "After upgrade.\n\n{{tools}}")
    (workspace / "AGENTS.md").write_text("After upgrade instructions.\n")

    def restart():
        s = Store(path)
        r = make_runtime("scripted/m", s)
        a = build_app(r, token=TOKEN, url="http://test")
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://test",
                              headers={"authorization": f"Bearer {TOKEN}"})
        return r, s, c

    rt2, store2, client2 = restart()
    first = await _context(client2, sid)
    assert "This session predates stored session snapshots" in first["instructions"]
    assert "After upgrade." in first["instructions"] and "After upgrade instructions." in first["instructions"]
    info = (await client2.get(f"/api/session/{sid}")).json()["snapshot"]
    assert info["origin"] == "reconstructed" and "not a record of the exact prompt" in info["notice"]
    full = (await client2.get(f"/api/session/{sid}/snapshot")).json()
    assert full["origin"] == "reconstructed" and full["system_prompt"] == first["instructions"]
    assert full["agent"]["path"].endswith("build.md") and full["agent"]["fingerprint"].startswith("sha256:")
    assert full["instruction_file"]["status"] == "loaded"
    second = await _context(client2, sid)
    assert second["instructions"] == first["instructions"]
    assert len((await client2.get(f"/api/session/{sid}/message")).json()) == 2
    events = (await client2.get(f"/api/session/{sid}/event")).json()["events"]
    assert sum(1 for e in events if e["type"] == "session.snapshot") == 1

    # Restart keeps the reconstructed snapshot; it does not reconstruct a different one.
    await rt2.shutdown()
    store2.close()
    rt3, store3, client3 = restart()
    third = (await client3.get(f"/api/session/{sid}/context")).json()
    assert third["instructions"] == first["instructions"]
    assert (await client3.get(f"/api/session/{sid}/snapshot")).json()["origin"] == "reconstructed"
    await rt3.shutdown()
    store3.close()
    await client2.aclose()
    await client3.aclose()


async def test_context_inspection_matches_the_actual_request(env):
    rt, client, provider, workspace, _ = env
    files = workspace / ".agents" / "agents"
    _agent_file(files, "build", "Context match.\n\n{{tools}}", tools=["read", "bash"])
    (workspace / "AGENTS.md").write_text("Project instructions.\n")
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    sid = session["id"]
    context = await _context(client, sid)
    assert "Project instructions." in context["instructions"]

    provider.replies = [[Text("done")]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "next"})
    await _idle(rt, sid)
    actual = provider.body(provider.requests[0])
    assert actual["instructions"] == context["instructions"]
    assert actual["tools"] == context["tools"]
    assert (actual["model"], actual["effort"]) == (context["model"], context["effort"])
    assert actual["messages"] == context["messages"] + 1  # the new user message
    await rt.shutdown()


async def test_updates_take_effect_within_turn_and_notices_follow_results(env):
    rt, client, provider, workspace, _ = env
    session = (await client.post("/api/session", json={
        "directory": str(workspace), "permission": "allow"})).json()
    sid = session["id"]
    gate = asyncio.Event()
    provider.gates = [gate]
    provider.replies = [[ToolCall("write-one", "write", json.dumps({"path": "blocked", "content": "no"}))],
                        [Text("finished")]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "work"})
    for _ in range(200):
        if provider.requests:
            break
        await asyncio.sleep(0.01)
    assert provider.requests
    await client.patch(f"/api/session/{sid}", json={
        "model": "scripted/new", "effort": "high", "disabled_tools": ["write"]})
    # Do not put a notice into stored context ahead of the in-flight assistant message.
    assert len(rt.store.messages(sid)) == 1
    gate.set()
    await _idle(rt, sid)
    assert provider.requests[0].model == "m"
    assert "write" in [t.name for t in provider.requests[0].tools]
    second = provider.requests[1]
    assert second.model == "new" and second.reasoning_effort == "high"
    assert "write" not in [t.name for t in second.tools]
    assert not (workspace / "blocked").exists()
    assert [m.role for m in second.messages] == ["user", "assistant", "tool", "user"]
    assert "tool availability changed" in second.messages[-1].parts[0].text
    assert "unknown tool" in second.messages[-2].parts[0].output
    await rt.shutdown()


async def test_snapshot_does_not_expose_control_tools_inside_code(env):
    rt, client, provider, workspace, _ = env
    session = (await client.post("/api/session", json={
        "directory": str(workspace), "agent": "orchestrate", "permission": "allow"})).json()
    sid = session["id"]
    assert "session" in session["tools"]
    provider.replies = [[ToolCall("cell", "code", json.dumps({"code":
        'try:\n    await session(agent="build", prompt="must not launch")\nexcept Exception as e:\n    print(type(e).__name__, e)'}))],
        [Text("done")]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "check isolation"})
    await _idle(rt, sid)
    assert not rt.store.children(sid)
    results = [p for m in rt.store.messages(sid) for p in m.message.parts if hasattr(p, "output")]
    assert any("session" in p.output and "Error" in p.output for p in results)
    await rt.shutdown()


async def test_running_tool_finishes_but_next_tool_uses_updated_switches(env, monkeypatch):
    from nexus.tools import Tool

    rt, client, provider, workspace, _ = env
    entered, release = asyncio.Event(), asyncio.Event()
    registry = rt._registry()

    async def blocked_read(args, ctx):
        entered.set()
        await release.wait()
        return "read finished"

    registry["read"] = Tool(registry["read"].spec, blocked_read)
    monkeypatch.setattr(rt, "_registry", lambda: registry)
    sid = (await client.post("/api/session", json={
        "directory": str(workspace), "permission": "allow"})).json()["id"]
    provider.replies = [[ToolCall("one", "read", '{"path":"x"}'),
                         ToolCall("two", "write", '{"path":"blocked","content":"no"}')], [Text("done")]]
    await client.post(f"/api/session/{sid}/prompt", json={"text": "work"})
    await asyncio.wait_for(entered.wait(), 2)
    await client.patch(f"/api/session/{sid}", json={"disabled_tools": ["read", "write"]})
    release.set()
    await _idle(rt, sid)
    results = [p for m in rt.store.messages(sid) for p in m.message.parts if hasattr(p, "output")]
    assert results[0].output == "read finished"
    assert "unknown tool" in results[1].output
    assert not (workspace / "blocked").exists()
    await rt.shutdown()
