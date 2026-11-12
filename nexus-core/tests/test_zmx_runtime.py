"""ZMX child sessions through the real runtime/store with a fake `agy`; no real CLI or model is used."""
import asyncio
import os
import sys

import pytest
from conftest import remembering

from nexus import runtime as runtime_module
from nexus.config import ConfigFile
from nexus.llmhub.hub import provider_for as real_provider_for
from nexus.llmhub.types import Done, PartDone, Text
from nexus.runtime import Conflict
from nexus.server import make_runtime
from nexus.store import Store

pytestmark = pytest.mark.skipif(os.name != "posix", reason="ZMX requires POSIX")
MODEL = "agy/gemini-3.8-flash-high"


class Normal:
    name = "normal"

    def body(self, req):
        return {"normal": True}

    async def stream(self, req):
        yield PartDone(Text("normal"))
        yield Done("end_turn")


FAKE = r'''#!%(python)s
import json, os, sys
if "--version" in sys.argv: print("1.4.12"); sys.exit()
if "--help" in sys.argv: print("--input-format --output-format stream-json --model --mode auto accept-edits plan --print-timeout --conversation --effort"); sys.exit()
def send(kind, data, **extra): print(json.dumps({"event": kind, kind: data, **extra}), flush=True)
prompt = json.loads(sys.stdin.readline())["message"]["content"]
open(os.path.join(os.path.dirname(__file__), "argv.json"), "a").write(json.dumps(sys.argv[1:]) + "\n")
send("init", {"cwd": os.getcwd(), "model": "gemini-3.8-flash-high", "permission_mode": "auto"}, conversation_id="conv-1")
send("step_update", {"step_index": 0, "step_type": "tool", "state": "RUNNING", "tool_name": "read_file", "tool_info": {"parameters": {"path": "a"}}})
send("step_update", {"step_index": 0, "step_type": "tool", "state": "DONE", "tool_name": "read_file", "tool_info": {"output": "contents"}})
send("step_update", {"step_index": 1, "step_type": "agent_response", "state": "DONE", "text_delta": "answer for " + prompt[-5:]})
send("result", {"status": "SUCCESS", "response": "answer for " + prompt[-5:], "usage": {"input_tokens": 7, "output_tokens": 3}}, conversation_id="conv-1")
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    cli = tmp_path / "agy"
    cli.write_text(FAKE % {"python": sys.executable})
    cli.chmod(0o700)
    monkeypatch.setenv("NEXUS10_AGY_CLI", str(cli))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    monkeypatch.setattr(runtime_module, "provider_for",
                        lambda ref: (Normal(), "m") if ref.startswith("fake/") else real_provider_for(ref))
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "fake/m"))
    return rt, workspace, cli


def enable():
    config = ConfigFile()
    config.patch(config.read()["revision"], {"zmx.antigravity": True}, [])


async def idle(rt):
    for _ in range(500):
        if not rt.turns:
            return
        await asyncio.sleep(.02)
    raise AssertionError("turn did not finish")


def test_off_by_default_and_roots_refused(env):
    rt, workspace, _ = env
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    with pytest.raises(ValueError, match="disabled"):
        rt.create_session(project, title=None, model=MODEL, permission=None, parent=root)
    enable()
    with pytest.raises(ValueError, match="subagent-only"):
        rt.create_session(project, title=None, model=MODEL, permission=None)
    with pytest.raises(Conflict, match="Cannot switch"):
        rt.update_session(root.id, model=MODEL)
    assert rt.session(root.id).model == "fake/m"


async def test_child_runs_stores_standard_records_and_resumes(env):
    rt, workspace, cli = env
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    rt.prompt(child.id, "first request")
    await idle(rt)
    messages = [m.message for m in rt.store.messages(child.id)]
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1].parts[0].name == "read_file" and messages[2].parts[0].output == "contents"
    assert messages[3].parts[0].text == "answer for quest"
    data = rt.session_json(rt.session(child.id))
    assert data["worker"]["session_id"] == "conv-1" and data["worker"]["context_complete"] is False
    assert data["worker"]["label"] == "ZMX · Antigravity" and "Nexus permission rules" in data["worker"]["not_applied"]
    assert data["tools"] == []
    # A client rebuilding from the state snapshot (start, reconnect) gets the same facts as `session.updated`.
    rows = {r["id"]: r for r in rt.store.state_snapshot()["sessions"]}
    assert rows[child.id]["worker"] == data["worker"] and rows[root.id]["worker"] is None
    usage = rt.store.session_usage(child.id)["models"]
    assert sum(m["input"] for m in usage.values()) == 7
    rt.prompt(child.id, "second request")
    await idle(rt)
    argv = [line for line in (cli.parent / "argv.json").read_text().splitlines()]
    assert "--conversation" not in argv[0] and "--conversation conv-1" in " ".join(argv[1].replace('"', "").replace(",", " ").split())


async def test_guards_change_nothing(env):
    rt, workspace, _ = env
    enable()
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=MODEL, permission="allow", parent=root)
    before = rt.session(child.id)
    for call in (lambda: rt.continue_session(child.id), lambda: rt.update_session(child.id, model="fake/m"),
                 lambda: rt.update_session(child.id, permission="ask")):
        with pytest.raises((Conflict, ValueError)):
            call()
    with pytest.raises(Conflict):
        await rt.btw(child.id, "side")
    with pytest.raises(Conflict):
        await rt.handoff(child.id)
    with pytest.raises(ValueError, match="text-only"):
        rt.prompt(child.id, "x", images=[{"name": "a.png", "data_url": "data:image/png;base64,AA=="}])
    assert rt.session(child.id).revision == before.revision and not rt.store.messages(child.id)
    ConfigFile().patch(ConfigFile().read()["revision"], {"zmx.antigravity": False}, [])
    with pytest.raises(ValueError, match="disabled"):
        rt.prompt(child.id, "off")
    assert rt.session_json(rt.session(child.id))["worker"]["label"]  # still readable


async def test_normal_children_are_unchanged(env):
    rt, workspace, _ = env
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model="fake/m", permission=None, parent=root)
    assert rt.session_json(rt.session(child.id))["worker"] is None
    rt.prompt(child.id, "ordinary")
    await idle(rt)
    assert [p.text for m in rt.store.messages(child.id) for p in m.message.parts if isinstance(p, Text)][-1] == "normal"


async def test_daemon_with_zmx_off_probes_no_cli_and_registers_no_provider(tmp_path, monkeypatch):
    """Starting the daemon and serving its provider, model and config routes with ZMX off never looks for a CLI,
    never spawns one and lists no ZMX provider or model."""
    import httpx

    from nexus.llmhub.hub import connected
    from nexus.server import build_app
    from nexus.workers import registry

    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NEXUS10_AGY_CLI", str(tmp_path / "must-not-run"))

    def forbidden(*_a, **_k):
        raise AssertionError("ZMX touched a CLI while switched off")

    monkeypatch.setattr(registry, "cli_path", forbidden)
    monkeypatch.setattr(registry, "probe", forbidden)
    real_exec = asyncio.create_subprocess_exec

    async def guarded_exec(program, *args, **kwargs):
        assert "must-not-run" not in str(program) and os.path.basename(str(program)) not in ("agy", "claude"), program
        return await real_exec(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded_exec)
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "mock/m"))
    app = build_app(rt, token="t", url="http://test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"authorization": "Bearer t"}) as client:
        providers = (await client.get("/api/provider")).json()
        models = (await client.get("/api/model")).json()["models"]
        config = (await client.get("/api/config")).json()
    assert not [p for p in providers if p.get("zmx") or p["id"] in registry.IDS]
    assert not [m for m in models if m.get("zmx") or m["ref"].partition("/")[0] in registry.IDS]
    assert config["values"]["zmx"] == {"claude_backend": "sdk", "antigravity": False}
    assert not set(connected()) & set(registry.IDS)
    assert registry.visible() == {}


def test_claude_pool_child_maps_to_claude_code_and_keeps_its_configured_effort(env, monkeypatch):
    """`[agents.models] build = ["claude/<id>"]` with `[agents.efforts.build]` on that ref: with the Claude backend set
    to ZMX the child runs `claude-code/<id>` at the configured effort, not the agent's or the provider's."""
    rt, workspace, _ = env
    monkeypatch.setattr(runtime_module, "connected", lambda: ["claude", "fake"])
    monkeypatch.setattr("nexus.workers.registry.cli_path", lambda pid: "/bin/true")
    config = ConfigFile()
    config.patch(config.read()["revision"], {"zmx.claude_backend": "zmx",
                                             "agents.models": {"build": ["claude/claude-sonnet-5-5"]},
                                             "agents.efforts": {"build": {"claude/claude-sonnet-5-5": "medium"}}}, [])
    project = rt.open_project(str(workspace))
    root = rt.create_session(project, title=None, model=None, permission=None)
    child = rt.create_session(project, title=None, model=None, permission="allow", agent="build", parent=root)
    data = rt.session_json(rt.session(child.id))
    assert data["model"] == "claude-code/claude-sonnet-5-5" and data["worker"]["label"] == "ZMX · Claude Code"
    assert (data["effort"], data["effort_source"]) == ("medium", "config")
