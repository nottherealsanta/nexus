import pytest

from nexus.codec import message_from_json, message_to_json
from nexus.llmhub.chat import chat_messages
from nexus.llmhub.responses import ResponsesQuirks, input_items
from nexus.llmhub.types import Message, Request, Text
from nexus.pastes import paste_parts

MARKER = "[Pasted ~3 tokens]"


def test_pastes_split_the_prompt_at_their_markers():
    parts = paste_parts(f"Summarise this: {MARKER} and then compare", [
        {"marker": MARKER, "text": "one\ntwo\nthree"},
    ])
    assert parts == (
        Text("Summarise this: "),
        Text("one\ntwo\nthree", collapsed=True),
        Text(" and then compare"),
    )


def test_markers_are_placed_in_text_order():
    parts = paste_parts(f"a {MARKER} b [Pasted ~1 tokens #2] c", [
        {"marker": "[Pasted ~1 tokens #2]", "text": "second"},
        {"marker": MARKER, "text": "first"},
    ])
    assert parts == (
        Text("a "),
        Text("first", collapsed=True),
        Text(" b "),
        Text("second", collapsed=True),
        Text(" c"),
    )


def test_collapsed_text_round_trips_and_keeps_the_full_text_for_providers():
    message = Message("user", (
        Text("Summarise: "),
        Text("one\ntwo\nthree", collapsed=True),
    ))
    encoded = message_to_json(message)
    assert encoded["parts"][1] == {
        "type": "text", "text": "one\ntwo\nthree", "origin": None, "raw": None, "collapsed": True,
    }
    assert "collapsed" not in message_to_json(Message("user", (Text("plain"),)))["parts"][0]
    assert message_from_json(encoded) == message
    assert chat_messages(Request("m", "", (message,)), origin="copilot") == [{
        "role": "user", "content": "Summarise: one\ntwo\nthree",
    }]
    assert input_items([message], origin="codex", quirks=ResponsesQuirks()) == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Summarise: "}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "one\ntwo\nthree"}]},
    ]


@pytest.mark.parametrize("pastes", [
    {},
    [None],
    [{"marker": "", "text": "x"}],
    [{"marker": MARKER, "text": ""}],
    [{"marker": MARKER, "text": "x"}] * 2,
    [{"marker": "[Pasted ~1 tokens]", "text": "x"}],
    [{"marker": f"[Pasted ~{i} tokens]", "text": "x"} for i in range(9)],
])
def test_invalid_pastes_are_rejected(pastes):
    with pytest.raises(ValueError):
        paste_parts("no marker here", pastes)
