"""Session-tree archiving: persistence, lifecycle rules, filters and notifications.

Everything runs offline with scripted providers and temporary homes/workspaces.
"""
import argparse
import asyncio
import json
import subprocess

import httpx
import pytest
from conftest import remembering

from nexus import runtime as runtime_module
from nexus.cli import _open_session
from nexus.client import Client
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
        return {"model": req.model, "messages": len(req.messages), "tools": [t.name for t in req.tools],
                "instructions": req.instructions, "effort": req.reasoning_effort}

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
    def __init__(self, rt, client, provider, workspace, store, tmp_path, app):
        self.rt, self.client, self.provider, self.workspace = rt, client, provider, workspace
        self.store, self.tmp_path, self.app = store, tmp_path, app

    def __iter__(self):
        # Tests unpack the four common pieces; `env.app`/`env.store` remain available.
        return iter((self.rt, self.client, self.provider, self.workspace))


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
    (workspace / "a.txt").write_text("old file\n")
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    store = Store(tmp_path / "nexus.db")
    rt = make_runtime(remembering(store, "scripted/m"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    return Env(rt, client, provider, workspace, store, tmp_path, app)


async def _idle(rt, session_id=None):
    for _ in range(500):
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


async def _child(client, parent, **fields):
    response = await client.post("/api/session", json={"parent": parent, **fields})
    assert response.status_code == 200, response.text
    return response.json()


def _texts(messages):
    values = (p.get("text") or p.get("output") for m in messages for p in m["parts"])
    return [value for value in values if value is not None]


def test_migration_and_restart_persistence(tmp_path):
    path = tmp_path / "nexus.db"
    store = Store(path)
    project, _ = store.project_for_path(str(tmp_path))
    session = store.create_session(project, title="old session", agent="build")
    store.append_message(session.id, Message("user", (Text("hello"),)))
    store.close()

    # A database from before archiving opens with every session live.
    store = Store(path)
    assert store.session(session.id).archived == 0
    store.db.execute("alter table session drop column archived")
    store.close()

    store = Store(path)
    assert store.session(session.id).archived == 0
    assert [m.message.parts[0].text for m in store.messages(session.id)] == ["hello"]

    ids, changed = store.set_tree_archived(session.id, True)
    assert ids == [session.id] and changed
    store.close()

    store = Store(path)
    assert store.session(session.id).archived == 1
    assert store.sessions(project.id, archived="exclude") == []
    assert [s.id for s in store.sessions(project.id, archived="only")] == [session.id]
    assert [s.id for s in store.sessions(project.id, archived="include")] == [session.id]
    assert [m.message.parts[0].text for m in store.messages(session.id)] == ["hello"]
    store.close()


async def test_whole_tree_archive_preserves_messages_events_snapshots_and_observations(env):
    rt, client, provider, workspace = env
    root = await _create(client, workspace)
    child = await _child(client, root["id"])
    provider.replies = [[ToolCall("r1", "read", json.dumps({"path": "a.txt"}))], [Text("read it")]]
    await client.post(f"/api/session/{root['id']}/prompt", json={"text": "read"})
    await _idle(rt, root["id"])

    path = str((workspace / "a.txt").resolve())
    assert rt.store.file_observation(root["id"], path) is not None
    before_messages = (await client.get(f"/api/session/{root['id']}/message")).json()
    before_events = rt.store.events(root["id"], limit=5000)
    before_snapshot = rt.store.snapshot(root["id"])
    before_child_snapshot = rt.store.snapshot(child["id"])
    before_observation = rt.store.file_observation(root["id"], path)
    before_directories = {s["id"]: s["directory"] for s in (await client.get("/api/session")).json()}
    before_file = (workspace / "a.txt").read_bytes()

    response = await client.post(f"/api/session/{root['id']}/archive")
    assert response.status_code == 200, response.text
    root_after = (await client.get(f"/api/session/{root['id']}")).json()
    child_after = (await client.get(f"/api/session/{child['id']}")).json()
    assert root_after["archived"] is True and child_after["archived"] is True
    assert root_after["status"] == "idle" and child_after["status"] == "idle"
    assert child_after["parent_id"] == root_after["id"]
    assert root_after["directory"] == before_directories[root_after["id"]]
    assert child_after["directory"] == before_directories[child_after["id"]]

    assert (await client.get(f"/api/session/{root['id']}/message")).json() == before_messages
    after_events = rt.store.events(root["id"], limit=5000)
    assert after_events[:len(before_events)] == before_events
    assert [e["type"] for e in after_events[len(before_events):]] == ["session.archived"]
    assert rt.store.snapshot(root["id"]) == before_snapshot
    assert rt.store.snapshot(child["id"]) == before_child_snapshot
    assert rt.store.file_observation(root["id"], path) == before_observation
    assert (workspace / "a.txt").read_bytes() == before_file

    response = await client.post(f"/api/session/{root['id']}/unarchive")
    assert response.status_code == 200, response.text
    assert (await client.get(f"/api/session/{root['id']}")).json()["archived"] is False
    assert (await client.get(f"/api/session/{child['id']}")).json()["archived"] is False
    assert (await client.get(f"/api/session/{root['id']}/message")).json() == before_messages
    assert rt.store.snapshot(root["id"]) == before_snapshot
    assert rt.store.file_observation(root["id"], path) == before_observation


async def test_archiving_does_not_touch_worktrees_or_branches(env):
    _, client, _, workspace = env
    git(workspace, "init", "-q", "-b", "main")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "init")
    root = await _create(client, workspace)
    path = env.tmp_path / "wt-feature"
    created = (await client.post(f"/api/session/{root['id']}/worktrees",
                                 json={"action": "create", "path": str(path), "branch": "feature"})).json()
    assert created["path"] == str(path.resolve())
    before = git(workspace, "worktree", "list", "--porcelain")
    before_branch = git(workspace, "branch", "--list", "feature")

    assert (await client.post(f"/api/session/{root['id']}/archive")).status_code == 200
    assert git(workspace, "worktree", "list", "--porcelain") == before
    assert git(workspace, "branch", "--list", "feature") == before_branch
    assert path.is_dir()
    assert (await client.get(f"/api/session/{root['id']}")).json()["directory"] == str(workspace.resolve())
    assert (await client.post(f"/api/session/{root['id']}/unarchive")).status_code == 200


async def test_archived_session_still_protects_its_worktree(env):
    _, client, _, workspace = env
    git(workspace, "init", "-q", "-b", "main")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "init")
    root = await _create(client, workspace)
    path = env.tmp_path / "wt-kept"
    created = (await client.post(f"/api/session/{root['id']}/worktrees",
                                 json={"action": "create", "path": str(path), "branch": "kept"})).json()
    moved = await client.post(f"/api/session/{root['id']}/directory", json={"directory": created["path"]})
    assert moved.status_code == 200, moved.text
    assert (await client.post(f"/api/session/{root['id']}/archive")).status_code == 200

    other = await _create(client, workspace)
    response = await client.post(f"/api/session/{other['id']}/worktrees",
                                 json={"action": "delete", "path": created["path"]})
    assert response.status_code == 400
    assert root["id"] in response.json()["error"] and "directory of session" in response.json()["error"]
    assert path.is_dir()


