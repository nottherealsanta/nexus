"""Loop tests (plan section 9).

The loop is driven entirely through fakes — a fake session, lease, assembler,
resolver, and sink — so these tests prove the protocol boundary holds without
importing any concrete manager. Integration with the real session store belongs
to the next packet.
"""
import ast
import asyncio
from pathlib import Path

import pytest

from nexus.core.cancel import CancelToken
from nexus.core.loop import ResolvedModel, run_turn
from nexus.core.turn import TurnLimits, TurnState
from nexus.errors import ProviderError
from nexus.events import Event
from nexus.model.capabilities import Capabilities
from nexus.model.message import Message, Text, ToolResult, ToolUse
from nexus.model.providers.scripted import (
    ScriptedProvider,
    Wait,
    text_response,
    tool_response,
)
from nexus.model.request import ModelRequest
from nexus.model.stream import (
    MessageStart,
    MessageStop,
    TextDelta,
    ThinkingDelta,
    ThinkingEnd,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

TERMINAL_EVENTS = ("turn.completed", "turn.failed", "turn.cancelled")


# ---------------------------------------------------------------------------
# Fakes (protocol-only)
# ---------------------------------------------------------------------------


class _MessageRecord:
    def __init__(self, seq: int, message: Message):
        self.seq = seq
        self.message = message


class _EventRecord:
    def __init__(self, seq: int, event: Event):
        self.seq = seq
        self.event = event


class FakeLease:
    def __init__(self, turn_id: str, limits: TurnLimits | None = None):
        self.turn_id = turn_id
        self.state = TurnState.new(turn_id=turn_id, session_id="s1").start()
        self.cancel_token = CancelToken()
        self.limits = limits
        self.released = False
        self.release_count = 0

    def release(self) -> None:
        self.released = True
        self.release_count += 1


class FakeSession:
    def __init__(self, session_id: str = "s1"):
        self.id = session_id
        self._messages: list[Message] = []
        self.records: list[_MessageRecord] = []
        self.begun = 0
        self.leases: list[FakeLease] = []
        self._seq = 0

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    @property
    def appended(self) -> list[Message]:
        return [record.message for record in self.records]

    def append_message(self, message: Message, *, seq: int | None = None) -> _MessageRecord:
        self._seq += 1
        record = _MessageRecord(self._seq, message)
        self._messages.append(message)
        self.records.append(record)
        return record

    def begin_turn(
        self, *, turn_id: str | None = None, limits: TurnLimits | None = None
    ) -> FakeLease:
        self.begun += 1
        lease = FakeLease(turn_id or f"turn-{self.begun}", limits=limits)
        self.leases.append(lease)
        return lease


class FakeSink:
    """Assigns a monotonic persistence sequence, standing in for the store."""

    def __init__(self):
        self.events: list[_EventRecord] = []

    def emit(self, event: Event) -> _EventRecord:
        record = _EventRecord(len(self.events) + 1, event)
        self.events.append(record)
        return record

    @property
    def types(self) -> list[str]:
        return [record.event.type for record in self.events]


class AppendEventSink:
    """A session-like sink exposing only ``append_event``."""

    def __init__(self):
        self.events: list[Event] = []

    def append_event(self, event: Event) -> Event:
        self.events.append(event)
        return event


class FakeAssembler:
    def __init__(self, system: str | None = None):
        self.system = system
        self.calls: list[ModelRequest] = []

    def assemble(self, session):
        request = ModelRequest(messages=session.messages, system=self.system)
        self.calls.append(request)
        return request


def make_resolver(provider, model: str = "m"):
    capabilities = provider.capabilities(model)

    def resolve(request: ModelRequest) -> ResolvedModel:
        return ResolvedModel(provider, model, capabilities)

    return resolve


def _run(session, provider, sink, lease, *, user_input="hi", **kwargs):
    return run_turn(
        session=session,
        user_input=user_input,
        assemble=FakeAssembler(),
        provider_for=make_resolver(provider),
        emit=sink,
        lease=lease,
        **kwargs,
    )


def _assert_single_terminal(sink: FakeSink, expected: str) -> None:
    terminals = [t for t in sink.types if t in TERMINAL_EVENTS]
    assert terminals == [expected]


# ---------------------------------------------------------------------------
# Text-only turns
# ---------------------------------------------------------------------------


async def test_text_only_event_and_message_ordering():
    provider = ScriptedProvider(
        text_response("Hello", usage=Usage(input=3, output=2))
    )
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("turn-1")

    outcome = await _run(session, provider, sink, lease, user_input=[Text(text="hi")])

    assert outcome.phase == "completed"
    assert outcome.stop_reason == "end_turn"
    assert outcome.ok

    assert [m.role for m in session.appended] == ["user", "assistant"]
    assistant = session.appended[1]
    assert [type(b).__name__ for b in assistant.content] == ["Text"]
    assert assistant.content[0].text == "Hello"
    assert assistant.meta.turn_id == "turn-1"

    types = sink.types
    assert types[0] == "turn.started"
    assert types[1] == "context.assembled"
    assert types[2] == "model.started"
    assert types.index("text.delta") < types.index("text")
    assert types.index("text") < types.index("model.usage")
    assert types.index("model.usage") < types.index("model.stopped")
    assert types[-1] == "turn.completed"
    model_started = next(record.event for record in sink.events if record.event.type == "model.started")
    assert model_started.data["reasoning_effort"] is None
    _assert_single_terminal(sink, "turn.completed")

    for index, record in enumerate(sink.events, start=1):
        assert record.event.session == "s1"
        assert record.event.turn == "turn-1"
        assert record.event.seq == 0  # the store assigns seq, not the loop
        assert record.seq == index  # but persistence order is monotonic
    assert lease.released


async def test_structured_history_reaches_assembler_including_new_input():
    session = FakeSession()
    session.append_message(Message(role="user", content=[Text(text="old")]))
    session.append_message(Message(role="assistant", content=[Text(text="reply")]))
    provider = ScriptedProvider(text_response("ok"))
    sink = FakeSink()
    lease = FakeLease("t")
    assembler = FakeAssembler(system="sys")

    outcome = await run_turn(
        session=session,
        user_input="new",
        assemble=assembler,
        provider_for=make_resolver(provider),
        emit=sink,
        lease=lease,
    )

    assert outcome.ok
    assert len(assembler.calls) == 1
    request = assembler.calls[0]
    assert request.system == "sys"
    assert [m.content[0].text for m in request.messages] == ["old", "reply", "new"]
    assert provider.requests[0].model == "m"


async def test_text_thinking_and_signature_are_collected_in_order():
    script = [
        MessageStart(model="m", provider="scripted"),
        ThinkingDelta(text="plan"),
        ThinkingEnd(signature="sig-1"),
        TextDelta(text="answer"),
        Usage(input=5, output=4),
        MessageStop(stop_reason="end_turn"),
    ]
    provider = ScriptedProvider(script)
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")

    outcome = await _run(session, provider, sink, lease)

    assert outcome.ok
    assistant = session.appended[1]
    assert [type(b).__name__ for b in assistant.content] == ["Thinking", "Text"]
    assert assistant.content[0].signature == "sig-1"
    assert assistant.content[1].text == "answer"

    thinking_event = next(r.event for r in sink.events if r.event.type == "thinking")
    assert thinking_event.data == {"text": "plan", "signature": "sig-1"}
    assert sink.types.index("thinking.delta") < sink.types.index("text.delta")
    assert sink.types.index("text") < sink.types.index("thinking")


# ---------------------------------------------------------------------------
# Tool calls without a dispatcher
# ---------------------------------------------------------------------------


async def test_valid_tool_calls_get_synthetic_results_and_continue():
    provider = ScriptedProvider(
        tool_response(("c1", "Read", {"path": "a"})),
        text_response("recovered"),
    )
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")

    outcome = await _run(session, provider, sink, lease)

    assert provider.calls == 2
    assert len(provider.requests) == 2
    assert [m.role for m in session.appended] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assistant = session.appended[1]
    assert [type(b).__name__ for b in assistant.content] == ["ToolUse"]
    assert assistant.content[0].input == {"path": "a"}

    results = session.appended[2].content
    assert len(results) == 1
    assert isinstance(results[0], ToolResult)
    assert results[0].is_error is True
    assert results[0].tool_use_id == "c1"
    assert "Phase 2" in results[0].content[0].text
    assert outcome.stop_reason == "end_turn"

    # Assistant message is durably before the result message.
    assert session.records[1].seq < session.records[2].seq
    _assert_single_terminal(sink, "turn.completed")


async def test_interleaved_tool_calls_preserve_wire_order():
    script = [
        MessageStart(model="m", provider="scripted"),
        ToolCallStart(id="a", name="Read"),
        ToolCallDelta(id="a", partial_json='{"path": "a'),
        ToolCallStart(id="b", name="Write"),
        ToolCallDelta(id="b", partial_json='{"path": "b'),
        ToolCallDelta(id="a", partial_json='.txt"}'),
        ToolCallEnd(id="a", input={}),
        ToolCallDelta(id="b", partial_json='.txt"}'),
        ToolCallEnd(id="b", input={}),
        MessageStop(stop_reason="tool_use"),
    ]
    provider = ScriptedProvider(script, text_response("done"))
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")

    outcome = await _run(session, provider, sink, lease)

    assert outcome.ok
    assistant = session.appended[1]
    tool_uses = [b for b in assistant.content if isinstance(b, ToolUse)]
    assert [b.id for b in tool_uses] == ["a", "b"]
    assert [b.name for b in tool_uses] == ["Read", "Write"]
    assert [b.input for b in tool_uses] == [{"path": "a.txt"}, {"path": "b.txt"}]

    results = session.appended[2].content
    assert [b.tool_use_id for b in results] == ["a", "b"]


async def test_malformed_tool_arguments_become_visible_error_result():
    script = [
        MessageStart(model="m", provider="scripted"),
        ToolCallStart(id="c1", name="Read"),
        ToolCallDelta(id="c1", partial_json='{"path": '),
        ToolCallEnd(id="c1", input={}),
        MessageStop(stop_reason="tool_use"),
    ]
    provider = ScriptedProvider(script, text_response("fixed"))
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")

    outcome = await _run(session, provider, sink, lease)

    assert outcome.ok
    assistant = session.appended[1]
    tool_uses = [b for b in assistant.content if isinstance(b, ToolUse)]
    assert [b.id for b in tool_uses] == ["c1"]

    results = session.appended[2].content
    assert isinstance(results[0], ToolResult)
    assert results[0].is_error is True
    assert "malformed" in results[0].content[0].text.lower()
    assert provider.calls == 2
    _assert_single_terminal(sink, "turn.completed")


async def test_malformed_budget_exhaustion_fails_the_turn():
    script = [
        MessageStart(model="m", provider="scripted"),
        ToolCallStart(id="c1", name="Read"),
        ToolCallDelta(id="c1", partial_json='{"path": '),
        ToolCallEnd(id="c1", input={}),
        MessageStop(stop_reason="tool_use"),
    ]
    provider = ScriptedProvider(script, text_response("ignored"))
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")

    outcome = await _run(session, provider, sink, lease, malformed_budget=0)

    assert outcome.phase == "failed"
    assert "malformed" in outcome.error
    assert provider.calls == 1
    _assert_single_terminal(sink, "turn.failed")


# ---------------------------------------------------------------------------
# Protocol-level tool dispatch (fakes only; no concrete manager imported)
# ---------------------------------------------------------------------------


class _FakeEntry:
    def __init__(self, call):
        self.call = call
        self.spec = object()
        self.key = call.name
        self.error = None
        self.code = None
        self.decision = None


class _FakePrepared:
    def __init__(self, calls):
        self.entries = [_FakeEntry(call) for call in calls]
        self.decisions = {}

    def calls(self):
        return [entry.call for entry in self.entries]

    def spec_map(self):
        return {entry.call.name: entry.spec for entry in self.entries}

    def apply_plan(self, plan):
        return self

    def with_decisions(self, decisions):
        self.decisions = dict(decisions)
        return self


class _FakeEvaluation:
    def __init__(self, call, outcome, code, reason):
        self.call = call
        self.spec = object()
        self.key = call.name
        self.outcome = outcome
        self.decision = None
        self.code = code
        self.reason = reason


class _FakePlan:
    def __init__(self, evaluations):
        self.evaluations = evaluations

    def asks(self):
        return [e for e in self.evaluations if e.outcome == "ask"]

    def failures(self):
        return [e for e in self.evaluations if e.outcome == "fail_turn"]


class _FakeRequest:
    def __init__(self, call):
        self.id = f"req-{call.id}"
        self.call = call

    def to_dict(self):
        return {"id": self.id, "call_id": self.call.id, "tool": self.call.name}


class _FakeGate:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.opened = []
        self.cancelled = 0

    def plan(self, prepared):
        evaluations = []
        for index, call in enumerate(prepared.calls()):
            outcome, code, reason = self._outcomes[index]
            evaluations.append(_FakeEvaluation(call, outcome, code, reason))
        return _FakePlan(evaluations)

    def request_for(self, evaluation):
        return _FakeRequest(evaluation.call)

    def open(self, request):
        self.opened.append(request.id)

    async def await_decision(self, request, /, *, cancel):
        return "allow_once"

    def resolution(self, request_id):
        return {"id": request_id, "decision": "allow_once"}

    def resolve(self, request_id, decision):
        return True

    def cancel_pending(self):
        self.cancelled += 1


class _FakeDispatcher:
    def __init__(self):
        self.prepare_calls = 0
        self.dispatch_calls = 0
        self.emitted: list[str] = []

    def prepare(self, tool_uses):
        self.prepare_calls += 1
        return _FakePrepared(list(tool_uses))

    async def dispatch(self, prepared, /, *, emit, cancel, parallel_allowed=True):
        self.dispatch_calls += 1
        results = []
        for entry in prepared.entries:
            await emit(
                "tool.started", {"call_id": entry.call.id, "tool": entry.call.name}
            )
            self.emitted.append("tool.started")
            await emit(
                "tool.completed", {"call_id": entry.call.id, "tool": entry.call.name}
            )
            self.emitted.append("tool.completed")
            results.append(
                ToolResult(
                    tool_use_id=entry.call.id,
                    content=[Text(text=f"ran {entry.call.name}")],
                )
            )
        return tuple(results)


async def test_protocol_tools_dispatch_and_persist_in_order():
    provider = ScriptedProvider(
        tool_response(("a", "Read", {"path": "a"}), ("b", "Read", {"path": "b"})),
        text_response("done"),
    )
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")
    dispatcher = _FakeDispatcher()
    gate = _FakeGate([("allow", "allow", "ok"), ("allow", "allow", "ok")])

    outcome = await run_turn(
        session=session,
        user_input="go",
        assemble=FakeAssembler(),
        provider_for=make_resolver(provider),
        emit=sink,
        tools=dispatcher,
        gate=gate,
        lease=lease,
    )

    assert outcome.ok
    assert dispatcher.dispatch_calls == 1
    assert [m.role for m in session.appended] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    results = session.appended[2].content
    assert [r.tool_use_id for r in results] == ["a", "b"]
    assert [r.content[0].text for r in results] == ["ran Read", "ran Read"]
    # Assistant tool_use is durable before the result message.
    assert session.records[1].seq < session.records[2].seq
    assert lease.released


async def test_protocol_fail_turn_ends_before_dispatch():
    provider = ScriptedProvider(
        tool_response(("a", "Read", {"path": "a"})), text_response("unreached")
    )
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")
    dispatcher = _FakeDispatcher()
    gate = _FakeGate([("fail_turn", "unattended_fail_turn", "no approver")])

    outcome = await run_turn(
        session=session,
        user_input="go",
        assemble=FakeAssembler(),
        provider_for=make_resolver(provider),
        emit=sink,
        tools=dispatcher,
        gate=gate,
        lease=lease,
    )

    assert outcome.phase == "failed"
    assert dispatcher.dispatch_calls == 0
    assert "tool.started" not in dispatcher.emitted
    _assert_single_terminal(sink, "turn.failed")
    assert lease.released


async def test_protocol_provider_without_tools_never_dispatches():
    provider = ScriptedProvider(
        tool_response(("a", "Read", {"path": "a"})),
        text_response("ok"),
        capabilities=Capabilities(tools=False, streaming=True),
    )
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")
    dispatcher = _FakeDispatcher()
    gate = _FakeGate([("allow", "allow", "ok")])

    outcome = await run_turn(
        session=session,
        user_input="go",
        assemble=FakeAssembler(),
        provider_for=make_resolver(provider),
        emit=sink,
        tools=dispatcher,
        gate=gate,
        lease=lease,
    )

    assert outcome.ok
    assert dispatcher.prepare_calls == 0
    assert dispatcher.dispatch_calls == 0
    assert provider.requests[0].tools == []
    result = session.appended[2].content[0]
    assert result.is_error is True
    assert "does not support tool calls" in result.content[0].text


# ---------------------------------------------------------------------------
# Cancellation, failure, and limits
# ---------------------------------------------------------------------------


async def test_cancellation_at_a_streaming_wait_point():
    provider = ScriptedProvider(
        [
            MessageStart(model="m", provider="scripted"),
            TextDelta(text="partial"),
            Wait(),
            MessageStop(stop_reason="end_turn"),
        ]
    )
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")

    task = asyncio.create_task(_run(session, provider, sink, lease))

    for _ in range(50):
        await asyncio.sleep(0)
        if "text.delta" in sink.types:
            break
    assert "text.delta" in sink.types
    assert not task.done()

    lease.cancel_token.cancel("user cancelled")
    outcome = await asyncio.wait_for(task, timeout=2)

    assert outcome.phase == "cancelled"
    assert outcome.stop_reason == "cancelled"
    assert lease.released
    _assert_single_terminal(sink, "turn.cancelled")


async def test_external_cancel_token_is_forwarded_to_the_lease():
    provider = ScriptedProvider([MessageStart(model="m", provider="scripted"), Wait()])
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")
    external = CancelToken()

    task = asyncio.create_task(
        _run(session, provider, sink, lease, cancel=external)
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if sink.types:
            break
    external.cancel("from ui")
    outcome = await asyncio.wait_for(task, timeout=2)

    assert outcome.phase == "cancelled"
    assert outcome.error == "from ui"
    assert lease.released


async def test_provider_failure_is_a_failed_turn_and_releases_the_lease():
    provider = ScriptedProvider(
        [MessageStart(model="m", provider="scripted"), ProviderError("overloaded")]
    )
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")

    outcome = await _run(session, provider, sink, lease)

    assert outcome.phase == "failed"
    assert outcome.stop_reason == "error"
    assert "overloaded" in outcome.error
    assert lease.released
    _assert_single_terminal(sink, "turn.failed")
    assert provider.requests  # the request was recorded before the failure


async def test_exception_before_any_event_still_fails_cleanly():
    provider = ScriptedProvider([ProviderError("no connection")])
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")

    outcome = await _run(session, provider, sink, lease)

    assert outcome.phase == "failed"
    assert "no connection" in outcome.error
    assert lease.released
    _assert_single_terminal(sink, "turn.failed")


async def test_max_iterations_bounds_synthetic_tool_loop():
    provider = ScriptedProvider(
        *[tool_response(("c1", "Read", {"path": "a"})) for _ in range(5)]
    )
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")

    outcome = await _run(
        session, provider, sink, lease, limits=TurnLimits(max_iterations=3)
    )

    assert outcome.phase == "completed"
    assert outcome.stop_reason == "max_iterations"
    assert provider.calls == 3
    assert outcome.iterations == 3
    _assert_single_terminal(sink, "turn.completed")


async def test_wall_clock_limit_completes_as_budget():
    class Clock:
        def __init__(self, values):
            self._values = list(values)

        def __call__(self):
            value = self._values.pop(0)
            return value

    provider = ScriptedProvider(
        tool_response(("c1", "Read", {"path": "a"})),
        text_response("unreached"),
    )
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")

    outcome = await _run(
        session,
        provider,
        sink,
        lease,
        limits=TurnLimits(max_iterations=5, max_seconds=10.0),
        clock=Clock([0.0, 0.0, 100.0, 100.0, 100.0]),
    )

    assert outcome.phase == "completed"
    assert outcome.stop_reason == "budget"
    assert provider.calls == 1
    _assert_single_terminal(sink, "turn.completed")


# ---------------------------------------------------------------------------
# Lease seam
# ---------------------------------------------------------------------------


async def test_loop_acquires_and_releases_a_lease_when_none_supplied():
    provider = ScriptedProvider(text_response("hi"))
    session = FakeSession()
    sink = FakeSink()

    outcome = await run_turn(
        session=session,
        user_input="hi",
        assemble=FakeAssembler(),
        provider_for=make_resolver(provider),
        emit=sink,
    )

    assert outcome.ok
    assert session.begun == 1
    assert session.leases[0].released


async def test_supplied_lease_is_used_and_released():
    provider = ScriptedProvider(text_response("hi"))
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("external-turn")

    outcome = await _run(session, provider, sink, lease)

    assert outcome.turn_id == "external-turn"
    assert session.begun == 0
    assert lease.released
    assert lease.release_count == 1


async def test_loop_accepts_session_style_event_sink_with_append_event():
    provider = ScriptedProvider(text_response("hi"))
    session = FakeSession()
    sink = AppendEventSink()

    outcome = await run_turn(
        session=session,
        user_input="hi",
        assemble=FakeAssembler(),
        provider_for=make_resolver(provider),
        emit=sink,
    )

    assert outcome.ok
    assert sink.events[0].type == "turn.started"
    assert sink.events[-1].type == "turn.completed"
    assert all(event.session == "s1" for event in sink.events)
    assert all(event.turn for event in sink.events)


async def test_empty_user_input_is_rejected_before_any_event_or_lease():
    provider = ScriptedProvider(text_response("hi"))
    session = FakeSession()
    sink = FakeSink()

    with pytest.raises(ValueError):
        await run_turn(
            session=session,
            user_input=[],
            assemble=FakeAssembler(),
            provider_for=make_resolver(provider),
            emit=sink,
        )

    assert session.begun == 0
    assert sink.types == []


# ---------------------------------------------------------------------------
# Protocol-only layering
# ---------------------------------------------------------------------------

_CONCRETE_SYMBOLS = {
    "Session",
    "SessionManager",
    "ContextManager",
    "Runtime",
    "ToolManager",
    "AnthropicProvider",
    "ScriptedProvider",
    "LegacyCodexCLIProvider",
}
_FORBIDDEN_PACKAGES = (
    "nexus.session",
    "nexus.context",
    "nexus.runtime",
    "nexus.tools",
    "nexus.agent",
    "nexus.cli",
    "nexus.provider",
    "nexus.model.providers",
)
_PACKAGE = ("nexus", "core")


def _resolve_from(node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    base = _PACKAGE[: len(_PACKAGE) - (node.level - 1)]
    parts = list(base)
    if node.module:
        parts.append(node.module)
    return ".".join(parts)


def test_loop_is_protocol_only():
    path = Path(__file__).resolve().parents[1] / "nexus" / "core" / "loop.py"
    tree = ast.parse(path.read_text())
    imported_modules: set[str] = set()
    imported_symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.add(_resolve_from(node))
            imported_symbols.update(alias.name for alias in node.names)

    for module in imported_modules:
        for forbidden in _FORBIDDEN_PACKAGES:
            assert not module.startswith(forbidden), (module, forbidden)
    assert not (imported_symbols & _CONCRETE_SYMBOLS)


def test_loop_does_not_import_network_or_lock_modules():
    path = Path(__file__).resolve().parents[1] / "nexus" / "core" / "loop.py"
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    assert not (modules & {"httpx", "fcntl"})


def test_msgspec_is_the_only_third_party_import_in_loop():
    path = Path(__file__).resolve().parents[1] / "nexus" / "core" / "loop.py"
    tree = ast.parse(path.read_text())
    third_party = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            third_party.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            third_party.add(node.module.split(".")[0])
    assert third_party <= {
        "asyncio",
        "collections",
        "contextlib",
        "inspect",
        "msgspec",
        "re",
        "time",
        "typing",
        "__future__",
    }


# ---------------------------------------------------------------------------
# Provider stream cleanup (source generator must close promptly)
# ---------------------------------------------------------------------------


class _ClosingProvider:
    """Provider whose source async generator records when its ``finally`` runs."""

    name = "closing"

    def __init__(self, mode: str, *, started: asyncio.Event | None = None):
        self._mode = mode
        self._started = started
        self.closed = False

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(streaming=True)

    def stream(self, req: ModelRequest):
        return self._stream(req)

    async def _stream(self, req: ModelRequest):
        try:
            yield MessageStart(model="m", provider=self.name)
            if self._mode == "cancel":
                if self._started is not None:
                    self._started.set()
                await asyncio.Event().wait()
            elif self._mode == "malformed":
                yield ToolCallStart(id="c1", name="Read")
                yield ToolCallDelta(id="c1", partial_json='{"path": ')
                yield ToolCallEnd(id="c1", input={})
                yield MessageStop(stop_reason="tool_use")
        finally:
            self.closed = True


async def test_source_generator_finally_runs_on_cancellation():
    started = asyncio.Event()
    provider = _ClosingProvider("cancel", started=started)
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")

    task = asyncio.create_task(_run(session, provider, sink, lease))
    await asyncio.wait_for(started.wait(), timeout=2)
    lease.cancel_token.cancel("stop")
    outcome = await asyncio.wait_for(task, timeout=2)

    assert outcome.phase == "cancelled"
    assert provider.closed is True
    assert lease.released


async def test_source_generator_finally_runs_on_malformed_stream_abort():
    provider = _ClosingProvider("malformed")
    session = FakeSession()
    sink = FakeSink()
    lease = FakeLease("t")

    outcome = await _run(session, provider, sink, lease, malformed_budget=0)

    assert provider.closed is True
    assert outcome.phase == "failed"
    assert lease.released


async def test_terminal_emit_failure_still_releases_supplied_lease():
    provider = ScriptedProvider(text_response("hi"))
    session = FakeSession()
    lease = FakeLease("t")

    class FailingSink:
        def emit(self, event):
            if event.type == "turn.completed":
                raise RuntimeError("sink failure")
            return event

    with pytest.raises(RuntimeError, match="sink failure"):
        await run_turn(
            session=session,
            user_input="hi",
            assemble=FakeAssembler(),
            provider_for=make_resolver(provider),
            emit=FailingSink(),
            lease=lease,
        )

    assert lease.released
    assert lease.release_count >= 1


def test_usage_event_reports_the_whole_prompt_for_every_adapter():
    from nexus.core.loop import _usage_event_data
    from nexus.model.providers.anthropic import AnthropicProvider
    from nexus.model.stream import Usage

    # Anthropic's ``input`` omits cache reads and writes; OpenAI-style ``input`` already includes them.
    assert _usage_event_data(Usage(input=10, output=5, cache_read=80, cache_write=10), AnthropicProvider)["prompt"] == 100
    assert _usage_event_data(Usage(input=100, output=5, cache_read=80), object())["prompt"] == 100
    assert "prompt" not in _usage_event_data(Usage(), object())
