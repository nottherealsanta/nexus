"""User tools: discovery without execution, quarantine, snapshot freezing and reload."""
import asyncio
import json
import os
import time

import httpx
import pytest
from conftest import remembering

from nexus import extensions
from nexus import runtime as runtime_module
from nexus.llmhub.types import Done, PartDone, Text, ToolCall
from nexus.server import build_app, make_runtime
from nexus.store import Store

HELLO = '''
NAME = "hello"
DESCRIPTION = "Greet someone."
PARAMETERS = {"type": "object", "properties": {"who": {"type": "string"}}, "required": ["who"]}
GUIDANCE = "Use to say hello."

def run(args):
    return "Hello, " + args["who"] + "! (v%s)"
'''


def hello(version="1"):
    return HELLO % version


class Scripted:
    name = "scripted"

    def __init__(self):
        self.replies = []
        self.requests = []

    def body(self, req):
        return {"instructions": req.instructions, "tools": [{"name": t.name} for t in req.tools]}

    async def stream(self, req):
        self.requests.append(req)
        parts = self.replies.pop(0)
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "scripted/m"))
    app = build_app(rt, token="t", url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": "Bearer t"})
    workspace = tmp_path / "proj"
    workspace.mkdir()
    tools = tmp_path / "home" / "tools"
    tools.mkdir(parents=True)
    return rt, client, provider, workspace, tools


async def _idle(rt):
    for _ in range(500):
        if not rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("still running")


def test_discovery_reads_metadata_without_running_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    marker = tmp_path / "ran"
    tools = tmp_path / "home" / "tools"
    tools.mkdir(parents=True)
    (tools / "side.py").write_text(
        f'open({str(marker)!r}, "w").write("x")\nDESCRIPTION = "d"\ndef run(args):\n    return "ok"\n')
    found = extensions.discover(None)
    assert "side" in found.tools and not marker.exists()


def test_bad_files_are_problems_and_names_cannot_shadow_builtins(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    tools = tmp_path / "home" / "tools"
    tools.mkdir(parents=True)
    (tools / "norun.py").write_text('DESCRIPTION = "d"\n')
    (tools / "syntax.py").write_text("def run(:\n")
    (tools / "dyn.py").write_text('DESCRIPTION = "a" + str(1)\ndef run(a):\n    return ""\n')
    (tools / "bash.py").write_text('DESCRIPTION = "d"\ndef run(a):\n    return ""\n')
    (tools / "good.py").write_text('DESCRIPTION = "d"\ndef run(a):\n    return ""\n')
    found = extensions.discover(None, {"bash"})
    assert list(found.tools) == ["good"]
    errors = {p["path"].rsplit("/", 1)[1]: p["error"] for p in found.problems}
    assert set(errors) == {"norun.py", "syntax.py", "dyn.py", "bash.py"}
    assert "run" in errors["norun.py"] and "literal" in errors["dyn.py"] and "built-in" in errors["bash.py"]


def test_project_tool_replaces_global_one(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "tools").mkdir(parents=True)
    (tmp_path / "home" / "tools" / "t.py").write_text('DESCRIPTION = "global"\ndef run(a):\n    return ""\n')
    project = tmp_path / "p"
    (project / ".agents" / "tools").mkdir(parents=True)
    (project / ".agents" / "tools" / "t.py").write_text('DESCRIPTION = "project"\ndef run(a):\n    return ""\n')
    found = extensions.discover(str(project))
    assert found.tools["t"].description == "project" and found.tools["t"].scope == "project"


async def _record(tmp_path, source, **meta):
    path = tmp_path / "t.py"
    path.write_text(source)
    return extensions.parse(path, "global").record() | {"name": "t"}


async def test_call_runs_in_a_child_process_with_a_scrubbed_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    record = await _record(tmp_path, (
        'import os\nDESCRIPTION = "d"\n'
        "async def run(args):\n"
        '    return f"{os.getpid()}|{os.environ.get(\'SECRET_TOKEN\')}|{os.getcwd()}|{args[\'n\']}"\n'))
    pid, secret, cwd, n = (await extensions.call(record, {"n": 3}, tmp_path)).split("|")
    assert int(pid) != os.getpid() and secret == "None" and n == "3"
    assert os.path.realpath(cwd) == os.path.realpath(tmp_path)


async def test_call_failures_timeouts_and_stray_prints(tmp_path):
    boom = await _record(tmp_path, 'DESCRIPTION = "d"\ndef run(a):\n    raise KeyError("x")\n')
    with pytest.raises(extensions.ToolError, match="KeyError"):
        await extensions.call(boom, {}, tmp_path)
    chatty = await _record(tmp_path, 'DESCRIPTION = "d"\ndef run(a):\n    print("noise")\n    return "fine"\n')
    assert await extensions.call(chatty, {}, tmp_path) == "fine"
    slow = await _record(tmp_path, 'import time\nDESCRIPTION = "d"\nTIMEOUT = 0.5\ndef run(a):\n    time.sleep(30)\n')
    started = time.monotonic()
    with pytest.raises(extensions.ToolError, match="timed out"):
        await extensions.call(slow, {}, tmp_path)
    assert time.monotonic() - started < 10
    dead = await _record(tmp_path, 'import os\nDESCRIPTION = "d"\ndef run(a):\n    os._exit(3)\n')
    with pytest.raises(extensions.ToolError, match="code 3"):
        await extensions.call(dead, {}, tmp_path)


async def test_new_session_gets_the_tool_and_a_running_one_keeps_its_code(env):
    rt, client, provider, workspace, tools = env
    (tools / "hello.py").write_text(hello("1"))
    first = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    listed = (await client.get("/api/extension", params={"directory": str(workspace)})).json()
    assert [t["name"] for t in listed["tools"]] == ["hello"] and listed["problems"] == []
    context = (await client.get(f"/api/session/{first['id']}/context")).json()
    assert "hello" in [t["name"] for t in context["tools"]] and "Use to say hello." in rt.store.snapshot(first["id"])["tool_definitions"]["hello"]["description"]

    (tools / "hello.py").write_text(hello("2"))
    second = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    for sid, version in ((first["id"], "(v1)"), (second["id"], "(v2)")):
        provider.replies = [[ToolCall("c1", "hello", json.dumps({"who": "Ada"}))], [Text("done")]]
        await client.post(f"/api/session/{sid}/prompt", json={"text": "greet"})
        await _idle(rt)
        messages = (await client.get(f"/api/session/{sid}/message")).json()
        result = next(m for m in messages if m["role"] == "tool")
        text = str(result)
        assert "Hello, Ada!" in text and version in text, text
        if sid == first["id"]:
            assert "changed on disk" in text
        else:
            assert "changed on disk" not in text
    await rt.shutdown()


async def test_tool_can_be_switched_off_per_session(env):
    rt, client, _, workspace, tools = env
    (tools / "hello.py").write_text(hello())
    made = (await client.post("/api/session", json={"directory": str(workspace), "disabled_tools": ["hello"]})).json()
    context = (await client.get(f"/api/session/{made['id']}/context")).json()
    assert "hello" not in [t["name"] for t in context["tools"]]
    await rt.shutdown()


def test_reload_stress_does_not_accumulate(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    tools = tmp_path / "home" / "tools"
    tools.mkdir(parents=True)
    seen = set()
    for i in range(200):
        (tools / "t.py").write_text(f'DESCRIPTION = "v{i}"\ndef run(a):\n    return "{i}"\n')
        found = extensions.discover(None)
        assert list(found.tools) == ["t"] and found.tools["t"].description == f"v{i}"
        seen.add(extensions.fingerprint([]))
    assert len(seen) > 1
