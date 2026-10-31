"""Workspace controls: on-demand tools, directory moves and Git worktrees.

Everything runs offline with scripted providers, temporary homes and temporary
Git repositories. A live provider is not needed: the contracts are checked on
the request objects the runtime hands to `provider.stream()`, the HTTP API,
stored events and the files on disk.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from conftest import remembering

from nexus import runtime as runtime_module
from nexus import worktrees as worktrees_module
from nexus.llmhub.types import Done, PartDone, Text, ToolCall
from nexus.server import build_app, make_runtime
from nexus.store import Store
from nexus.tools import Tool, ToolError

TOKEN = "t0ken"
TASK_TOOLS = ["read", "grep", "glob", "ls", "bash", "bash_output", "bash_kill", "edit", "patch", "write", "web_search", "web_fetch", "ask", "code"]


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


class Env:
    def __init__(self, rt, client, provider, workspace, store, tmp_path):
        self.rt, self.client, self.provider, self.workspace = rt, client, provider, workspace
        self.store, self.tmp_path = store, tmp_path


def git(cwd, *args):
    result = subprocess.run(
        ["git", "-c", "user.name=Nexus Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    git(workspace, "init", "-q", "-b", "main")
    (workspace / "a.txt").write_text("old file\n")
    (workspace / "AGENTS.md").write_text("Old rules.\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "init")
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    store = Store(tmp_path / "nexus.db")
    rt = make_runtime(remembering(store, "scripted/m"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    return Env(rt, client, provider, workspace, store, tmp_path)


async def _idle(rt, session_id=None):
    for _ in range(400):
        if session_id is None:
            if not rt.turns:
                return
        elif session_id not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"still running: {list(rt.turns)}")


async def _create(client, workspace, **fields):
    response = await client.post("/api/session", json={"directory": str(workspace), **fields})
    assert response.status_code == 200, response.text
    return response.json()


def _results(messages):
    return [p for m in messages for p in m["parts"] if p["type"] == "tool_result"]


def _texts(messages):
    return [p.get("text") or p.get("output") for m in messages for p in m["parts"]]


async def _messages_of(client, sid):
    return (await client.get(f"/api/session/{sid}/message")).json()


async def _context(client, sid):
    return (await client.get(f"/api/session/{sid}/context")).json()


async def _events(client, sid, type_=None):
    events = (await client.get(f"/api/session/{sid}/event", params={"limit": 5000})).json()["events"]
    return [e for e in events if type_ is None or e["type"] == type_]


def test_workspace_control_names_are_consistent_everywhere():
    from nexus import agents as agents_module
    from nexus import workspace as workspace_module

    assert set(agents_module.NEXUS_CONTROL_TOOLS) == {"session", "message_session", "escalate", *workspace_module.CONTROL_NAMES}
    assert workspace_module.WORKSPACE_TOOLS == ("change_session_directory", "worktree")
    assert workspace_module.EXPOSE == "expose_workspace_tools"


# -- activation -----------------------------------------------------------------


async def test_workspace_tools_are_hidden_until_exposed_and_then_callable(env):
    destination = env.tmp_path / "elsewhere"
    destination.mkdir()
    (destination / "AGENTS.md").write_text("Elsewhere rules.\n")
    session = await _create(env.client, env.workspace)
    sid = session["id"]

    context = await _context(env.client, sid)
    names = [t["name"] for t in context["tools"]]
    assert names == TASK_TOOLS + ["expose_workspace_tools"]
    assert "`expose_workspace_tools`" not in context["instructions"]
    assert "`change_session_directory`" not in context["instructions"]
    assert "`worktree`" not in context["instructions"]
    assert session["workspace"] == {"entry": True, "active": False}
    assert session["tools"] == names

    env.provider.replies = [
        [ToolCall("e1", "expose_workspace_tools", "{}")],
        [ToolCall("d1", "change_session_directory", json.dumps({"directory": str(destination)}))],
        [Text("moved")],
    ]
    await env.client.post(f"/api/session/{sid}/prompt", json={"text": "work elsewhere"})
    await _idle(env.rt, sid)

    # The call that activated did not see the new schemas; the next model call did.
    assert env.provider.requests[0].tools and "change_session_directory" not in [
        t.name for t in env.provider.requests[0].tools]
    assert "change_session_directory" in [t.name for t in env.provider.requests[1].tools]
    assert "worktree" in [t.name for t in env.provider.requests[1].tools]

    messages = await _messages_of(env.client, sid)
    activation = next(output for output in _texts(messages) if "workspace control tools activated" in (output or ""))
    assert "### `change_session_directory`" in activation and "Parameters:" in activation
    assert "### `worktree`" in activation
    assert "List, create or delete" in activation

    refreshed = (await env.client.get(f"/api/session/{sid}")).json()
    assert refreshed["workspace"] == {"entry": True, "active": True}
    assert "change_session_directory" in refreshed["tools"] and "worktree" in refreshed["tools"]
    context = await _context(env.client, sid)
    assert "change_session_directory" in [t["name"] for t in context["tools"]]
    # The frozen prompt still names only the entry point.
    assert "`change_session_directory`" not in context["instructions"]
    # The tool from the second model call really ran.
    assert Path(refreshed["directory"]) == destination.resolve()
    assert not await _events(env.client, sid, "permission.asked")  # activation and moving never ask
    await env.rt.shutdown()


async def test_activation_is_idempotent_and_survives_restart(env):
    session = await _create(env.client, env.workspace)
    sid = session["id"]
    first = (await env.client.post(f"/api/session/{sid}/workspace-tools", json={})).json()
    assert first["workspace"] == {"entry": True, "active": True}
    messages_before = await _messages_of(env.client, sid)
    second = (await env.client.post(f"/api/session/{sid}/workspace-tools", json={})).json()
    assert second["workspace"] == {"entry": True, "active": True}
    assert len(await _messages_of(env.client, sid)) == len(messages_before)
    notices = [t for t in _texts(messages_before) if "workspace control tools activated" in (t or "")]
    assert len(notices) == 1
    assert len(await _events(env.client, sid, "session.workspace_tools")) == 1
    assert "change_session_directory" in [t["name"] for t in (await _context(env.client, sid))["tools"]]

    path = env.store.path
    await env.rt.shutdown()
    env.store.close()
    store = Store(path)
    rt = make_runtime(remembering(store, "scripted/m"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    restored = (await client.get(f"/api/session/{sid}")).json()
    assert restored["workspace"] == {"entry": True, "active": True}
    assert {"change_session_directory", "worktree"} <= set(restored["tools"])
    snapshot = (await client.get(f"/api/session/{sid}/snapshot")).json()
    assert snapshot["workspace"] == {"entry": True, "activated": True}
    assert "change_session_directory" in snapshot["tool_definitions"]
    await rt.shutdown()
    store.close()
    await client.aclose()


async def test_parent_and_child_activation_are_independent(env):
    parent = await _create(env.client, env.workspace, agent="orchestrate")
    child = (await env.client.post("/api/session", json={"parent": parent["id"], "agent": "build"})).json()
    await env.client.post(f"/api/session/{parent['id']}/workspace-tools", json={})

    parent_context = await _context(env.client, parent["id"])
    child_context = await _context(env.client, child["id"])
    assert "change_session_directory" in [t["name"] for t in parent_context["tools"]]
    assert "change_session_directory" not in [t["name"] for t in child_context["tools"]]
    assert (await env.client.get(f"/api/session/{child['id']}")).json()["workspace"]["active"] is False

    await env.client.post(f"/api/session/{child['id']}/workspace-tools", json={})
    assert "change_session_directory" in [t["name"] for t in (await _context(env.client, child["id"]))["tools"]]
    assert (await env.client.get(f"/api/session/{parent['id']}")).json()["workspace"]["active"] is True
    await env.rt.shutdown()


async def test_workspace_control_tools_are_not_available_inside_code(env):
    session = await _create(env.client, env.workspace, permission="allow")
    sid = session["id"]
    await env.client.post(f"/api/session/{sid}/workspace-tools", json={})
    destination = env.tmp_path / "inside-code"
    destination.mkdir()
    code = (
        "try:\n"
        "    await expose_workspace_tools()\n"
        "    print('expose_workspace_tools RAN')\n"
        "except Exception as e:\n"
        "    print('expose_workspace_tools', type(e).__name__)\n"
        "try:\n"
        f"    await change_session_directory(directory={str(destination)!r})\n"
        "    print('change_session_directory RAN')\n"
        "except Exception as e:\n"
        "    print('change_session_directory', type(e).__name__)\n"
        "try:\n"
        "    await worktree(action='list')\n"
        "    print('worktree RAN')\n"
        "except Exception as e:\n"
        "    print('worktree', type(e).__name__)\n"
    )
    env.provider.replies = [[ToolCall("c1", "code", json.dumps({"code": code}))], [Text("done")]]
    await env.client.post(f"/api/session/{sid}/prompt", json={"text": "try"})
    await _idle(env.rt, sid)
    output = next(r["output"] for r in _results(await _messages_of(env.client, sid)) if "kernel" in r["output"])
    assert "RAN" not in output
    assert all(name in output for name in ("expose_workspace_tools", "change_session_directory", "worktree"))
    assert (await env.client.get(f"/api/session/{sid}")).json()["directory"] == str(env.workspace.resolve())
    await env.rt.shutdown()


# -- directory changes -----------------------------------------------------------


async def test_directory_change_waits_for_the_running_call_and_rebinds_later_calls(env, monkeypatch):
    destination = env.tmp_path / "later"
    destination.mkdir()
    session = await _create(env.client, env.workspace, permission="allow")
    sid = session["id"]

    entered, release = asyncio.Event(), asyncio.Event()
    seen: list[Path] = []
    registry = env.rt._registry()
    original = registry["read"]

    async def blocked_read(args, ctx):
        seen.append(ctx.workspace)
        entered.set()
        await release.wait()
        return "read finished"

    registry["read"] = Tool(original.spec, blocked_read)
    monkeypatch.setattr(env.rt, "_registry", lambda: registry)

    env.provider.replies = [
        [ToolCall("read-1", "read", '{"path": "a.txt"}'),
         ToolCall("bash-1", "bash", '{"command": "pwd"}')],
        [Text("done")],
    ]
    await env.client.post(f"/api/session/{sid}/prompt", json={"text": "work"})
    await asyncio.wait_for(entered.wait(), 5)

    response = await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(destination)})
    assert response.status_code == 202
    body = response.json()
    assert body["change"]["status"] == "pending"
    assert body["directory_change"]["status"] == "pending"
    assert body["directory"] == str(env.workspace.resolve())
    conflict = await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(destination)})
    assert conflict.status_code == 409

    release.set()
    await _idle(env.rt, sid)

    assert seen and seen[0] == env.workspace.resolve()  # the running call kept its directory
    messages = await _messages_of(env.client, sid)
    outputs = [r["output"] for r in _results(messages)]
    assert any(output.startswith(f"[cwd: {destination.resolve()}]") for output in outputs)  # bash pwd ran there
    notice_index = next(i for i, m in enumerate(messages)
                        if m["role"] == "user" and "session directory changed" in m["parts"][0]["text"])
    tool_index = next(i for i, m in enumerate(messages) if m["role"] == "tool")
    assert notice_index > tool_index  # the announcement followed the running call's results
    assert str(destination.resolve()) in messages[notice_index]["parts"][0]["text"]
    session = (await env.client.get(f"/api/session/{sid}")).json()
    assert session["directory"] == str(destination.resolve())
    assert session["directory_change"] is None
    assert session["directory_changed"] is True
    changed = await _events(env.client, sid, "session.directory_changed")
    assert len(changed) == 1
    assert changed[0]["data"]["from"] == str(env.workspace.resolve())
    assert changed[0]["data"]["to"] == str(destination.resolve())
    assert changed[0]["data"]["frozen_directory"] == str(env.workspace.resolve())
    await env.rt.shutdown()


async def test_directory_change_loads_destination_instructions_and_keeps_history(env):
    destination = env.tmp_path / "destination"
    destination.mkdir()
    (destination / "AGENTS.md").write_text("New rules.\n")
    session = await _create(env.client, env.workspace)
    sid = session["id"]
    env.provider.replies = [[Text("hello")]]
    await env.client.post(f"/api/session/{sid}/prompt", json={"text": "start"})
    await _idle(env.rt, sid)
    before_context = await _context(env.client, sid)
    before_messages = await _messages_of(env.client, sid)
    assert "Old rules." in before_context["instructions"]
    assert env.provider.requests[0].instructions == before_context["instructions"]

    response = await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(destination)})
    assert response.status_code == 200
    assert response.json()["change"]["status"] == "applied"
    after_context = await _context(env.client, sid)
    assert after_context["instructions"] == before_context["instructions"]
    assert "Old rules." in after_context["instructions"] and "New rules." not in after_context["instructions"]

    messages = await _messages_of(env.client, sid)
    assert messages[: len(before_messages)] == before_messages
    notice = messages[-1]["parts"][0]["text"]
    assert messages[-1]["role"] == "user"
    assert "session directory changed" in notice
    assert str(env.workspace.resolve()) in notice and str(destination.resolve()) in notice
    assert "New rules." in notice and "Status: loaded" in notice
    assert "session start are unchanged" in notice

    current = (await env.client.get(f"/api/session/{sid}")).json()
    assert current["directory"] == str(destination.resolve())
    assert current["starting_directory"] == str(env.workspace.resolve())
    assert current["project_id"] == session["project_id"]
    snapshot = (await env.client.get(f"/api/session/{sid}/snapshot")).json()
    assert snapshot["directory"] == str(env.workspace.resolve())
    assert snapshot["system_prompt"] == before_context["instructions"]
    assert snapshot["instruction_file"]["path"] == str(env.workspace / "AGENTS.md")
    assert snapshot["current_instruction_file"]["path"] == str(destination / "AGENTS.md")
    assert len(snapshot["directory_changes"]) == 1

    event = (await _events(env.client, sid, "session.directory_changed"))[0]["data"]
    assert event["instructions"]["status"] == "loaded"
    assert event["project_id"] == session["project_id"]
    await env.rt.shutdown()


async def test_directory_change_reports_missing_unreadable_and_oversized_instructions(env):
    session = await _create(env.client, env.workspace)
    sid = session["id"]
    cases = []
    missing = env.tmp_path / "no-instructions"
    missing.mkdir()
    cases.append((missing, "Status: not found", "No instructions file was loaded"))
    unreadable = env.tmp_path / "unreadable"
    (unreadable / "AGENTS.md").mkdir(parents=True)
    cases.append((unreadable, "Status: unreadable", "No instructions were loaded"))
    oversized = env.tmp_path / "oversized"
    oversized.mkdir()
    (oversized / "AGENTS.md").write_text("x" * (256 * 1024 + 1))
    cases.append((oversized, "Status: too large", "limit 262144 bytes"))
    for directory, status, message in cases:
        response = await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(directory)})
        assert response.status_code == 200, response.text
        notice = (await _messages_of(env.client, sid))[-1]["parts"][0]["text"]
        assert status in notice and message in notice
        if directory == oversized:
            assert "x" * 100 not in notice
    await env.rt.shutdown()


async def test_direct_and_nested_tools_use_the_new_directory(env):
    destination = env.tmp_path / "moved"
    destination.mkdir()
    (destination / "a.txt").write_text("moved file\n")
    session = await _create(env.client, env.workspace, permission="allow")
    sid = session["id"]
    await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(destination)})

    env.provider.replies = [[ToolCall("r1", "read", '{"path": "a.txt"}')], [Text("read")]]
    await env.client.post(f"/api/session/{sid}/prompt", json={"text": "read"})
    await _idle(env.rt, sid)
    read_output = _results(await _messages_of(env.client, sid))[-1]["output"]
    assert read_output.startswith(f"[path: {destination.resolve()}/a.txt]")  # the moved cwd is explicit

    env.provider.replies = [[ToolCall("w1", "write", '{"path": "direct.txt", "content": "hi"}')], [Text("done")]]
    await env.client.post(f"/api/session/{sid}/prompt", json={"text": "write"})
    await _idle(env.rt, sid)
    assert (destination / "direct.txt").read_text() == "hi"
    assert not (env.workspace / "direct.txt").exists()

    code = 'await write(path="nested.txt", content="nested")'
    env.provider.replies = [[ToolCall("c1", "code", json.dumps({"code": code}))], [Text("done")]]
    await env.client.post(f"/api/session/{sid}/prompt", json={"text": "nest"})
    await _idle(env.rt, sid)
    assert (destination / "nested.txt").read_text() == "nested"
    assert not (env.workspace / "nested.txt").exists()
    await env.rt.shutdown()


async def test_read_observations_do_not_transfer_between_different_files(env):
    destination = env.tmp_path / "other"
    destination.mkdir()
    (destination / "a.txt").write_text("new file\n")
    session = await _create(env.client, env.workspace, permission="allow")
    sid = session["id"]
    env.provider.replies = [[ToolCall("r1", "read", '{"path": "a.txt"}')], [Text("read")]]
    await env.client.post(f"/api/session/{sid}/prompt", json={"text": "read"})
    await _idle(env.rt, sid)
    await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(destination)})

    # The old a.txt observation must not authorize the destination's a.txt.
    env.provider.replies = [[ToolCall("e1", "edit", json.dumps({
        "path": "a.txt", "old_string": "new file", "new_string": "changed"}))], [Text("try")]]
    await env.client.post(f"/api/session/{sid}/prompt", json={"text": "edit"})
    await _idle(env.rt, sid)
    result = _results(await _messages_of(env.client, sid))[-1]
    assert result["is_error"] and "has not been read in this session" in result["output"]
    assert (destination / "a.txt").read_text() == "new file\n"

    env.provider.replies = [
        [ToolCall("r2", "read", '{"path": "a.txt"}')],
        [Text("read")],
    ]
    await env.client.post(f"/api/session/{sid}/prompt", json={"text": "read it"})
    await _idle(env.rt, sid)
    env.provider.replies = [
        [ToolCall("e2", "edit", json.dumps({"path": "a.txt", "old_string": "new file", "new_string": "changed"}))],
        [Text("done")],
    ]
    await env.client.post(f"/api/session/{sid}/prompt", json={"text": "edit it"})
    await _idle(env.rt, sid)
    assert (destination / "a.txt").read_text() == "changed\n"
    await env.rt.shutdown()


async def test_new_children_inherit_the_directory_and_existing_children_keep_theirs(env):
    parent = await _create(env.client, env.workspace, agent="orchestrate")
    old_child = (await env.client.post("/api/session", json={"parent": parent["id"], "agent": "build"})).json()
    destination = env.tmp_path / "after-move"
    destination.mkdir()
    await env.client.post(f"/api/session/{parent['id']}/directory", json={"directory": str(destination)})
    new_child = (await env.client.post("/api/session", json={"parent": parent["id"], "agent": "build"})).json()
    assert old_child["directory"] == str(env.workspace.resolve())
    assert new_child["directory"] == str(destination.resolve())
    assert (await env.client.get(f"/api/session/{old_child['id']}")).json()["directory"] == str(env.workspace.resolve())
    await env.rt.shutdown()


async def test_tool_switches_work_after_activation_and_agent_switch_keeps_the_move(env):
    destination = env.tmp_path / "kept"
    destination.mkdir()
    (destination / "AGENTS.md").write_text("Kept rules.\n")
    session = await _create(env.client, env.workspace)
    sid = session["id"]
    await env.client.post(f"/api/session/{sid}/workspace-tools", json={})
    await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(destination)})

    # A switch must not reject the workspace control names as unknown executables.
    switched = (await env.client.patch(
        f"/api/session/{sid}", json={"disabled_tools": ["bash", "worktree"]})).json()
    assert "change_session_directory" in switched["tools"] and "worktree" not in switched["tools"]
    assert "bash" not in switched["tools"]
    cleared = (await env.client.patch(f"/api/session/{sid}", json={"disabled_tools": []})).json()
    assert "bash" in cleared["tools"] and "worktree" in cleared["tools"]

    changed_agent = (await env.client.post(f"/api/session/{sid}/agent", json={"agent": "orchestrate"})).json()
    assert changed_agent["directory"] == str(destination.resolve())
    assert changed_agent["workspace"] == {"entry": True, "active": True}
    snapshot = (await env.client.get(f"/api/session/{sid}/snapshot")).json()
    assert snapshot["current_instruction_file"]["path"] == str(destination / "AGENTS.md")
    assert len(snapshot["directory_changes"]) == 1
    await env.rt.shutdown()


async def test_directory_move_survives_restart(env):
    destination = env.tmp_path / "persisted"
    destination.mkdir()
    (destination / "AGENTS.md").write_text("Persisted rules.\n")
    session = await _create(env.client, env.workspace)
    sid = session["id"]
    before = await _context(env.client, sid)
    await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(destination)})
    path = env.store.path
    await env.rt.shutdown()
    env.store.close()

    store = Store(path)
    rt = make_runtime(remembering(store, "scripted/m"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    restored = (await client.get(f"/api/session/{sid}")).json()
    assert restored["directory"] == str(destination.resolve())
    assert restored["starting_directory"] == str(env.workspace.resolve())
    assert restored["directory_changed"] is True
    snapshot = (await client.get(f"/api/session/{sid}/snapshot")).json()
    assert snapshot["current_instruction_file"]["path"] == str(destination / "AGENTS.md")
    assert snapshot["directory_changes"][0]["to"] == str(destination.resolve())
    assert (await client.get(f"/api/session/{sid}/context")).json()["instructions"] == before["instructions"]
    await rt.shutdown()
    store.close()
    await client.aclose()


async def test_directory_move_before_an_old_session_is_reconstructed_keeps_the_old_start(env):
    session = await _create(env.client, env.workspace)
    sid = session["id"]
    path = env.store.path
    await env.rt.shutdown()
    with env.store.db:
        env.store.db.execute("delete from session_snapshot")
    env.store.close()
    destination = env.tmp_path / "before-reconstruction"
    destination.mkdir()

    store = Store(path)
    rt = make_runtime(remembering(store, "scripted/m"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    moved = (await client.post(f"/api/session/{sid}/directory", json={"directory": str(destination)})).json()
    assert moved["change"]["status"] == "applied"
    assert moved["directory"] == str(destination.resolve())
    assert moved["starting_directory"] == str(env.workspace.resolve())
    snapshot = (await client.get(f"/api/session/{sid}/snapshot")).json()
    assert snapshot["origin"] == "reconstructed"
    assert snapshot["directory"] == str(env.workspace.resolve())
    assert snapshot["current_instruction_file"]["path"] == str(destination / "AGENTS.md")
    await rt.shutdown()
    store.close()
    await client.aclose()


# -- worktrees -------------------------------------------------------------------


async def test_worktree_list_create_reuse_delete(env):
    session = await _create(env.client, env.workspace, permission="allow")
    sid = session["id"]
    repo = env.workspace.resolve()
    worktree = env.tmp_path / "wt-one"

    listing = (await env.client.get(f"/api/session/{sid}/worktrees")).json()
    assert listing["repository"] == str(repo) and listing["main"] == str(repo)
    assert [item["main"] for item in listing["worktrees"]] == [True]

    # A project with no session can show the same branch information.
    project_listing = (await env.client.get(
        f"/api/project/{session['project_id']}/worktrees")).json()
    assert project_listing["main"] == str(repo)
    assert project_listing["project_directory"] == str(repo)
    assert [item["path"] for item in project_listing["worktrees"]] == [str(repo)]

    # An uncommitted change stays in the original checkout and is not copied.
    (env.workspace / "uncommitted.txt").write_text("keep me\n")
    created = (await env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "create", "path": str(worktree), "branch": "feature"})).json()
    assert created["created"] is True and created["branch"] == "feature"
    assert created["path"] == str(worktree.resolve())
    assert created["session_directory"] == str(repo)
    assert (env.workspace / "uncommitted.txt").read_text() == "keep me\n"
    assert not (worktree / "uncommitted.txt").exists()
    assert (await env.client.get(f"/api/session/{sid}")).json()["directory"] == str(repo)
    assert str(worktree.resolve()) in git(env.workspace, "worktree", "list", "--porcelain")

    reused = (await env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "create", "path": str(worktree), "branch": "feature"})).json()
    assert reused["created"] is False and reused["reused"] is True and reused["branch"] == "feature"
    mismatch = await env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "create", "path": str(worktree), "branch": "other"})
    assert mismatch.status_code == 400 and "already a worktree" in mismatch.json()["error"]

    listing = (await env.client.get(f"/api/session/{sid}/worktrees")).json()
    entry = next(item for item in listing["worktrees"] if item["path"] == str(worktree.resolve()))
    assert entry["branch"] == "feature" and entry["dirty"] is False

    deleted = (await env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "delete", "path": str(worktree)})).json()
    assert deleted["path"] == str(worktree.resolve())
    assert not worktree.exists()
    assert "feature" in git(env.workspace, "branch", "--list", "feature")  # the branch is kept
    assert (await env.client.get(f"/api/session/{sid}")).json()["directory"] == str(repo)
    assert len(await _events(env.client, sid, "worktree.created")) == 2
    assert len(await _events(env.client, sid, "worktree.deleted")) == 1
    await env.rt.shutdown()


async def test_worktree_tool_lists_and_creates_without_moving_the_session(env):
    session = await _create(env.client, env.workspace, permission="allow")
    sid = session["id"]
    await env.client.post(f"/api/session/{sid}/workspace-tools", json={})
    worktree = env.tmp_path / "wt-tool"
    env.provider.replies = [
        [ToolCall("w1", "worktree", json.dumps({"action": "list"}))],
        [ToolCall("w2", "worktree", json.dumps({"action": "create", "path": str(worktree), "branch": "tool-branch"}))],
        [Text("done")],
    ]
    await env.client.post(f"/api/session/{sid}/prompt", json={"text": "worktrees"})
    await _idle(env.rt, sid)
    outputs = [r["output"] for r in _results(await _messages_of(env.client, sid))]
    assert any(output.startswith(f"Git worktrees of {env.workspace.resolve()}") for output in outputs)
    created = next(output for output in outputs if "created worktree" in output)
    assert str(worktree.resolve()) in created and "session directory is unchanged" in created
    assert (await env.client.get(f"/api/session/{sid}")).json()["directory"] == str(env.workspace.resolve())
    await env.rt.shutdown()


async def test_worktree_delete_refusals(env):
    session = await _create(env.client, env.workspace, permission="allow")
    sid = session["id"]
    repo = env.workspace.resolve()
    other = await _create(env.client, env.workspace)

    # The main checkout.
    main = await env.client.post(f"/api/session/{sid}/worktrees", json={"action": "delete", "path": str(repo)})
    assert main.status_code == 400 and "main checkout" in main.json()["error"]

    # A path that is not a registered worktree.
    unrelated = env.tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep")
    invalid = await env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "delete", "path": str(unrelated)})
    assert invalid.status_code == 400 and "not a registered worktree" in invalid.json()["error"]
    assert (unrelated / "keep.txt").exists()

    # A dirty worktree.
    dirty = env.tmp_path / "dirty-wt"
    await env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "create", "path": str(dirty), "branch": "dirty-branch"})
    (dirty / "untracked.txt").write_text("not committed")
    refused = await env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "delete", "path": str(dirty)})
    assert refused.status_code == 400 and "dirty worktree" in refused.json()["error"]
    assert dirty.exists()

    # A worktree used by a session (this one, after moving there).
    moved = (await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(dirty)})).json()
    assert moved["directory"] == str(dirty.resolve())
    in_use = await env.client.post(f"/api/session/{other['id']}/worktrees", json={
        "action": "delete", "path": str(dirty)})
    assert in_use.status_code == 400 and "directory of session" in in_use.json()["error"]
    assert dirty.exists()

    # Leaving first makes it deletable; its uncommitted file must be gone/committed first.
    (dirty / "untracked.txt").unlink()
    moved = (await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(repo)})).json()
    assert moved["directory"] == str(repo)

    # A project path is Nexus state too, even without a session.
    project = (await env.client.post("/api/project", json={"path": str(dirty)})).json()
    project_refused = await env.client.post(f"/api/session/{other['id']}/worktrees", json={
        "action": "delete", "path": str(dirty)})
    assert project_refused.status_code == 400 and "path of project" in project_refused.json()["error"]
    await env.client.delete(f"/api/project/{project['id']}")

    deleted = await env.client.post(f"/api/session/{other['id']}/worktrees", json={
        "action": "delete", "path": str(dirty)})
    assert deleted.status_code == 200 and not dirty.exists()
    await env.rt.shutdown()


async def test_conflicting_worktree_operations_are_serialized(env, monkeypatch):
    session = await _create(env.client, env.workspace)
    sid = session["id"]
    existing = env.tmp_path / "serialize-existing"
    await env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "create", "path": str(existing), "branch": "serialize-existing"})

    entered, release = asyncio.Event(), asyncio.Event()
    original = worktrees_module.create

    async def blocked_create(directory, path, branch=None, base=None):
        entered.set()
        await release.wait()
        return await original(directory, path, branch, base)

    monkeypatch.setattr(worktrees_module, "create", blocked_create)
    new_path = env.tmp_path / "serialize-new"
    creating = asyncio.create_task(env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "create", "path": str(new_path), "branch": "serialize-new"}))
    await asyncio.wait_for(entered.wait(), 5)
    deleting = asyncio.create_task(env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "delete", "path": str(existing)}))
    await asyncio.sleep(0.05)
    assert not deleting.done()  # it waits for the in-flight create instead of racing it
    release.set()
    created, deleted = await asyncio.gather(creating, deleting)
    assert created.status_code == 200 and created.json()["created"] is True
    assert deleted.status_code == 200 and not existing.exists()
    await env.rt.shutdown()


async def test_worktree_create_and_delete_validate_git_state(env):
    session = await _create(env.client, env.workspace)
    sid = session["id"]
    existing = env.tmp_path / "occupied"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep")
    occupied = await env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "create", "path": str(existing)})
    assert occupied.status_code == 400 and "already exists" in occupied.json()["error"]
    assert (existing / "keep.txt").read_text() == "keep"

    bad_branch = await env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "create", "path": str(env.tmp_path / "wt-bad"), "branch": "-bad"})
    assert bad_branch.status_code == 400 and "invalid branch" in bad_branch.json()["error"]

    bad_base = await env.client.post(f"/api/session/{sid}/worktrees", json={
        "action": "create", "path": str(env.tmp_path / "wt-base"), "branch": "new-one", "base": "no-such-ref"})
    assert bad_base.status_code == 400 and "not a commit or branch" in bad_base.json()["error"]
    assert not (env.tmp_path / "wt-base").exists()

    action = await env.client.post(f"/api/session/{sid}/worktrees", json={"action": "nope"})
    assert action.status_code == 400 and "action must be" in action.json()["error"]

    # A session outside Git clearly reports that worktree operations need a repository.
    plain_dir = env.tmp_path / "plain"
    plain_dir.mkdir()
    plain = await _create(env.client, plain_dir)
    no_git = await env.client.get(f"/api/session/{plain['id']}/worktrees")
    assert no_git.status_code == 400 and "not inside a Git repository" in no_git.json()["error"]
    await env.rt.shutdown()


# -- API surface and history ------------------------------------------------------


async def test_workspace_endpoints_require_the_token_and_validate_input(env):
    session = await _create(env.client, env.workspace)
    sid = session["id"]
    calls = [
        ("post", f"/api/session/{sid}/workspace-tools", {}),
        ("post", f"/api/session/{sid}/directory", {"directory": str(env.workspace)}),
        ("get", f"/api/session/{sid}/worktrees", None),
        ("post", f"/api/session/{sid}/worktrees", {"action": "list"}),
    ]
    for method, url, body in calls:
        kwargs = {"headers": {"authorization": "Bearer wrong"}}
        if body is not None:
            kwargs["json"] = body
        response = await getattr(env.client, method)(url, **kwargs)
        assert response.status_code == 401, (url, response.text)

    missing = await env.client.post("/api/session/ses_nope/directory", json={"directory": str(env.workspace)})
    assert missing.status_code == 404
    assert (await env.client.post("/api/session/ses_nope/workspace-tools", json={})).status_code == 404
    assert (await env.client.get("/api/session/ses_nope/worktrees")).status_code == 404

    assert (await env.client.post(f"/api/session/{sid}/directory", json={})).status_code == 400
    assert (await env.client.post(f"/api/session/{sid}/directory", json={
        "directory": str(env.tmp_path / "missing")})).status_code == 400
    file_path = env.tmp_path / "file.txt"
    file_path.write_text("x")
    assert (await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(file_path)})).status_code == 400
    assert (await env.client.post(f"/api/session/{sid}/worktrees", json={"action": "create"})).status_code == 400
    assert (await env.client.post(f"/api/session/{sid}/worktrees", json={"action": "delete"})).status_code == 400
    changed = (await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(env.workspace)})).json()
    assert changed["change"]["status"] == "unchanged"
    await env.rt.shutdown()


async def test_stored_pre_workspace_snapshot_keeps_its_tools_until_activation(env):
    session = await _create(env.client, env.workspace)
    sid = session["id"]
    # A snapshot stored before workspace controls existed: no entry point in the prompt or definitions.
    current_session = env.rt.session(sid)
    found = env.rt.agents(str(env.workspace))
    old = env.rt._build_snapshot(
        project=env.rt.project(current_session.project_id),
        directory=current_session.directory, spec=found.get("build", child=False), found=found,
        disabled=set(), child=False, workspace={"entry": False, "activated": False})
    env.store.set_snapshot(sid, old)

    current = (await env.client.get(f"/api/session/{sid}")).json()
    assert current["workspace"] == {"entry": False, "active": False}
    assert "expose_workspace_tools" not in current["tools"]
    context = await _context(env.client, sid)
    assert "expose_workspace_tools" not in [t["name"] for t in context["tools"]]
    assert "`expose_workspace_tools`" not in context["instructions"]

    activated = (await env.client.post(f"/api/session/{sid}/workspace-tools", json={})).json()
    assert activated["workspace"] == {"entry": False, "active": True}
    assert {"change_session_directory", "worktree"} <= set(activated["tools"])
    assert "expose_workspace_tools" not in activated["tools"]  # explicit activation, not a silent entry point
    await env.rt.shutdown()


async def test_older_sessions_keep_their_snapshot_until_explicit_activation(env):
    session = await _create(env.client, env.workspace)
    sid = session["id"]
    path = env.store.path
    await env.rt.shutdown()
    with env.store.db:
        env.store.db.execute("delete from session_snapshot")
    env.store.close()

    store = Store(path)
    rt = make_runtime(remembering(store, "scripted/m"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    context = (await client.get(f"/api/session/{sid}/context")).json()
    names = [t["name"] for t in context["tools"]]
    assert names == TASK_TOOLS and "expose_workspace_tools" not in names
    assert "`change_session_directory`" not in context["instructions"]
    assert (await client.get(f"/api/session/{sid}")).json()["workspace"] == {"entry": False, "active": False}

    # An agent switch rebuilds the snapshot deliberately but must not add the entry point to an old session.
    switched = (await client.post(f"/api/session/{sid}/agent", json={"agent": "orchestrate"})).json()
    assert switched["workspace"] == {"entry": False, "active": False}
    assert "expose_workspace_tools" not in switched["tools"]

    activated = (await client.post(f"/api/session/{sid}/workspace-tools", json={})).json()
    assert activated["workspace"] == {"entry": False, "active": True}
    assert {"change_session_directory", "worktree"} <= set(activated["tools"])
    context = (await client.get(f"/api/session/{sid}/context")).json()
    assert {"change_session_directory", "worktree"} <= {t["name"] for t in context["tools"]}
    assert "`change_session_directory`" not in context["instructions"]  # the frozen prompt is untouched
    notice = (await client.get(f"/api/session/{sid}/message")).json()[-1]["parts"][0]["text"]
    assert "workspace control tools activated" in notice
    await rt.shutdown()
    store.close()
    await client.aclose()


async def test_activation_describes_only_enabled_tools(env):
    sid = (await _create(env.client, env.workspace, disabled_tools=["worktree"]))["id"]
    text = env.rt.activate_workspace_tools(sid, source="api")
    assert "### `change_session_directory`" in text
    assert "### `worktree`" not in text
    assert "Switched off and unavailable: worktree" in text
    assert "worktree" not in env.rt.activate_workspace_tools(sid)
    await env.rt.shutdown()


async def test_worktree_delete_protects_subdirectories_and_background_jobs(env):
    sid = (await _create(env.client, env.workspace))["id"]
    worker = (await _create(env.client, env.workspace))["id"]
    target = env.tmp_path / "used-wt"
    await env.rt.create_worktree(sid, str(target), "used")
    sub = target / "sub"
    sub.mkdir()
    await env.rt.change_directory(worker, str(sub), source="api")
    with pytest.raises(ToolError, match="directory of session"):
        await env.rt.delete_worktree(sid, str(target))
    await env.rt.change_directory(worker, str(env.workspace), source="api")
    job = await env.rt.jobs.start(session_id=worker, root=worker, command="sleep 30",
                                  directory=target, timeout=None, on_finish=lambda _: None)
    with pytest.raises(ToolError, match="running background job"):
        await env.rt.delete_worktree(sid, str(target))
    await env.rt.jobs.kill(job.id)
    await env.rt.delete_worktree(sid, str(target))
    assert not target.exists()
    await env.rt.shutdown()


async def test_delete_reservation_blocks_new_directory_users(env, monkeypatch):
    sid = (await _create(env.client, env.workspace))["id"]
    target = env.tmp_path / "deleting-wt"
    await env.rt.create_worktree(sid, str(target), "deleting")
    entered, release = asyncio.Event(), asyncio.Event()
    remove = worktrees_module.remove

    async def blocked_remove(*args):
        entered.set()
        await release.wait()
        return await remove(*args)

    monkeypatch.setattr(worktrees_module, "remove", blocked_remove)
    task = asyncio.create_task(env.rt.delete_worktree(sid, str(target)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        response = await env.client.post(f"/api/session/{sid}/directory", json={"directory": str(target)})
        assert response.status_code == 400 and "being deleted" in response.json()["error"]
        with pytest.raises(ValueError, match="being deleted"):
            env.rt.open_project(str(target))
    finally:
        release.set()
        await task
    assert not env.rt.removing_worktrees
    await env.rt.shutdown()


async def test_pending_move_loads_instructions_when_applied(env):
    sid = (await _create(env.client, env.workspace))["id"]
    target = env.tmp_path / "pending-target"
    target.mkdir()
    instructions = target / "AGENTS.md"
    instructions.write_text("Queued rules.")
    gate = asyncio.Event()
    env.provider.gates = [gate]
    env.provider.replies = [[Text("done")]]
    env.rt.prompt(sid, "work")
    while not env.provider.requests:
        await asyncio.sleep(0)
    await env.rt.change_directory(sid, str(target), source="api")
    instructions.write_text("Applied rules.")
    gate.set()
    await _idle(env.rt, sid)
    messages = await _messages_of(env.client, sid)
    notice = next(text for text in _texts(messages) if text and "session directory changed" in text)
    assert "Applied rules." in notice and "Queued rules." not in notice
    await env.rt.shutdown()


async def test_interrupted_tool_move_gets_forward_notice_without_saved_result(env):
    sid = (await _create(env.client, env.workspace))["id"]
    target = env.tmp_path / "interrupted-target"
    target.mkdir()
    env.provider.gates = [asyncio.Event()]
    env.provider.replies = [[Text("unused")]]
    env.rt.prompt(sid, "work")
    while not env.provider.requests:
        await asyncio.sleep(0)
    await env.rt.change_directory(sid, str(target), source="tool")
    await env.rt.interrupt(sid)
    assert env.rt.session(sid).directory == str(target.resolve())
    assert any(text and "session directory changed" in text
               for text in _texts(await _messages_of(env.client, sid)))
    await env.rt.shutdown()


async def test_git_metadata_preserves_unusual_worktree_paths(env):
    sid = (await _create(env.client, env.workspace))["id"]
    target = env.tmp_path / 'quoted "€\npath'
    await env.rt.create_worktree(sid, str(target), "unusual")
    listing = await env.rt.list_worktrees(sid)
    assert any(item["path"] == str(target.resolve()) for item in listing["worktrees"])
    state = await worktrees_module.git_state(target)
    assert state["worktree"] == str(target.resolve())
    await env.rt.delete_worktree(sid, str(target))
    await env.rt.shutdown()


@pytest.mark.parametrize("helper", ["_git", "_git_ok"])
async def test_cancelled_git_stops_its_process_group(tmp_path, monkeypatch, helper):
    real_spawn = asyncio.create_subprocess_exec
    entered = asyncio.Event()
    processes = []
    marker = tmp_path / "late-write"

    async def spawn(*args, **kwargs):
        proc = await real_spawn("/bin/bash", "-c", 'sleep 0.2; touch "$1"', "_", str(marker), **kwargs)
        processes.append(proc)
        entered.set()
        return proc

    monkeypatch.setattr(worktrees_module.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(getattr(worktrees_module, helper)("status", cwd=tmp_path))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None
    await asyncio.sleep(0.3)
    assert not marker.exists()


async def test_tool_move_cannot_replace_an_already_pending_api_move(env):
    from nexus.runtime import Conflict

    sid = (await _create(env.client, env.workspace))["id"]
    first, second = env.tmp_path / "first-target", env.tmp_path / "second-target"
    first.mkdir()
    second.mkdir()
    env.provider.gates = [asyncio.Event()]
    env.provider.replies = [[Text("unused")]]
    env.rt.prompt(sid, "work")
    while not env.provider.requests:
        await asyncio.sleep(0)
    await env.rt.change_directory(sid, str(first), source="api")
    with pytest.raises(Conflict, match="already pending"):
        await env.rt.change_directory(sid, str(second), source="tool")
    assert env.rt.pending_directories[sid]["to"] == str(first.resolve())
    await env.rt.shutdown()
