from nexus.llmhub.chat import _State, build_body, events_for_chunk, finish
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

CHUNKS = [
    {"id": "c1", "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "Think."}}]},
    {"id": "c1", "choices": [{"index": 0, "delta": {"content": "Running."}}]},
    {"id": "c1", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "bash", "arguments": ""}}]}}]},
    {"id": "c1", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"command":"ls"}'}}]}}]},
    {"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    {"id": "c1", "choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 40}}},
]


def _run(origin="opencode"):
    state = _State()
    events = [e for chunk in CHUNKS for e in events_for_chunk(chunk, state, origin=origin)]
    return events + finish(state, origin=origin)


def test_chunks_normalize_to_deltas_then_parts():
    events = _run()
    assert events[:5] == [ReasoningDelta("Think."), TextDelta("Running."), ToolCallStarted("call_a", "bash"),
                          ToolArgsDelta("call_a", '{"command":"ls"}'), PartDone(Reasoning("Think.", origin="opencode", raw={"field": "reasoning_content"}))]
    parts = [e.part for e in events if isinstance(e, PartDone)]
    assert parts[1:] == [Text("Running.", origin="opencode"), ToolCall("call_a", "bash", '{"command":"ls"}', origin="opencode")]
    assert Usage(50, 40, 9, 0) in events
    assert events[-1] == Done("tool_calls", "c1")


def test_body_puts_own_reasoning_back_under_its_field():
    parts = tuple(e.part for e in _run() if isinstance(e, PartDone))
    req = Request(
        model="kimi", instructions="sys",
        messages=(Message("user", (Text("go"),)), Message("assistant", parts), Message("tool", (ToolResult("call_a", "x"),))),
        tools=(ToolSpec("bash", "run", {"type": "object"}),), reasoning_effort="high",
    )
    body = build_body(req, origin="opencode")
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "Running.", "reasoning_content": "Think.",
         "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "bash", "arguments": '{"command":"ls"}'}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "x"},
    ]
    assert body["reasoning_effort"] == "high" and body["stream_options"] == {"include_usage": True}
    assert "reasoning_content" not in build_body(req, origin="copilot")["messages"][2]


def test_length_finish_is_max_tokens():
    state = _State()
    events_for_chunk({"choices": [{"index": 0, "delta": {"content": "a"}, "finish_reason": "length"}]}, state, origin="copilot")
    assert finish(state, origin="copilot")[-1].stop == "max_tokens"