async def test_child_only_archive_is_rejected_with_the_root(env):
    _, client, _, workspace = env
    root = await _create(client, workspace)
    child = await _child(client, root["id"])
    response = await client.post(f"/api/session/{child['id']}/archive")
    assert response.status_code == 400
    assert root["id"] in response.json()["error"] and "root" in response.json()["error"]
    assert (await client.get(f"/api/session/{root['id']}")).json()["archived"] is False
    assert (await client.get(f"/api/session/{child['id']}")).json()["archived"] is False


async def test_archive_and_unarchive_are_idempotent_and_publish_one_transition(env):
    rt, client, _, workspace = env
    root = await _create(client, workspace)
    child = await _child(client, root["id"])

    for _ in range(2):
        response = await client.post(f"/api/session/{root['id']}/archive")
        assert response.status_code == 200 and response.json()["archived"] is True
    archived = [e for e in rt.store.events(root["id"], limit=5000) if e["type"] == "session.archived"]
    assert len(archived) == 1
    assert archived[0]["data"] == {"root": root["id"], "sessions": [root["id"], child["id"]], "archived": True}

    for _ in range(2):
        response = await client.post(f"/api/session/{root['id']}/unarchive")
        assert response.status_code == 200 and response.json()["archived"] is False
    unarchived = [e for e in rt.store.events(root["id"], limit=5000) if e["type"] == "session.unarchived"]
    assert len(unarchived) == 1
    assert unarchived[0]["data"]["archived"] is False


