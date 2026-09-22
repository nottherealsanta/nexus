import msgspec

from nexus.model.message import Message, MessageMeta, Text, ToolUse
from nexus.model.request import ModelRequest, SamplingParams, ToolSchema


def _sample_request() -> ModelRequest:
    return ModelRequest(
        messages=[
            Message("user", [Text("hi")]),
            Message("assistant", [ToolUse("c1", "Read", {"p": "a"})], meta=MessageMeta(provider="x")),
        ],
        system="sys",
        tools=[ToolSchema("Read", "read a file", {"type": "object"})],
        params=SamplingParams(temperature=0.2, max_output_tokens=10),
        model="m",
        provider="p",
        metadata={"k": "v"},
    )


def test_request_msgpack_roundtrip():
    request = _sample_request()
    decoded = msgspec.msgpack.Decoder(ModelRequest).decode(msgspec.msgpack.encode(request))
    assert decoded == request


def test_request_defaults():
    request = ModelRequest(messages=[])
    assert request.system is None
    assert request.tools == []
    assert request.params == SamplingParams()
    assert request.metadata == {}


def test_contracts_are_frozen():
    schema = ToolSchema("Read", "read", {"type": "object"})
    try:
        schema.name = "Write"
        assert False
    except AttributeError:
        pass
    request = ModelRequest(messages=[])
    try:
        request.system = "x"
        assert False
    except AttributeError:
        pass
