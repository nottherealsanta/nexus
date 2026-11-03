from nexus.llmhub.types import Done, PartDone, Request, Text, ToolCall, ToolResult
from nexus.loop import Agent, ToolEnd, TurnEnd


class Scripted:
    """A provider that replays one scripted reply per request and records requests."""

    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[Request] = []

    async def stream(self, req):
        self.requests.append(req)
        parts = self.replies.pop(0)
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


async def test_loop_runs_tools_until_the_model_stops(tmp_path):
    provider = Scripted([
        [ToolCall("c1", "write", '{"path": "x.txt", "content": "hi"}')],
        [Text("done")],
    ])
    agent = Agent(provider, model="m", workspace=tmp_path)
    events = [e async for e in agent.turn("make x.txt")]

    assert (tmp_path / "x.txt").read_text() == "hi"
    ends = [e for e in events if isinstance(e, ToolEnd)]
    assert ends[0].result == ToolResult("c1", "created x.txt (1 lines, 2 bytes)")
    assert events[-1] == TurnEnd("end_turn", 2)
    # The second request carries the call and its result.
    assert [m.role for m in provider.requests[1].messages] == ["user", "assistant", "tool"]


async def test_declined_and_unknown_tools_are_errors(tmp_path):
    async def deny(call, args):
        return False

    provider = Scripted([[ToolCall("c1", "bash", '{"command": "rm -rf /"}'), ToolCall("c2", "nope", "{}")], [Text("ok")]])
    agent = Agent(provider, model="m", workspace=tmp_path, approve=deny)
    events = [e async for e in agent.turn("go")]
    results = [e.result for e in events if isinstance(e, ToolEnd)]
    assert all(r.is_error for r in results)
    assert "declined" in results[0].output and "unknown tool" in results[1].output


async def test_step_limit_disables_tools_and_asks_for_a_summary(tmp_path):
    provider = Scripted([
        [ToolCall("c1", "write", '{"path": "a.txt", "content": "a"}')],
        [Text("limit reached; a.txt is written, nothing else is left")],
    ])
    agent = Agent(provider, model="m", workspace=tmp_path, max_steps=2)
    events = [e async for e in agent.turn("go")]
    final = provider.requests[1].messages[-1]
    assert final.role == "user" and "step limit" in final.parts[0].text and "text only" in final.parts[0].text
    assert "step limit" not in str(provider.requests[0].messages)
    assert events[-1] == TurnEnd("max_steps", 2)


async def test_a_tool_call_after_the_step_limit_is_refused_and_not_run(tmp_path):
    provider = Scripted([
        [ToolCall("c1", "write", '{"path": "a.txt", "content": "a"}')],
        [ToolCall("c2", "write", '{"path": "b.txt", "content": "b"}')],
    ])
    agent = Agent(provider, model="m", workspace=tmp_path, max_steps=2)
    events = [e async for e in agent.turn("go")]
    results = [e.result for e in events if isinstance(e, ToolEnd)]
    assert not (tmp_path / "b.txt").exists() and results[-1].is_error and "step limit" in results[-1].output
    assert events[-1] == TurnEnd("max_steps", 2)


async def test_edit_patch_and_write_need_no_prior_read(tmp_path):
    (tmp_path / "f.txt").write_text("one\ntwo\n")
    provider = Scripted([
        [ToolCall("e", "edit", '{"path": "f.txt", "old_string": "one", "new_string": "1"}')],
        [ToolCall("e2", "edit", '{"path": "f.txt", "old_string": "two", "new_string": "2"}')],
        [ToolCall("p", "patch", '{"patch": "*** Begin Patch\\n*** Update File: f.txt\\n@@\\n-1\\n+uno\\n*** End Patch"}')],
        [ToolCall("w", "write", '{"path": "f.txt", "content": "done\\n"}')],
        [Text("ok")],
    ])
    agent = Agent(provider, model="m", workspace=tmp_path)
    events = [e async for e in agent.turn("go")]
    assert not any(e.result.is_error for e in events if isinstance(e, ToolEnd))
    assert (tmp_path / "f.txt").read_text() == "done\n"
