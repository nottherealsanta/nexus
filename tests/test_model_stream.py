import msgspec

from nexus.errors import MalformedToolCall
from nexus.model.stream import (
    MessageStart,
    MessageStop,
    Raw,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ThinkingEnd,
    ToolCallAccumulator,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

ALL_EVENTS = [
    MessageStart(model="m", provider="p"),
    TextDelta("x"),
    ThinkingDelta("t"),
    ThinkingEnd("sig"),
    ToolCallStart("1", "T"),
    ToolCallDelta("1", '{"a"'),
    ToolCallEnd("1", {"a": 1}),
    Usage(input=1, output=2, cache_read=3, cache_write=4, reasoning=5),
    MessageStop("end_turn"),
    Raw({"z": 1}),
]


def test_stream_event_union_roundtrip():
    decoder = msgspec.msgpack.Decoder(StreamEvent)
    for event in ALL_EVENTS:
        assert decoder.decode(msgspec.msgpack.encode(event)) == event


def test_accumulator_single_call():
    acc = ToolCallAccumulator()
    assert acc.handle(ToolCallStart("c1", "Read")) is None
    acc.handle(ToolCallDelta("c1", '{"pa'))
    acc.handle(ToolCallDelta("c1", 'th": "a.txt"}'))
    end = acc.handle(ToolCallEnd("c1", {}))
    assert end == ToolCallEnd(id="c1", input={"path": "a.txt"})


def test_accumulator_supports_interleaved_calls():
    acc = ToolCallAccumulator()
    acc.handle(ToolCallStart("a", "A"))
    acc.handle(ToolCallStart("b", "B"))
    acc.handle(ToolCallDelta("a", '{"x":'))
    acc.handle(ToolCallDelta("b", '{"y":'))
    acc.handle(ToolCallDelta("a", "1}"))
    acc.handle(ToolCallDelta("b", "2}"))
    assert acc.pending == ["a", "b"]
    assert acc.handle(ToolCallEnd("a", {})).input == {"x": 1}
    assert acc.handle(ToolCallEnd("b", {})).input == {"y": 2}
    assert acc.pending == []


def test_accumulator_raises_typed_malformed_error():
    acc = ToolCallAccumulator()
    acc.start("c", "T")
    acc.delta("c", "{not json")
    try:
        acc.finish("c")
        assert False
    except MalformedToolCall as exc:
        assert exc.tool_call_id == "c"
        assert exc.raw == "{not json"


def test_accumulator_rejects_non_object_json():
    acc = ToolCallAccumulator()
    acc.start("c", "T")
    acc.delta("c", "[1, 2]")
    try:
        acc.finish("c")
        assert False
    except MalformedToolCall:
        pass


def test_accumulator_rejects_delta_before_start():
    acc = ToolCallAccumulator()
    try:
        acc.delta("missing", "{}")
        assert False
    except MalformedToolCall:
        pass


def test_accumulator_falls_back_to_provided_input():
    acc = ToolCallAccumulator()
    assert acc.finish("missing", {"pre": 1}) == {"pre": 1}
    assert acc.finish("missing2") == {}


def test_accumulator_falls_back_to_authoritative_input_on_malformed():
    acc = ToolCallAccumulator()
    acc.start("c", "T")
    acc.delta("c", '{"partial":')
    assert acc.finish("c", {"complete": True}) == {"complete": True}


def test_accumulator_falls_back_to_authoritative_input_on_non_object():
    acc = ToolCallAccumulator()
    acc.start("c", "T")
    acc.delta("c", "[1, 2]")
    assert acc.finish("c", {"complete": True}) == {"complete": True}


def test_accumulator_does_not_mask_malformed_without_authority():
    acc = ToolCallAccumulator()
    acc.start("c", "T")
    acc.delta("c", '{"partial":')
    try:
        acc.finish("c")
        assert False
    except MalformedToolCall as exc:
        assert exc.raw == '{"partial":'
