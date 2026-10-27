from nexus.llmhub.providers import PROVIDERS
from nexus.llmhub.responses import ResponsesQuirks, _State, build_body, events_for
from nexus.llmhub.types import (
    Done,
    Message,
    PartDone,
    Reasoning,
    ReasoningDelta,
    Request,
    Text,
    TextDelta,
    ToolArgsDelta,
    ToolCall,
    ToolCallStarted,
    ToolResult,
    ToolSpec,
    Usage,
)

REASONING_ITEM = {"id": "rs_1", "type": "reasoning", "summary": [{"type": "summary_text", "text": "Plan."}], "encrypted_content": "gAAA"}
CALL_ITEM = {"id": "fc_1", "type": "function_call", "status": "completed", "call_id": "call_1", "name": "bash", "arguments": '{"command":"ls"}'}

STREAM = [
    ("response.created", {"type": "response.created", "response": {"id": "resp_1"}}),
    ("response.output_item.added", {"type": "response.output_item.added", "item": {"id": "rs_1", "type": "reasoning"}}),
    ("response.reasoning_summary_part.added", {"type": "response.reasoning_summary_part.added"}),
    ("response.reasoning_summary_text.delta", {"type": "response.reasoning_summary_text.delta", "delta": "Plan."}),
    ("response.output_item.done", {"type": "response.output_item.done", "item": REASONING_ITEM}),
    ("response.output_item.added", {"type": "response.output_item.added", "item": {**CALL_ITEM, "arguments": ""}}),
    ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"command":'}),
    ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '"ls"}'}),
    ("response.output_item.done", {"type": "response.output_item.done", "item": CALL_ITEM}),
    ("response.completed", {"type": "response.completed", "response": {"id": "resp_1", "usage": {
        "input_tokens": 100, "input_tokens_details": {"cached_tokens": 80},
        "output_tokens": 20, "output_tokens_details": {"reasoning_tokens": 7}}}}),
]


CODEX = PROVIDERS["codex"].quirks
GROK = PROVIDERS["grok"].quirks


def _normalize(stream, origin="codex"):
    state = _State()
    return [event for name, data in stream for event in events_for(name, data, state, origin=origin)]


def test_stream_normalizes_to_deltas_parts_usage_done():
    events = _normalize(STREAM)
    assert ReasoningDelta("Plan.") in events
    assert ToolCallStarted("call_1", "bash") in events
    assert [e.delta for e in events if isinstance(e, ToolArgsDelta)] == ['{"command":', '"ls"}']
    assert all(e.id == "call_1" for e in events if isinstance(e, ToolArgsDelta))
    parts = [e.part for e in events if isinstance(e, PartDone)]
    assert parts == [Reasoning("Plan.", origin="codex", raw=REASONING_ITEM), ToolCall("call_1", "bash", '{"command":"ls"}', origin="codex", raw=CALL_ITEM)]
    assert Usage(100, 80, 20, 7) in events
    assert events[-1] == Done("tool_calls", "resp_1")


def test_text_and_incomplete():
    state = _State()
    assert events_for("x", {"type": "response.output_text.delta", "delta": "hi"}, state, origin="codex") == [TextDelta("hi")]
    out = events_for("x", {"type": "response.incomplete", "response": {"id": "r", "incomplete_details": {"reason": "max_output_tokens"}}}, state, origin="codex")
    assert out[-1].stop == "max_tokens"


def _request(parts):
    return Request(
        model="m",
        instructions="be brief",
        messages=(
            Message("user", (Text("list files"),)),
            Message("assistant", tuple(parts)),
            Message("tool", (ToolResult("call_1", "a\nb"),)),
            Message("assistant", (Text("from another provider"), Reasoning("foreign"))),
        ),
        tools=(ToolSpec("bash", "run", {"type": "object"}),),
        reasoning_effort="low",
        conversation_id="conv",
    )


def test_codex_replays_own_items_verbatim_and_translates_others():
    parts = [e.part for e in _normalize(STREAM) if isinstance(e, PartDone)]
    body = build_body(_request(parts), origin="codex", quirks=CODEX)
    assert body["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list files"}]},
        REASONING_ITEM,
        CALL_ITEM,
        {"type": "function_call_output", "call_id": "call_1", "output": "a\nb"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "from another provider"}]},
    ]
    assert body["store"] is False and body["stream"] is True
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["reasoning"] == {"summary": "auto", "effort": "low"}
    assert body["prompt_cache_key"] == "conv"
    assert body["tools"][0]["name"] == "bash"


def test_grok_rebuilds_items_and_drops_reasoning_without_encrypted_content():
    parts = [e.part for e in _normalize(STREAM, origin="grok") if isinstance(e, PartDone)]
    body = build_body(_request(parts), origin="grok", quirks=GROK)
    assert body["input"][1] == {"type": "function_call", "call_id": "call_1", "name": "bash", "arguments": '{"command":"ls"}'}
    assert not any(item.get("type") == "reasoning" for item in body["input"])
    assert "include" not in body and "prompt_cache_key" not in body


def test_foreign_items_are_never_replayed_raw():
    parts = [e.part for e in _normalize(STREAM, origin="grok") if isinstance(e, PartDone)]
    body = build_body(_request(parts), origin="codex", quirks=ResponsesQuirks(encrypted_reasoning=True, replay_raw=True))
    assert body["input"][1] == {"type": "function_call", "call_id": "call_1", "name": "bash", "arguments": '{"command":"ls"}'}
    assert not any(item.get("type") == "reasoning" for item in body["input"])
