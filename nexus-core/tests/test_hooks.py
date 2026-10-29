"""Command hooks (PLAN.md H): parsing, the runner's limits, and the loop's use of decisions."""
from __future__ import annotations

import json
import sys

import pytest

from nexus.hooks import HookRunner, parse_hooks
from nexus.llmhub.types import Done, PartDone, Text, ToolCall, ToolResult, ToolSpec
from nexus.loop import Agent, HookFired, ToolEnd, TurnEnd
from nexus.tools import Tool


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def runner(tmp_path, event, entries) -> HookRunner:
    hooks, problems = parse_hooks({event: entries})
    assert not problems, problems
    return HookRunner(hooks, tmp_path, session_id="s1")


def test_parse_reports_bad_entries_and_keeps_good_ones():
    hooks, problems = parse_hooks({
        "PreToolUse": [{"name": "ok", "command": ["true"]}, {"command": "not a list"}, {"command": ["x"], "timeout_s": 9999}],
        "Nope": [],
        "TurnEnd": [{"command": ["true"], "matcher": "bash"}],
    })
    assert [h.name for h in hooks] == ["ok"]
    assert len(problems) == 4


@pytest.mark.asyncio
async def test_allow_block_and_payload(tmp_path):
    echo = "import json,sys;d=json.load(sys.stdin);print(json.dumps({'decision':'block','message':d['tool_input']['command']}))"
    run = runner(tmp_path, "PreToolUse", [{"name": "b", "matcher": "bash(rm *)", "command": py(echo)}])
    assert await run.fire("PreToolUse", tool="bash", args={"command": "ls"}) == []
    [out] = await run.fire("PreToolUse", tool="bash", args={"command": "rm -rf x"})
    assert (out.decision, out.message) == ("block", "rm -rf x")


@pytest.mark.asyncio
async def test_nonzero_timeout_and_big_output_follow_on_nonzero(tmp_path):
    for mode, expected in (("block", "block"), ("warn", "warn"), ("ignore", "ignored")):
        run = runner(tmp_path, "TurnEnd", [{"command": py("import sys;sys.exit(3)"), "on_nonzero": mode}])
        assert (await run.fire("TurnEnd"))[0].decision == expected
    run = runner(tmp_path, "TurnEnd", [{"command": py("import time;time.sleep(30)"), "timeout_s": 0.3, "on_nonzero": "block"}])
    [out] = await run.fire("TurnEnd")
    assert out.decision == "block" and "timed out" in out.error
    run = runner(tmp_path, "TurnEnd", [{"command": py("print('x'*200000)"), "on_nonzero": "block"}])
    assert (await run.fire("TurnEnd"))[0].decision == "block"


@pytest.mark.asyncio
async def test_environment_is_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    code = "import os,json;print(json.dumps({'context':str('SECRET_TOKEN' in os.environ)+os.environ['NEXUS_HOOK_EVENT']}))"
    [out] = await runner(tmp_path, "TurnEnd", [{"command": py(code)}]).fire("TurnEnd")
    assert out.context == "FalseTurnEnd"


class Script:
    """A provider that calls `bash` once, then answers."""

    def __init__(self) -> None:
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            yield PartDone(ToolCall("c1", "bash", json.dumps({"command": "rm -rf x"})))
        else:
            yield PartDone(Text("done"))
        yield Done("stop")


async def drive(tmp_path, entries, event="PreToolUse"):
    ran = []

    async def bash(args, ctx):
        ran.append(args)
        return "ran"

    provider = Script()
    agent = Agent(provider, model="m", workspace=tmp_path,
                  tools=(Tool(ToolSpec("bash", "d", {"type": "object"}), bash),),
                  hooks=runner(tmp_path, event, entries))
    events = [e async for e in agent.turn("go")]
    return events, ran, provider, agent


@pytest.mark.asyncio
async def test_pre_tool_block_gives_the_model_an_error_result(tmp_path):
    code = "import json;print(json.dumps({'decision':'block','message':'no rm'}))"
    events, ran, _, _ = await drive(tmp_path, [{"name": "guard", "matcher": "bash(rm *)", "command": py(code)}])
    assert ran == []
    [end] = [e for e in events if isinstance(e, ToolEnd)]
    assert end.result == ToolResult("c1", "blocked by hook guard: no rm", is_error=True)
    assert any(isinstance(e, HookFired) and e.decision == "block" for e in events)


@pytest.mark.asyncio
async def test_modify_changes_the_input_and_is_announced(tmp_path):
    code = "import json;print(json.dumps({'decision':'modify','tool_input':{'command':'ls'}}))"
    _events, ran, provider, _ = await drive(tmp_path, [{"name": "fix", "command": py(code)}])
    assert ran == [{"command": "ls"}]
    second = provider.requests[1].messages
    text = " ".join(p.text for m in second if m.role == "user" for p in m.parts if isinstance(p, Text))
    assert "[hook fix] changed the input" in text and '"ls"' in text


@pytest.mark.asyncio
async def test_allow_only_is_not_context(tmp_path):
    events, ran, _, agent = await drive(tmp_path, [{"command": py("pass")}])
    assert ran == [{"command": "rm -rf x"}]
    assert [m.role for m in agent.messages] == ["user", "assistant", "tool", "assistant"]
    assert any(isinstance(e, HookFired) and e.decision == "allow" for e in events)


@pytest.mark.asyncio
async def test_prompt_block_ends_the_turn_without_a_model_call(tmp_path):
    code = "import json;print(json.dumps({'decision':'block','message':'not today'}))"
    events, _, provider, agent = await drive(tmp_path, [{"name": "gate", "command": py(code)}], "UserPromptSubmit")
    assert provider.requests == []
    assert next(e for e in events if isinstance(e, TurnEnd)).stop == "blocked"
    assert "not today" in agent.messages[-1].parts[0].text
