"""Agent hot reload: global files reach new sessions only, changes are detectable, bad files are listed."""
import asyncio
import os

import httpx
import pytest
from conftest import remembering

from nexus import agents
from nexus import runtime as runtime_module
from nexus.server import build_app, make_runtime
from nexus.store import Store


class Stub:
    name = "stub"

    def body(self, req):
        return {"instructions": req.instructions, "tools": [{"name": t.name} for t in req.tools]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (Stub(), ref.partition("/")[2]))
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "scripted/m"))
    app = build_app(rt, token="t", url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": "Bearer t"})
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return rt, client, tmp_path / "home" / "agents", workspace


def _write(directory, name, body, extra=""):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(f"---\ndescription: d\n{extra}---\n{body}\n")


async def test_global_agent_edit_reaches_new_sessions_only(env):
    rt, client, glob, workspace = env
    _write(glob, "build", "Global one.\n\n{{tools}}")
    first = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    _write(glob, "build", "Global two.\n\n{{tools}}")
    second = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    ctx1 = (await client.get(f"/api/session/{first['id']}/context")).json()["instructions"]
    ctx2 = (await client.get(f"/api/session/{second['id']}/context")).json()["instructions"]
    assert "Global one." in ctx1 and "Global two." not in ctx1
    assert "Global two." in ctx2
    await rt.shutdown()


def test_fingerprint_sees_adds_edits_and_removals(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    project = tmp_path / "p"
    folder = project / ".agents" / "agents"
    seen = [agents.fingerprint([str(project)])]
    _write(folder, "x", "Body")
    seen.append(agents.fingerprint([str(project)]))
    _write(folder, "x", "Longer body")
    seen.append(agents.fingerprint([str(project)]))
    (folder / "x.md").unlink()
    seen.append(agents.fingerprint([str(project)]))
    assert len(set(seen[:3])) == 3 and seen[3] == seen[0]
    assert agents.fingerprint([]) == agents.fingerprint([str(tmp_path / "missing")])


async def test_color_mode_and_problems_are_served(env):
    rt, client, glob, workspace = env
    _write(glob, "pretty", "Body", extra="mode: subagent\ncolor: '#aabbcc'\n")
    _write(glob, "broken", "Body", extra="color: 12\n")
    found = (await client.get("/api/agent", params={"directory": str(workspace)})).json()
    pretty = next(a for a in found["agents"] if a["name"] == "pretty")
    assert pretty["color"] == "#aabbcc" and pretty["mode"] == "subagent"
    assert [p["path"] for p in found["problems"]] == [str(glob / "broken.md")]
    assert "color" in found["problems"][0]["error"]
    assert "broken" not in {a["name"] for a in found["agents"]}
    await rt.shutdown()


async def test_agents_changed_is_published(env, monkeypatch):
    rt, client, _glob, workspace = env
    real_sleep = asyncio.sleep

    async def fast(delay, *a, **k):
        await real_sleep(0.01)

    monkeypatch.setattr("nexus.server.asyncio.sleep", fast)
    await client.post("/api/project", json={"path": str(workspace)})
    events = []
    original = rt.bus.publish
    rt.bus.publish = lambda t, d=None, *a, **k: (events.append(t), original(t, d, *a, **k))[1]
    app = client._transport.app
    async with app.router.lifespan_context(app):
        await real_sleep(0.05)
        _write(workspace / ".agents" / "agents", "new", "Body")
        os.utime(workspace / ".agents" / "agents" / "new.md", None)
        for _ in range(100):
            if "agents.changed" in events:
                break
            await real_sleep(0.02)
    assert "agents.changed" in events
