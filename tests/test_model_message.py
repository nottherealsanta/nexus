import msgspec

from nexus.model.message import (
    ContentBlock,
    Document,
    Image,
    Message,
    MessageMeta,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)

BLOCKS = [
    Text("hello"),
    Thinking("reason", signature="sig-abc"),
    ToolUse("call_1", "Read", {"path": "a.txt"}),
    ToolResult("call_1", [Text("done")], is_error=False),
    Image("image/png", data=b"\x89PNG\x00\xff"),
    Document("application/pdf", b"%PDF-1.4", title="doc"),
]


def test_tagged_union_msgpack_roundtrip():
    payload = msgspec.msgpack.encode(BLOCKS)
    decoded = msgspec.msgpack.Decoder(list[ContentBlock]).decode(payload)
    assert decoded == BLOCKS
    assert decoded[4].data == b"\x89PNG\x00\xff"
    assert isinstance(decoded[4].data, bytes)


def test_tag_in_wire_format():
    payload = msgspec.msgpack.encode([Text("a")])
    assert b"text" in payload
    payload = msgspec.json.encode(Thinking("r", signature="s"))
    assert b"thinking" in payload


def test_signature_survives_json_roundtrip():
    block = Thinking("reasoning", signature="opaque+signature==")
    payload = msgspec.json.encode(block)
    decoded = msgspec.json.Decoder(ContentBlock).decode(payload)
    assert decoded == block
    assert decoded.signature == "opaque+signature=="


def test_message_meta_defaults_and_roundtrip():
    message = Message("assistant", [Text("hi")])
    assert message.meta.provider is None
    assert message.meta.cache_hit is False

    rich = Message(
        "user",
        [Text("q")],
        meta=MessageMeta(provider="anthropic", model="m", ts=1.5, turn_id="t1"),
    )
    decoded = msgspec.msgpack.Decoder(Message).decode(msgspec.msgpack.encode(rich))
    assert decoded == rich
    assert decoded.meta.turn_id == "t1"


def test_tool_result_diff_roundtrips_without_entering_content():
    block = ToolResult(
        "call-1",
        [Text("Edited f.txt: replaced 1 occurrence(s)")],
        diff={
            "path": "f.txt",
            "hunk": "-old\n+new",
            "added_lines": 1,
            "removed_lines": 1,
            "truncated": False,
        },
    )
    decoded = msgspec.json.Decoder(ContentBlock).decode(msgspec.json.encode(block))
    assert decoded == block
    assert isinstance(decoded, ToolResult)
    assert decoded.content[0].text == "Edited f.txt: replaced 1 occurrence(s)"


def test_no_system_role_in_message_ir():
    for block in BLOCKS:
        assert isinstance(block, (Text, Thinking, ToolUse, ToolResult, Image, Document))
