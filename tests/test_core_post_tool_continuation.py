"""Deterministic parent continuation across tool and provider outcomes."""

import pytest

from nexus.core.loop import run_turn
from nexus.core.turn import TurnLimits
from nexus.errors import ProviderError
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from nexus.model.message import Text, ToolResult
from nexus.model.stream import MessageStop

from test_core_loop import (
    FakeAssembler,
    FakeLease,
    FakeSession,
    FakeSink,
    _FakeDispatcher,
    _FakeGate,
    _assert_single_terminal,
    make_resolver,
)


@pytest.mark.parametrize("tool_outcome", ["success", "failure", "subagent_exhaustion"])
@pytest.mark.parametrize("continuation", ["answer", "exception", "error_stop", "eof", "empty_stop"])
async def test_parent_continues_after_result_without_redispatch(tool_outcome, continuation):
    session, sink, lease = FakeSession(), FakeSink(), FakeLease("parent")
    children = []

    class Dispatcher(_FakeDispatcher):
        async def dispatch(self, prepared, /, *, emit, cancel, parallel_allowed=True):
            self.dispatch_calls += 1
            if tool_outcome == "subagent_exhaustion":
                child_session, child_sink = FakeSession("child"), FakeSink()
                child_lease = FakeLease("child", TurnLimits(max_iterations=1))
                child_provider = ScriptedProvider(
                    tool_response(("child-call", "Read", {})),
                    text_response("must not be reached"),
                )
                child = await run_turn(
                    session=child_session, user_input="child task",
                    assemble=FakeAssembler(), provider_for=make_resolver(child_provider),
                    emit=child_sink, lease=child_lease, tools=_FakeDispatcher(),
                    gate=_FakeGate([("allow", "allow", "ok")]),
                )
                assert child.stop_reason == "max_iterations"
                assert not child.ok
                assert child_provider.calls == 1
                assert child_lease.release_count == 1
                _assert_single_terminal(child_sink, "turn.completed")
                children.append(child)
                content = [Text(text="subagent exhausted: max_iterations")]
            else:
                content = [Text(text=f"tool {tool_outcome}")]
            return tuple(ToolResult(
                tool_use_id=call.id, content=content,
                is_error=tool_outcome != "success",
            ) for call in prepared.calls())

    second = {
        "answer": text_response("parent continued"),
        "exception": [ProviderError("continuation unavailable")],
        "error_stop": [MessageStop(stop_reason="error")],
        "eof": [],
        "empty_stop": [MessageStop(stop_reason="end_turn")],
    }[continuation]
    provider = ScriptedProvider(
        tool_response(("parent-call", "Task", {})), second,
        text_response("must not be retried"),
    )
    assembler, dispatcher = FakeAssembler(), Dispatcher()
    outcome = await run_turn(
        session=session, user_input="parent task", assemble=assembler,
        provider_for=make_resolver(provider), emit=sink, lease=lease,
        tools=dispatcher, gate=_FakeGate([("allow", "allow", "ok")]),
    )

    assert provider.calls == len(assembler.calls) == 2, outcome
    assert dispatcher.dispatch_calls == 1
    assert len(children) == (tool_outcome == "subagent_exhaustion")
    result_message = assembler.calls[1].messages[-1]
    assert result_message.role == "user"
    assert len(result_message.content) == 1
    result = result_message.content[0]
    assert isinstance(result, ToolResult)
    assert result.tool_use_id == "parent-call"
    assert result.is_error == (tool_outcome != "success")
    assert sum(isinstance(block, ToolResult) for message in session.messages
               for block in message.content) == 1
    failed = continuation in {"exception", "error_stop"}
    _assert_single_terminal(sink, "turn.failed" if failed else "turn.completed")
    assert outcome.phase == ("failed" if failed else "completed")
    assert sink.types.count("tool.result") == 1
    result_index = sink.types.index("tool.result")
    assert sink.types.index("model.started", result_index) > result_index
    assert lease.release_count == 1
    if continuation == "answer":
        assert session.messages[-1].content == [Text(text="parent continued")]


async def test_error_stop_with_tool_call_fails_without_dispatch():
    session, sink, lease = FakeSession(), FakeSink(), FakeLease("parent")
    dispatcher = _FakeDispatcher()
    events = tool_response(("call", "Task", {}))
    events[-1] = MessageStop(stop_reason="error")
    provider = ScriptedProvider(events, text_response("must not be reached"))

    outcome = await run_turn(
        session=session, user_input="task", assemble=FakeAssembler(),
        provider_for=make_resolver(provider), emit=sink, lease=lease,
        tools=dispatcher, gate=_FakeGate([("allow", "allow", "ok")]),
    )

    assert outcome.phase == "failed"
    assert provider.calls == 1
    assert dispatcher.dispatch_calls == 0
    assert "tool.requested" not in sink.types
    _assert_single_terminal(sink, "turn.failed")
    assert lease.release_count == 1
