"""Phase 3 durable tool-result notes and eviction (exit criterion 1).

The durable note lives on the persisted IR ``ToolResult.context_note``. Context
eviction reads it directly from the persisted block (with an external resolver
only as a compatibility fallback), replaces **content** only in an assembled
copy, and preserves the block's id and error flag. The durable log is never
mutated.
"""
from __future__ import annotations

from nexus.context.compact import MappingNoteResolver, evict_tool_results
from nexus.model.message import Message, Text, ToolResult, ToolUse
from nexus.tools.manager import PreparedBatch
from nexus.tools.spec import ToolCall, ToolExecutionResult, ToolSpec


def _assistant_tool_use(call_id="c1"):
    return Message(
        role="assistant", content=[ToolUse(id=call_id, name="Read", input={"path": "a"})]
    )


def _result_message(call_id="c1", body="B" * 400, note=None, is_error=False):
    return Message(
        role="user",
        content=[
            ToolResult(
                tool_use_id=call_id,
                content=[Text(text=body)],
                is_error=is_error,
                context_note=note,
            )
        ],
    )


# ---------------------------------------------------------------------------
# The IR carries the note and adapters do not send it
# ---------------------------------------------------------------------------


def test_tool_result_note_round_trips_through_json():
    import msgspec

    block = ToolResult(
        tool_use_id="c1",
        content=[Text(text="body")],
        context_note="[Read: 10 lines; re-run Read]",
    )
    encoded = msgspec.json.encode(block)
    decoded = msgspec.json.decode(encoded, type=ToolResult)
    assert decoded == block
    assert decoded.context_note == "[Read: 10 lines; re-run Read]"
    # A block persisted before the field existed decodes to ``None``.
    legacy = msgspec.json.decode(
        msgspec.json.encode(
            ToolResult(tool_use_id="c1", content=[Text(text="body")])
        ),
        type=ToolResult,
    )
    assert legacy.context_note is None


def test_execution_result_to_tool_result_carries_note():
    executed = ToolExecutionResult.text("BODY", context_note="[note]")
    block = executed.to_tool_result("call-1")
    assert block.context_note == "[note]"
    assert block.tool_use_id == "call-1"
    assert block.is_error is False


def test_batch_conversion_carries_notes():
    from nexus.tools.manager import PreparedCall

    spec = ToolSpec(
        name="Read",
        description="read",
        input_schema={"type": "object"},
        bundle="fs",
    )
    entry = PreparedCall(call=ToolCall(id="c1", name="Read"), spec=spec)
    batch = PreparedBatch((entry,))
    results = batch.to_ir_results(
        [ToolExecutionResult.text("BODY", context_note="[note]")]
    )
    assert results[0].context_note == "[note]"
    assert results[0].tool_use_id == "c1"


def test_anthropic_adapter_does_not_send_context_note():
    from nexus.model.providers.anthropic import _content_to_wire

    wire = _content_to_wire(
        [
            ToolResult(
                tool_use_id="c1",
                content=[Text(text="body")],
                context_note="HARNESS-ONLY-NOTE",
            )
        ]
    )
    assert wire == [
        {
            "type": "tool_result",
            "tool_use_id": "c1",
            "content": [{"type": "text", "text": "body"}],
            "is_error": False,
        }
    ]
    assert "HARNESS-ONLY-NOTE" not in str(wire)


# ---------------------------------------------------------------------------
# Eviction reads the persisted note; no resolver required
# ---------------------------------------------------------------------------


def test_eviction_reads_the_persisted_note_without_a_resolver():
    messages = [
        _assistant_tool_use("c1"),
        _result_message("c1", body="R" * 500, note="[Read a: 5 lines; re-run Read]"),
        Message(role="user", content=[Text(text="tail")]),
        Message(role="assistant", content=[Text(text="tail2")]),
    ]
    original_block = messages[1].content[0]

    result = evict_tool_results(messages, note_resolver=None, keep_recent=0)

    assert result.evicted == 1
    evicted = result.messages[1].content[0]
    assert isinstance(evicted, ToolResult)
    assert evicted.tool_use_id == "c1"
    assert evicted.is_error is False
    assert evicted.content[0].text == "[Read a: 5 lines; re-run Read]"
    # The assembled copy keeps the note; the original log block is untouched.
    assert evicted.context_note == "[Read a: 5 lines; re-run Read]"
    assert original_block.content[0].text == "R" * 500


def test_persisted_note_wins_over_external_resolver():
    messages = [
        _assistant_tool_use("c1"),
        _result_message("c1", body="R" * 500, note="[persisted]"),
        Message(role="user", content=[Text(text="tail")]),
    ]
    result = evict_tool_results(
        messages,
        note_resolver=MappingNoteResolver({"c1": "[external]"}),
        keep_recent=0,
    )
    assert result.messages[1].content[0].content[0].text == "[persisted]"


def test_eviction_preserves_pairing_id_error_and_idempotence():
    messages = [
        _assistant_tool_use("c1"),
        _result_message("c1", body="R" * 500, note="[note]", is_error=True),
        _assistant_tool_use("c2"),
        _result_message("c2", body="S" * 500, note="[note2]"),
        Message(role="user", content=[Text(text="tail")]),
    ]
    first = evict_tool_results(messages, note_resolver=None, keep_recent=0)
    second = evict_tool_results(first.messages, note_resolver=None, keep_recent=0)

    assert first.evicted == 1  # only the non-error result is eligible
    assert second.evicted == 0
    assert second.messages == first.messages
    # The error result keeps its content (never evicted) and its flag.
    error_block = next(
        block
        for message in first.messages
        for block in message.content
        if isinstance(block, ToolResult) and block.tool_use_id == "c1"
    )
    assert error_block.is_error is True
    assert error_block.content[0].text == "R" * 500


def test_builtin_style_notes_are_re_runnable_text():
    # A representative built-in note must be actionable, not empty.
    messages = [
        _assistant_tool_use("c1"),
        _result_message(
            "c1", body="x" * 200, note="[Grep 'x': 12 match(es); re-run Grep to see them]"
        ),
        Message(role="user", content=[Text(text="tail")]),
    ]
    result = evict_tool_results(messages, note_resolver=None, keep_recent=0)
    replacement = result.messages[1].content[0].content[0].text
    assert "re-run" in replacement
