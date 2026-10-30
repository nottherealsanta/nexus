"""There is no default model: a new session starts with the user's last selection."""

import httpx
import pytest
from conftest import remembering

from nexus import runtime as runtime_module
from nexus.server import build_app, make_runtime
from nexus.store import Store


class Stub:
    name = "stub"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (Stub(), ref.partition("/")[2]))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    agents = workspace / ".agents" / "agents"
    agents.mkdir(parents=True)
    (agents / "free.md").write_text("---\ndescription: d\n---\nFree.\n")
    (agents / "pinned.md").write_text("---\ndescription: d\nmodel: scripted/pin\n---\nPinned.\n")

    def make(last=""):
        store = Store(tmp_path / "nexus.db")
        rt = make_runtime(remembering(store, last) if last else store)
        app = build_app(rt, token="t", url="http://test")
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                   headers={"authorization": "Bearer t"})
        return rt, client

    return make, workspace


async def test_without_a_selection_a_session_has_no_model_and_cannot_run(env):
    make, workspace = env
    _, client = make()
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    assert (session["model"], session["model_source"]) == ("", "none")
    assert (await client.get("/api/model/last")).json() == {"model": "", "source": "none"}
    reply = await client.post(f"/api/session/{session['id']}/prompt", json={"text": "hi"})
    assert reply.status_code == 400 and "no model selected" in reply.text


async def test_selection_is_remembered_globally_and_per_agent(env):
    make, workspace = env
    _, client = make()
    project = (await client.post("/api/project", json={"path": str(workspace)})).json()["id"]
    first = (await client.post("/api/session", json={"project": project, "model": "scripted/a"})).json()
    assert first["model"] == "scripted/a"
    plan = (await client.post("/api/session", json={"project": project, "agent": "orchestrate", "model": "scripted/b"})).json()
    assert plan["model"] == "scripted/b"
    # Another agent with no history of its own gets the last selection anywhere.
    scout = (await client.post("/api/session", json={"project": project, "agent": "free"})).json()
    assert (scout["model"], scout["model_source"]) == ("scripted/b", "session")
    # An agent used before gets what was last selected for it.
    build = (await client.post("/api/session", json={"project": project, "agent": "build"})).json()
    assert build["model"] == "scripted/a"
    got = (await client.get("/api/model/last", params={"project": project, "agent": "build"})).json()
    assert got == {"model": "scripted/a", "source": "agent"}
    assert (await client.get("/api/model/last", params={"project": project, "agent": "free"})).json() == {
        "model": "scripted/b", "source": "last"}
    # Another project has no per-agent history, only the global last selection.
    other = (await client.post("/api/project", json={"path": str(workspace.parent)})).json()["id"]
    assert (await client.get("/api/model/last", params={"project": other, "agent": "build"})).json()["source"] == "last"


async def test_model_switch_is_remembered_and_agent_switch_keeps_the_model(env):
    make, workspace = env
    _, client = make()
    session = (await client.post("/api/session", json={"directory": str(workspace), "model": "scripted/a"})).json()
    session = (await client.post(f"/api/session/{session['id']}/model", json={"model": "scripted/c"})).json()
    assert (await client.get("/api/model/last")).json()["model"] == "scripted/c"
    session = (await client.post(f"/api/session/{session['id']}/agent", json={"agent": "orchestrate"})).json()
    assert (session["agent"], session["model"]) == ("orchestrate", "scripted/c")
    session = (await client.post(f"/api/session/{session['id']}/agent", json={"agent": "pinned"})).json()
    assert (session["model"], session["model_source"]) == ("scripted/c", "session")


async def test_agent_pin_applies_until_the_session_leaves_it(env):
    make, workspace = env
    _, client = make("scripted/last")
    session = (await client.post("/api/session", json={"directory": str(workspace), "agent": "pinned"})).json()
    assert (session["model"], session["model_source"]) == ("scripted/pin", "agent")
    session = (await client.post(f"/api/session/{session['id']}/agent", json={"agent": "orchestrate"})).json()
    assert (session["model"], session["model_source"]) == ("scripted/pin", "session")
    # `""` goes back to the agent's own model, which only a pinning agent has.
    reply = await client.post(f"/api/session/{session['id']}/model", json={"model": ""})
    assert reply.status_code == 400


async def test_a_remembered_model_is_replaced_by_the_next_selection(env):
    make, workspace = env
    _, client = make("scripted/last")
    project = (await client.post("/api/project", json={"path": str(workspace)})).json()["id"]
    assert (await client.post("/api/session", json={"project": project})).json()["model"] == "scripted/last"
    await client.post("/api/session", json={"project": project, "model": "scripted/picked"})
    assert (await client.post("/api/session", json={"project": project})).json()["model"] == "scripted/picked"
    # The memory is on disk: a restarted daemon still has it.
    _, client2 = make()
    assert (await client2.get("/api/model/last")).json()["model"] == "scripted/picked"


async def test_subagent_inherits_the_parent_model(env):
    make, workspace = env
    rt, _ = make()
    project = rt.open_project(str(workspace))
    parent = rt.create_session(project, title=None, model="scripted/a", permission=None)
    child = rt.create_session(project, title=None, model=None, permission=None, agent="scout", parent=parent)
    assert not child.model
    assert rt.session_json(child)["model"] == "scripted/a" and rt.session_json(child)["model_source"] == "parent"
    rt.update_session(parent.id, model="scripted/b")
    assert rt.session_json(child)["model"] == "scripted/b"
    assert rt.remembered_model(project.id, "scout") == ("", "none")  # a subagent's model is never "selected"

