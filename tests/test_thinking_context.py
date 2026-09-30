"""Live thinking context indicators use the durable reducer projection."""
from dataclasses import replace

import msgspec
import pytest
from provider_conformance.adapters import ADAPTER_SPECS
from provider_conformance.contract import EventsStep
from provider_conformance.events import reply_thinking

from nexus.core.cancel import CancelToken
from nexus.core.loop import _BlockCollector, _Emitter, _collect_stream
from nexus.events import Event
from nexus.model.message import Message, Text
from nexus.model.request import ModelRequest
from nexus.view.fold import fold

from nexus.ui_support.context import thinking_status
from nexus.view.model import BlockView, ConversationView, MessageView, TurnView


def test_thinking_status_latest_phrase_and_lifecycle():
    block = BlockView(kind="thinking", text="**Starting search****Checking candidates**")
    message = MessageView(role="assistant", blocks=[block])
    turn = TurnView(messages=[message])
    view = ConversationView(turns=[turn])
    assert thinking_status(view) == "Thinking · Checking candidates"
    message.blocks = [replace(block, finalized=True)]
    assert thinking_status(view) == ""
    message.blocks = [block, BlockView(kind="text", text="Answer")]
    assert thinking_status(view) == ""
    message.blocks = [block]
    message.done = True
    assert thinking_status(view) == ""
    message.done = False
    turn.phase = "completed"
    assert thinking_status(view) == ""


def test_thinking_status_bounded_and_no_previous_turn_leak():
    message = MessageView(role="assistant", blocks=[BlockView(kind="thinking", text="x" * 1000)])
    assert len(thinking_status(ConversationView(turns=[TurnView(messages=[message])]))) == 131
    assert thinking_status(ConversationView(turns=[TurnView(phase="completed", messages=[message]), TurnView()])) == ""


# Unsigned Ollama and OpenCode thought streams also support live display.
THINKING_ADAPTERS = list(ADAPTER_SPECS)


@pytest.mark.parametrize("spec", THINKING_ADAPTERS, ids=lambda spec: spec.name)
async def test_provider_thinking_reaches_context_and_replays_without_duplicates(spec):
    adapter = spec.make()
    adapter.queue(EventsStep(reply_thinking("**Checking candidates**", signature="opaque", text="896")))
    events = [Event("turn.started", turn="t", seq=1), Event("model.started", turn="t", seq=2)]
    states = []

    def emit(event):
        events.append(msgspec.structs.replace(event, seq=len(events) + 1))
        states.append((event.type, thinking_status(fold(events))))

    try:
        await _collect_stream(
            adapter.provider, ModelRequest(messages=[Message("user", [Text("check")])]),
            _BlockCollector(), _Emitter(emit, "s", "t"), CancelToken(),
        )
    finally:
        await adapter.aclose()
    assert ("thinking.delta", "Thinking · Checking candidates") in states
    assert ("thinking.end", "") in states
    assert all(not status for kind, status in states if kind in {"thinking.end", "text.delta"})
    # The final legacy aggregate must not duplicate already-closed thought runs.
    events.append(Event("thinking", {"text": "**Checking candidates**", "signature": "opaque"}, turn="t", seq=len(events) + 1))
    view = fold(events)
    assert view.turns[0].messages[-1].thinking == "**Checking candidates**"
    assert thinking_status(view) == ""


@pytest.mark.parametrize("boundary", ["text", "tool", "stop"])
async def test_unsigned_thinking_closes_at_content_boundary(boundary):
    from nexus.model.providers.scripted import ScriptedProvider
    from nexus.model.stream import MessageStart, MessageStop, TextDelta, ThinkingDelta, ToolCallStart, ToolCallEnd

    following = {
        "text": [TextDelta(text="answer")],
        "tool": [ToolCallStart(id="call", name="Read"), ToolCallEnd(id="call", input={"path": "a"})],
        "stop": [],
    }[boundary]
    provider = ScriptedProvider([MessageStart(), ThinkingDelta(text="checking"), *following, MessageStop(stop_reason="end_turn")])
    events = [Event("turn.started", turn="t", seq=1), Event("model.started", turn="t", seq=2)]
    collector = _BlockCollector()
    def emit(event):
        events.append(msgspec.structs.replace(event, seq=len(events) + 1))
    await _collect_stream(provider, ModelRequest(messages=[]), collector, _Emitter(emit, "s", "t"), CancelToken())
    assert "thinking.end" in [event.type for event in events]
    assert thinking_status(fold(events)) == ""
    assert collector.blocks()[0].signature is None
