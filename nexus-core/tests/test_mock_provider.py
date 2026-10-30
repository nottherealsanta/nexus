from __future__ import annotations

import asyncio

from nexus.llmhub.types import Done, PartDone, Request, TextDelta, Usage, WireEvent, WireRequest, WireResponse
from nexus.mock.provider import MockProvider


def test_hello_stream_has_wire_and_normalized_events():
    async def run():
        events = [e async for e in MockProvider("hello").stream(Request("hello", "", ()))]
        assert isinstance(events[0], WireRequest)
        assert isinstance(events[1], WireResponse)
        assert any(isinstance(e, WireEvent) for e in events)
        assert any(isinstance(e, TextDelta) for e in events)
        assert any(isinstance(e, PartDone) for e in events)
        assert any(isinstance(e, Usage) for e in events)
        assert isinstance(events[-1], Done)
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert "No model was called" in text

    asyncio.run(run())


def test_tool_marathon_emits_real_tool_calls():
    async def run():
        events = [e async for e in MockProvider("tool_marathon").stream(Request("tool_marathon", "", ()))]
        from nexus.llmhub.types import ToolCallStarted

        calls = [e for e in events if isinstance(e, ToolCallStarted)]
        assert len(calls) == 1
        assert calls[0].name == "write"

    asyncio.run(run())


def test_mock_registration_is_dev_gated():
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env["NEXUS10_DEV"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", "from nexus.llmhub.hub import provider_for; p,m=provider_for('mock/hello'); print(p.name,m)"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "mock hello"


def test_tool_marathon_runs_through_runtime(tmp_path):
    import os
    import subprocess
    import sys

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = """
import asyncio, os
WORKSPACE=os.environ["WORKSPACE"]
from nexus.bus import Bus
from nexus.runtime import Runtime
from nexus.store import Store
async def main():
    store=Store()
    runtime=Runtime(store,Bus(),"mock/tool_marathon")
    project=runtime.open_project(WORKSPACE)
    session=runtime.create_session(project,title=None,model="mock/tool_marathon",permission="allow")
    runtime.prompt(session.id,"Run the tool marathon.")
    await runtime.turns[session.id]
    messages=store.messages(session.id)
    assert (WORKSPACE + "/mock-output.md")
    assert any("mock verdict — tool-marathon: 5/5 checks passed" in "".join(p.text for p in item.message.parts if hasattr(p,"text")) for item in messages if item.message.role=="assistant")
    with open(WORKSPACE+"/mock-output.md") as f: assert f.read()=="hello from mock\\n"
    await runtime.shutdown()
asyncio.run(main())
"""
    env = os.environ.copy()
    env.update(NEXUS10_DEV="1", NEXUS10_HOME=str(home), WORKSPACE=str(workspace))
    result = subprocess.run([sys.executable, "-c", script], check=False, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_parallel_subagents_runs_child_sessions(tmp_path):
    import os
    import subprocess
    import sys

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = """
import asyncio, os
from nexus.bus import Bus
from nexus.runtime import Runtime
from nexus.store import Store
async def main():
    store=Store()
    runtime=Runtime(store,Bus(),"mock/parallel_subagents")
    project=runtime.open_project(os.environ["WORKSPACE"])
    session=runtime.create_session(project,title=None,model="mock/parallel_subagents",permission="allow",agent="orchestrate")
    runtime.prompt(session.id,"Exercise parallel sub agents.")
    await runtime.turns[session.id]
    messages=store.messages(session.id)
    assert any("mock verdict — parallel-subagents: 2/2 checks passed" in "".join(p.text for p in item.message.parts if hasattr(p,"text")) for item in messages if item.message.role=="assistant")
    assert len(store.children(session.id)) == 2
    await runtime.shutdown()
asyncio.run(main())
"""
    env = os.environ.copy()
    env.update(NEXUS10_DEV="1", NEXUS10_HOME=str(home), WORKSPACE=str(workspace))
    result = subprocess.run([sys.executable, "-c", script], check=False, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