async def test_busy_root_waiting_for_approval_refuses_archive(env):
    rt, client, provider, workspace = env
    root = await _create(client, workspace, permission="ask")
    provider.replies = [[ToolCall("b1", "bash", '{"command": "true"}')]]
    await client.post(f"/api/session/{root['id']}/prompt", json={"text": "run"})
    for _ in range(200):
        if rt.pending_approvals(root["id"]):
            break
        await asyncio.sleep(0.01)
    [pending] = rt.pending_approvals(root["id"])

    response = await client.post(f"/api/session/{root['id']}/archive")
    assert response.status_code == 409
    assert "interrupt" in response.json()["error"]
    assert rt.pending_approvals(root["id"])[0]["id"] == pending["id"]
    assert (await client.get(f"/api/session/{root['id']}")).json()["archived"] is False

    await client.post(f"/api/session/{root['id']}/interrupt")
    await _idle(rt, root["id"])
    assert (await client.post(f"/api/session/{root['id']}/archive")).status_code == 200


async def test_waiting_for_a_question_refuses_archive(env):
    rt, client, provider, workspace = env
    root = await _create(client, workspace)
    args = {"questions": [{"id": "q", "question": "Which one?", "allow_free_text": True}]}
    provider.replies = [[ToolCall("q1", "ask", json.dumps(args))]]
    await client.post(f"/api/session/{root['id']}/prompt", json={"text": "ask"})
    for _ in range(200):
        if rt.pending_questions(root["id"]):
            break
        await asyncio.sleep(0.01)
    [question] = rt.pending_questions(root["id"])

    response = await client.post(f"/api/session/{root['id']}/archive")
    assert response.status_code == 409
    assert rt.pending_questions(root["id"])[0]["id"] == question["id"]
    assert (await client.get(f"/api/session/{root['id']}")).json()["archived"] is False

    provider.replies = [[Text("done")]]
    replied = await client.post(f"/api/session/{root['id']}/question/{question['id']}",
                                json={"answers": [{"question_id": "q", "text": "the first"}]})
    assert replied.status_code == 204
    await _idle(rt, root["id"])
    assert (await client.post(f"/api/session/{root['id']}/archive")).status_code == 200


async def test_busy_descendant_refuses_archive(env):
    rt, client, provider, workspace = env
    root = await _create(client, workspace)
    child = await _child(client, root["id"])
    gate = asyncio.Event()
    provider.gates = [gate]
    provider.replies = [[Text("child done")]]
    await client.post(f"/api/session/{child['id']}/prompt", json={"text": "work"})
    for _ in range(200):
        if child["id"] in rt.turns:
            break
        await asyncio.sleep(0.01)
    assert root["id"] not in rt.turns and child["id"] in rt.turns

    response = await client.post(f"/api/session/{root['id']}/archive")
    assert response.status_code == 409
    assert child["id"] in response.json()["error"]
    assert (await client.get(f"/api/session/{root['id']}")).json()["archived"] is False

    gate.set()
    await _idle(rt)
    assert (await client.post(f"/api/session/{root['id']}/archive")).status_code == 200


