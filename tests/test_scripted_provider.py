"""Tests for the deterministic ``ScriptedProvider`` test double."""
import ast
import asyncio
from pathlib import Path

import pytest

from nexus.errors import ProviderError
from nexus.model.capabilities import Capabilities
from nexus.model.message import Message, Text
from nexus.model.provider import Provider
from nexus.model.providers.scripted import (
    ScriptedProvider,
    ScriptExhausted,
    Wait,
    text_response,
    tool_response,
)
from nexus.model.request import ModelRequest
from nexus.model.stream import (
    MessageStart,
    MessageStop,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)


def request() -> ModelRequest:
    return ModelRequest(messages=[Message(role="user", content=[Text(text="hi")])])


async def collect(provider: ScriptedProvider, req: ModelRequest | None = None):
    return [event async for event in provider.stream(req or request())]


def test_provider_is_protocol_compliant():
    provider = ScriptedProvider(text_response("x"))
    assert isinstance(provider, Provider)
    assert provider.name == "scripted"


async def test_each_call_replays_its_script_in_order_and_records_the_request():
    provider = ScriptedProvider(
        [MessageStart(model="m", provider="scripted"), TextDelta("one"), MessageStop("end_turn")],
        [MessageStart(model="m", provider="scripted"), TextDelta("two"), MessageStop("end_turn")],
    )
    first = await collect(provider)
    second = await collect(provider)

    assert [e.text for e in first if isinstance(e, TextDelta)] == ["one"]
    assert [e.text for e in second if isinstance(e, TextDelta)] == ["two"]
    assert provider.calls == 2
    assert provider.remaining == 0
    assert len(provider.requests) == 2
    assert provider.requests[0].messages[0].content[0].text == "hi"


async def test_builders_produce_expected_event_shapes():
    text_events = text_response("hello", usage=Usage(input=2, output=3))
    assert isinstance(text_events[0], MessageStart)
    assert text_events[1] == TextDelta(text="hello")
    assert any(isinstance(e, Usage) for e in text_events)
    assert isinstance(text_events[-1], MessageStop)
    assert text_events[-1].stop_reason == "end_turn"

    tool_events = tool_response(("c1", "Read", {"path": "a"}))
    assert any(isinstance(e, ToolCallStart) for e in tool_events)
    end = next(e for e in tool_events if isinstance(e, ToolCallEnd))
    assert end.id == "c1"
    assert end.input == {"path": "a"}
    assert tool_events[-1].stop_reason == "tool_use"


async def test_configurable_name_and_capabilities():
    caps = Capabilities(tools=False, streaming=True, thinking=False)
    provider = ScriptedProvider(text_response("x"), name="custom", model="local", capabilities=caps)
    assert provider.name == "custom"
    assert provider.capabilities("whatever") is caps
    assert provider.capabilities("whatever").tools is False

    default = ScriptedProvider(text_response("x"))
    assert default.capabilities("m").tools is True


async def test_scripted_exception_is_raised():
    provider = ScriptedProvider([MessageStart(model="m", provider="scripted"), ProviderError("boom")])
    with pytest.raises(ProviderError, match="boom"):
        await collect(provider)


async def test_exhaustion_raises_a_clear_error():
    provider = ScriptedProvider(text_response("only one"))
    await collect(provider)
    with pytest.raises(ScriptExhausted) as excinfo:
        await collect(provider)
    assert "exhausted" in str(excinfo.value)
    assert excinfo.value.index == 1
    assert excinfo.value.available == 1


async def test_wait_step_blocks_until_released():
    gate = asyncio.Event()
    provider = ScriptedProvider(
        [
            MessageStart(model="m", provider="scripted"),
            Wait(event=gate),
            TextDelta("after"),
            MessageStop("end_turn"),
        ]
    )
    task = asyncio.create_task(collect(provider))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert task.done() is False

    gate.set()
    events = await asyncio.wait_for(task, timeout=1)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["after"]


async def test_callable_steps_support_sync_and_async_factories():
    async def async_step(req: ModelRequest):
        return TextDelta("async")

    provider = ScriptedProvider(
        [
            lambda req: [TextDelta("sync-a"), TextDelta("sync-b")],
            async_step,
            MessageStop("end_turn"),
        ]
    )
    events = await collect(provider)
    assert [e.text for e in events if isinstance(e, TextDelta)] == [
        "sync-a",
        "sync-b",
        "async",
    ]


async def test_callable_step_can_return_a_single_event():
    provider = ScriptedProvider([lambda req: MessageStart(model="m", provider="scripted")])
    events = await collect(provider)
    assert len(events) == 1
    assert isinstance(events[0], MessageStart)


async def test_count_tokens_returns_none_for_heuristic_fallback():
    provider = ScriptedProvider(text_response("x"))
    assert await provider.count_tokens(request()) is None


async def test_close_is_idempotent_and_rejects_streaming():
    provider = ScriptedProvider(text_response("x"))
    await provider.aclose()
    await provider.aclose()
    assert provider.closed is True
    with pytest.raises(ProviderError, match="closed"):
        await collect(provider)


async def test_empty_script_yields_nothing_but_still_counts_as_a_call():
    provider = ScriptedProvider([])
    assert await collect(provider) == []
    assert provider.calls == 1


def test_scripted_provider_has_no_network_or_clock_dependence():
    path = (
        Path(__file__).resolve().parents[1]
        / "nexus"
        / "model"
        / "providers"
        / "scripted.py"
    )
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    assert not (modules & {"httpx", "requests", "socket", "time", "random", "datetime"})
