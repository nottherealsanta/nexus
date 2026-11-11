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
