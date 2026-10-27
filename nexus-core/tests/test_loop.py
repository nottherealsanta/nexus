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