async def test_list_filters_and_explicit_reads(env):
    _, client, _, workspace = env
    first = await _create(client, workspace, title="first")
    first_child = await _child(client, first["id"])
    second = await _create(client, workspace, title="second")
    assert (await client.post(f"/api/session/{first['id']}/archive")).status_code == 200

    project = (await client.post("/api/project", json={"path": str(workspace)})).json()
    default = (await client.get("/api/session", params={"project": project["id"]})).json()
    assert [s["id"] for s in default] == [second["id"]]
    only = (await client.get("/api/session", params={"project": project["id"], "archived": "only"})).json()
    assert {s["id"] for s in only} == {first["id"], first_child["id"]}
    inclusive = (await client.get("/api/session", params={"project": project["id"], "archived": "include"})).json()
    assert {s["id"] for s in inclusive} == {first["id"], first_child["id"], second["id"]}
    assert (await client.get("/api/session", params={"archived": "maybe"})).status_code == 400
    assert (await client.get("/api/session", params={"parent": first["id"]})).json() == []
    kids = (await client.get("/api/session", params={"parent": first["id"], "archived": "include"})).json()
    assert [s["id"] for s in kids] == [first_child["id"]]
    assert [s["id"] for s in (await client.get(f"/api/session/{first['id']}/children")).json()] == [first_child["id"]]

    assert (await client.get(f"/api/session/{first['id']}")).status_code == 200
    tree = (await client.get(f"/api/session/{first_child['id']}/tree")).json()
    assert tree["id"] == first["id"] and [c["id"] for c in tree["children"]] == [first_child["id"]]
    assert (await client.get(f"/api/session/{first['id']}/message")).status_code == 200
    assert (await client.get(f"/api/session/{first['id']}/event")).status_code == 200
    assert (await client.get(f"/api/session/{first['id']}/snapshot")).status_code == 200


async def test_prompt_and_child_creation_require_unarchive(env):
    rt, client, provider, workspace = env
    root = await _create(client, workspace)
    child = await _child(client, root["id"])
    assert (await client.post(f"/api/session/{root['id']}/archive")).status_code == 200
    requests_before = len(provider.requests)

    prompt = await client.post(f"/api/session/{root['id']}/prompt", json={"text": "hello"})
    assert prompt.status_code == 409 and "unarchive" in prompt.json()["error"]
    child_prompt = await client.post(f"/api/session/{child['id']}/prompt", json={"text": "hello"})
    assert child_prompt.status_code == 409 and "unarchive" in child_prompt.json()["error"]
    for parent in (root["id"], child["id"]):
        made = await client.post("/api/session", json={"parent": parent, "agent": "build"})
        assert made.status_code == 409 and "unarchive" in made.json()["error"]
    assert len(provider.requests) == requests_before and not rt.turns

    assert (await client.post(f"/api/session/{root['id']}/unarchive")).status_code == 200
    assert not rt.turns and len(provider.requests) == requests_before
    made = await client.post("/api/session", json={"parent": root["id"], "agent": "build"})
    assert made.status_code == 200
    provider.replies = [[Text("ok")]]
    assert (await client.post(f"/api/session/{root['id']}/prompt", json={"text": "hello"})).status_code == 202
    await _idle(rt, root["id"])


async def test_background_completion_retains_notice_without_waking(env):
    rt, client, provider, workspace = env
    root = await _create(client, workspace, permission="allow")
    provider.replies = [
        [ToolCall("b1", "bash", json.dumps({"command": "sleep 1.2; printf late", "background": True}))],
        [Text("started")],
    ]
    await client.post(f"/api/session/{root['id']}/prompt", json={"text": "start"})
    await _idle(rt, root["id"])
    assert (await client.post(f"/api/session/{root['id']}/archive")).status_code == 200
    requests_before = len(provider.requests)

    for _ in range(400):
        if any(e["type"] == "bash.finished" for e in rt.store.events(root["id"], limit=5000)):
            break
        await asyncio.sleep(0.01)
    assert any(e["type"] == "bash.finished" for e in rt.store.events(root["id"], limit=5000))
    await asyncio.sleep(0.05)
    assert root["id"] not in rt.turns and len(provider.requests) == requests_before
    assert (await client.get(f"/api/session/{root['id']}")).json()["archived"] is True

    messages = (await client.get(f"/api/session/{root['id']}/message")).json()
    notice = next(text for text in _texts(messages) if "→ Background Shell Done:" in text)
    assert "late" in notice and "bash_output" in notice

    assert (await client.post(f"/api/session/{root['id']}/unarchive")).status_code == 200
    assert not rt.turns and len(provider.requests) == requests_before
    provider.replies = [[Text("resumed")]]
    await client.post(f"/api/session/{root['id']}/prompt", json={"text": "continue"})
    await _idle(rt, root["id"])
    assert any("→ Background Shell Done:" in p.text for m in provider.requests[-1].messages for p in m.parts if isinstance(p, Text))


