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


@pytest.mark.parametrize("decision", ["block", "modify", "allow"])
async def test_nested_tools_use_hooks_modified_approval_and_visible_notes(tmp_path, decision):
    from nexus.kernels import Kernels
    from nexus.tools.code import SPEC

    ran, approvals, published = [], [], []
    kernels = Kernels()

    async def fake_bash(args, ctx):
        ran.append(args)
        return "ran"

    tool = Tool(ToolSpec("bash", "fake shell", {"type": "object"}), fake_bash)

    async def allow(call, args):
        approvals.append((call.name, args))
        return True

    async def code(args, ctx):
        return await kernels.run(args, root="r", caller="s", ctx=ctx, tools=[tool], approve=allow,
                                 publish=lambda kind, data: published.append((kind, data)))

    class NestedScript(Script):
        async def stream(self, request):
            self.requests.append(request)
            yield PartDone(ToolCall("cell", "code", json.dumps({"code": "print(await bash(command='original'))"}))
                           if len(self.requests) == 1 else Text("done"))
            yield Done("stop")

    payload = {"decision": decision, "tool_input": {"command": "changed"}, "message": "guard"}
    entries = {"PreToolUse": [{"name": "guard", "matcher": "bash", "command": py(f"print({json.dumps(json.dumps(payload))})")}],
               "PostToolUse": [{"name": "after", "matcher": "bash", "command": py("print('{\"context\":\"post evidence\"}')")}]}
    hooks, problems = parse_hooks(entries)
    assert not problems
    provider = NestedScript()
    agent = Agent(provider, model="m", workspace=tmp_path, tools=(tool, Tool(SPEC, code)), approve=allow,
                  hooks=HookRunner(hooks, tmp_path, session_id="s"))
    try:
        events = [e async for e in agent.turn("go")]
        assert ran == ([] if decision == "block" else [{"command": "changed" if decision == "modify" else "original"}])
        assert any(isinstance(e, HookFired) and e.hook == "guard" for e in events)
        text = " ".join(p.text for m in provider.requests[-1].messages for p in m.parts if isinstance(p, Text))
        assert "post evidence" in text
        calls = [data for kind, data in published if kind == "code.call"]
        assert len(calls) == 1
        if decision == "block":
            assert "blocked by hook" in calls[0]["error"] and not any(name == "bash" for name, args in approvals)
        elif decision == "modify":
            assert ("bash", {"command": "changed"}) in approvals
            assert "changed the input" in text
    finally:
        await kernels.shutdown()


async def test_tool_time_budget_cancels_work_and_disables_tools(tmp_path):
    import asyncio

    finished = []

    async def slow(args, ctx):
        await asyncio.sleep(5)
        finished.append(True)
        return "late"

    provider = Script()
    agent = Agent(provider, model="m", workspace=tmp_path,
                  tools=(Tool(ToolSpec("bash", "fake", {"type": "object"}), slow),), max_tool_seconds=0.05)
    events = [event async for event in agent.turn("go")]
    assert not finished and not provider.requests[-1].tools
    assert any(isinstance(event, ToolEnd) and "time budget expired" in event.result.output for event in events)


@pytest.mark.parametrize("nested", [False, True])
async def test_post_hook_deadline_preserves_completed_effect_and_result(tmp_path, nested):
    import asyncio

    from nexus.kernels import Kernels
    from nexus.tools import ToolContext
    from nexus.tools.code import SPEC

    kernels, effects, records = Kernels(), [], []

    async def allow(*_):
        return True

    async def effect(args, ctx):
        effects.append(True)
        return "IMPORTANT SUCCESS RESULT"

    tool = Tool(ToolSpec("bash", "fake effect", {"type": "object"}), effect)

    async def code(args, ctx):
        return await kernels.run(args, root="r", caller="s", ctx=ctx, tools=[tool], approve=allow,
                                 publish=lambda kind, data: records.append((kind, data)))

    class Provider(Script):
        async def stream(self, request):
            self.requests.append(request)
            call = ToolCall("c", "code", json.dumps({"code": "print(await bash(command='fake'))"})) if nested else ToolCall("c", "bash", "{}")
            yield PartDone(call if len(self.requests) == 1 else Text("partial report"))
            yield Done("stop")

    provider = Provider()
    agent = Agent(provider, model="m", workspace=tmp_path, tools=(tool, Tool(SPEC, code)), max_tool_seconds=0.2)
    fire = agent._fire

    async def slow_post(event, **kwargs):
        if event == "PostToolUse" and kwargs.get("tool") == "bash":
            await asyncio.sleep(5)
        return await fire(event, **kwargs)

    agent._fire = slow_post
    try:
        await kernels.run({"code": "0"}, root="r", caller="s", ctx=ToolContext(tmp_path), tools=[],
                          approve=allow, publish=lambda *_: None)
        events = [event async for event in agent.turn("go")]
        assert effects == [True] and not provider.requests[-1].tools
        if nested:
            calls = [data for kind, data in records if kind == "code.call"]
            assert len(calls) == 1 and calls[0]["output"] == "IMPORTANT SUCCESS RESULT" and calls[0]["error"] is None
        else:
            [end] = [event for event in events if isinstance(event, ToolEnd)]
            assert end.result.output == "IMPORTANT SUCCESS RESULT" and not end.result.is_error
        assert any("result is preserved" in p.text for m in provider.requests[-1].messages for p in m.parts if isinstance(p, Text))
    finally:
        await kernels.shutdown()
