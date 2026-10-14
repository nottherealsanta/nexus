"""GPT Live wire shapes: parsing is total, appends are chunked on code points (notes sections 3-4)."""
from __future__ import annotations

import json

import pytest

from nexus.live import protocol as lp


def frame(**kw) -> str:
    return json.dumps(kw)


def test_session_payload_matches_the_observed_shape():
    request = lp.CallRequest("v=0\r\n", lp.LiveSession(instructions="Be brief", voice="cove"))
    assert request.to_payload() == {
        "sdp": "v=0\r\n",
        "session": {
            "model": "gpt-live-1-codex",
            "instructions": "Be brief",
            "audio": {"output": {"voice": "cove"}},
            "delegation": {"type": "client"},
        },
    }
    assert "v=0" not in repr(request)  # the SDP never prints


def test_sideband_reconnected_is_a_synthetic_exported_event():
    assert lp.SidebandReconnected().gap_s == 0.0 and lp.SidebandReconnected(gap_s=1.5).gap_s == 1.5
    assert "SidebandReconnected" in lp.__all__
    assert lp.SidebandReconnected in lp.LiveEvent.__args__
    assert isinstance(lp.parse_event(frame(type="sideband.reconnected")), lp.Unknown)  # never a wire event


def test_resolve_voice():
    assert lp.resolve_voice("") == lp.DEFAULT_VOICE == lp.resolve_voice("default")
    assert lp.resolve_voice("  COVE ") == "cove"
    with pytest.raises(ValueError):
        lp.resolve_voice("robot")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (frame(type="session.started", session={"id": "s1"}), lp.SessionStarted("s1")),
        (frame(type="session.updated", session={"id": "s1"}), lp.SessionUpdated("s1")),
        (frame(type="session.closed", reason="bye"), lp.SessionClosed("bye")),
        (frame(type="input_transcript.added", item={"text": "hi"}), lp.TranscriptAdded("user", "hi")),
        (frame(type="output_transcript.added", item={"text": "yo"}), lp.TranscriptAdded("assistant", "yo")),
        (frame(type="session.input_transcript.delta", delta="h"), lp.TranscriptDelta("user", "h")),
        (frame(type="session.output_transcript.delta", delta="y"), lp.TranscriptDelta("assistant", "y")),
        (frame(type="turn.done", turn={"role": "assistant", "transcript": "done"}), lp.TurnDone("assistant", "done")),
    ],
)
def test_known_events(raw, expected):
    assert lp.parse_event(raw) == expected


def test_delegation_created_joins_parts_with_newlines():
    event = lp.parse_event(frame(type="delegation.created", item={
        "type": "delegation", "target": "client", "id": "d1",
        "content": [{"type": "input_text", "text": "read"}, {"type": "input_text", "text": "README"}, {"type": "other"}],
    }))
    assert event == lp.DelegationCreated("d1", "read\nREADME")


@pytest.mark.parametrize("item", [
    {"type": "delegation", "target": "server", "id": "d", "content": []},
    {"type": "delegation", "target": "client", "id": "", "content": []},
    {"type": "delegation", "target": "client", "id": "d", "content": "text"},
    "nope",
])
def test_invalid_delegation_is_unknown(item):
    assert isinstance(lp.parse_event(frame(type="delegation.created", item=item)), lp.Unknown)


def test_error_event_message_order_and_fatal_flag():
    assert lp.parse_event(frame(type="error", message="top", error={"message": "inner"})) == lp.ErrorEvent("top")
    assert lp.parse_event(frame(type="error", error={"message": "inner"})).message == "inner"
    assert lp.parse_event(frame(type="error", error="plain")).message == "plain"
    assert lp.parse_event(frame(type="error")).message == "GPT Live error"
    assert lp.parse_event(frame(type="error", status=401, message="x")).fatal is True
    assert lp.parse_event(frame(type="error", error={"code": "Token_Expired", "message": "x"})).fatal is True
    assert lp.parse_event(frame(type="error", message="x")).fatal is False


@pytest.mark.parametrize("raw", [
    "", "not json", "[]", "42", "null", frame(), frame(type=5), frame(type="future.thing", a=1),
    frame(type="turn.done", turn={"role": "system", "transcript": "x"}), frame(type="turn.done"),
    b"\xff\xfe", "x" * (lp.MAX_EVENT_BYTES + 1),
])
def test_parse_event_never_raises_and_falls_back_to_unknown(raw):
    assert isinstance(lp.parse_event(raw), lp.Unknown)


def test_unknown_keeps_the_raw_type():
    assert lp.parse_event(frame(type="output_audio.delta", audio="AAAA")) == lp.Unknown("output_audio.delta")


@pytest.mark.parametrize("text", [
    "plain ascii " * 100,
    "é" * 400,                    # 2 bytes each
    "日本語のテキスト" * 80,       # 3 bytes each
    "🙂" * 300,                   # 4 bytes each
    "a" + "🙂" * 200 + "é" * 7,
    "é" * 300,              # combining sequences may split; code points never do
])
def test_chunk_utf8_bounds_and_roundtrip(text):
    chunks = lp.chunk_utf8(text)
    assert "".join(chunks) == text
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= 500
        chunk.encode("utf-8")  # no split surrogates / code points
    # greedy: every chunk but the last cannot take the next code point
    for chunk, nxt in zip(chunks, chunks[1:]):
        assert len(chunk.encode()) + len(nxt[0].encode()) > 500


def test_chunk_utf8_edges():
    assert lp.chunk_utf8("") == [""]
    assert lp.chunk_utf8("a" * 500) == ["a" * 500]
    assert lp.chunk_utf8("a" * 501) == ["a" * 500, "a"]
    assert lp.chunk_utf8("a" * 499 + "é") == ["a" * 499, "é"]
    with pytest.raises(ValueError):
        lp.chunk_utf8("x", 3)


def test_append_messages_shapes():
    plain = lp.append_message("hello", channel="speakable")
    assert plain == {"type": "session.context.append", "channel": "speakable",
                     "content": [{"type": "input_text", "text": "hello"}]}
    assert "channel" not in lp.append_message("x")
    delegated = lp.append_message("p", channel="commentary", delegation_id="d1")
    assert delegated["type"] == "delegation.context.append"
    assert delegated["delegation_item_id"] == "d1" and delegated["channel"] == "commentary"
    with pytest.raises(ValueError):
        lp.append_message("x", channel="loud")
    many = lp.append_messages("é" * 600, delegation_id="d")
    assert len(many) == 3 and all(m["delegation_item_id"] == "d" for m in many)
    assert "".join(m["content"][0]["text"] for m in many) == "é" * 600


def test_final_message_prefix_is_raw_not_escaped():
    text = lp.final_message("<b>a & b</b>")
    assert text == '"Agent Final Message":\n\n<b>a & b</b>'


def test_close_message_and_call_ids():
    assert lp.close_message() == {"type": "session.close"}
    assert lp.valid_call_id("rtc_AbC-123")
    assert lp.valid_call_id("123e4567-e89b-12d3-a456-426614174000")
    assert not lp.valid_call_id("calls") and not lp.valid_call_id("")