async def test_api_authentication_and_error_responses(env):
    _, client, _, workspace = env
    root = await _create(client, workspace)
    child = await _child(client, root["id"])

    unauthenticated = await client.post(f"/api/session/{root['id']}/archive", headers={"authorization": ""})
    assert unauthenticated.status_code == 401
    assert (await client.post("/api/session/ses_missing/archive")).status_code == 404
    assert (await client.post("/api/session/ses_missing/unarchive")).status_code == 404
    assert (await client.post(f"/api/session/{child['id']}/archive")).status_code == 400
    assert (await client.get("/api/session", params={"archived": "bogus"})).status_code == 400


async def _cli_client(app):
    cli = Client("http://test", TOKEN)
    await cli.http.aclose()
    cli.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"authorization": f"Bearer {TOKEN}"})
    return cli


def _cli_args(workspace, **fields):
    base = {"session": None, "workspace": str(workspace), "cont": False, "model": None,
            "effort": None, "agent": None, "yes": False, "disable": None}
    base.update(fields)
    return argparse.Namespace(**base)


async def test_cli_continuation_skips_archived_and_explicit_id_is_actionable(env):
    _, client, _, workspace = env
    older = await _create(client, workspace, title="older")
    latest = await _create(client, workspace, title="latest")
    cli = await _cli_client(env.app)
    try:
        chosen = await _open_session(cli, _cli_args(workspace, cont=True))
        assert chosen["id"] == latest["id"]

        assert (await client.post(f"/api/session/{latest['id']}/archive")).status_code == 200
        chosen = await _open_session(cli, _cli_args(workspace, cont=True))
        assert chosen["id"] == older["id"]

        with pytest.raises(ValueError) as caught:
            await _open_session(cli, _cli_args(workspace, session=latest["id"]))
        assert "archived" in str(caught.value) and "unarchive" in str(caught.value)
    finally:
        await cli.close()


async def test_cached_parent_cannot_create_child_after_archiving(env):
    from nexus.runtime import Conflict

    rt, client, _, workspace = env
    root = await _create(client, workspace)
    cached = rt.session(root["id"])
    rt.archive_session(cached.id, archived=True)
    with pytest.raises(Conflict, match="unarchive"):
        rt.create_session(rt.project(cached.project_id), title=None, model=None,
                          permission=None, parent=cached, agent="build")
    assert not rt.store.children(cached.id)


async def test_deep_tree_busy_check_and_archived_descendant_notifications(env):
    rt, client, provider, workspace = env
    root = await _create(client, workspace)
    child = await _child(client, root["id"])
    grandchild = await _child(client, child["id"])
    gate = asyncio.Event()
    provider.gates = [gate]
    provider.replies = [[Text("done")]]
    rt.prompt(grandchild["id"], "work")
    refused = await client.post(f"/api/session/{root['id']}/archive")
    assert refused.status_code == 409 and grandchild["id"] in refused.json()["error"]
    assert not any(s.archived for s in rt.store.sessions())
    gate.set()
    await _idle(rt)
    assert (await client.post(f"/api/session/{root['id']}/archive")).status_code == 200
    assert all(s.archived for s in rt.store.sessions())
    count = len(provider.requests)
    rt._notify(grandchild["id"], "A retained descendant notice.")
    assert not rt.turns and len(provider.requests) == count
    messages = rt.store.messages(grandchild["id"])
    assert messages[-1].message.parts[0].text == "A retained descendant notice."
    assert (await client.post(f"/api/session/{root['id']}/unarchive")).status_code == 200
    assert not any(s.archived for s in rt.store.sessions())
    assert not rt.turns and len(provider.requests) == count
