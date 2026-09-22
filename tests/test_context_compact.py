"""Phase 3 pure compaction strategies (plan 5.2)."""
from __future__ import annotations

import pytest

from nexus.context.compact import (
    CompactionError,
    MappingNoteResolver,
    SummaryArtifact,
    compact,
    drop_oldest,
    evict_tool_results,
    hybrid,
    summarize,
)
from nexus.model.message import Message, Text, ToolResult, ToolUse


def user(text):
    return Message(role="user", content=[Text(text=text)])


def assistant(text):
    return Message(role="assistant", content=[Text(text=text)])


def tool_exchange(call_id="c1", body="BODY" * 50, is_error=False):
    return [
        Message(
            role="assistant",
            content=[ToolUse(id=call_id, name="Read", input={"path": "a"})],
        ),
        Message(
            role="user",
            content=[
                ToolResult(
                    tool_use_id=call_id, content=[Text(text=body)], is_error=is_error
                )
            ],
        ),
    ]


class RecordingSummarizer:
    def __init__(self, artifact=None):
        self.seen = []
        self.artifact = artifact

    def summarize(self, messages):
        self.seen.append(list(messages))
        if self.artifact is not None:
            return self.artifact
        return SummaryArtifact(
            text="SUMMARY", source_count=len(messages), tokens=7
        )


# ---------------------------------------------------------------------------
# drop_oldest
# ---------------------------------------------------------------------------


def test_drop_oldest_keeps_a_whole_message_suffix():
    messages = [user("a"), assistant("b"), user("c"), assistant("d")]
    result = drop_oldest(messages, keep=2)
    assert result.messages == tuple(messages[-2:])
    assert result.dropped == 2
    assert result.strategy == "drop_oldest"
    action = result.actions[0]
    assert action.dropped_messages == 2
    assert action.retained_messages == 2


def test_drop_oldest_never_mutates_input_and_never_splits_blocks():
    messages = [
        Message(role="assistant", content=[Text(text="x"), Text(text="y")]),
        user("b"),
        user("c"),
    ]
    before = [list(m.content) for m in messages]
    result = drop_oldest(messages, keep=1)
    assert result.messages[-1] == messages[-1]
    assert [list(m.content) for m in messages] == before
    # No partial content: retained messages are identical objects/content.
    assert all(m in messages for m in result.messages)


def test_drop_oldest_keep_larger_than_input_is_a_noop():
    messages = [user("a"), assistant("b")]
    result = drop_oldest(messages, keep=5)
    assert result.messages == tuple(messages)
    assert result.dropped == 0


def test_drop_oldest_negative_keep_rejected():
    with pytest.raises(ValueError):
        drop_oldest([user("a")], keep=-1)


# ---------------------------------------------------------------------------
# evict_tool_results
# ---------------------------------------------------------------------------


def test_evict_replaces_content_retaining_id_error_and_pairing():
    _assistant, result_message = tool_exchange(is_error=False)
    original_block = result_message.content[0]
    messages = [
        Message(
            role="assistant",
            content=[ToolUse(id="c1", name="Read", input={})],
        ),
        result_message,
        user("later"),
        assistant("even later"),
    ]

    result = evict_tool_results(
        messages,
        note_resolver=MappingNoteResolver({"c1": "read a: 3 lines"}),
        keep_recent=0,
    )

    assert result.evicted == 1
    evicted_block = result.messages[1].content[0]
    assert isinstance(evicted_block, ToolResult)
    assert evicted_block.tool_use_id == "c1"
    assert evicted_block.is_error is False
    assert evicted_block.content[0].text == "read a: 3 lines"
    # Original block content untouched.
    assert original_block.content[0].text != "read a: 3 lines"
    assert len(original_block.content[0].text) == 200


def test_evict_skips_errors_and_missing_notes():
    error_message = tool_exchange(call_id="e1", is_error=True)[1]
    ok_message = tool_exchange(call_id="c1")[1]
    messages = [error_message, ok_message, user("tail"), assistant("tail2")]

    result = evict_tool_results(
        messages,
        note_resolver=MappingNoteResolver({"c1": None, "e1": "should not apply"}),
        keep_recent=0,
    )
    assert result.evicted == 0
    assert result.messages == tuple(messages)


def test_evict_respects_keep_recent_and_min_chars():
    message = tool_exchange(call_id="c1", body="x" * 10)[1]
    messages = [message, user("a"), assistant("b")]

    kept = evict_tool_results(
        messages,
        note_resolver={"c1": "note"},
        keep_recent=3,
    )
    assert kept.evicted == 0

    too_short = evict_tool_results(
        messages,
        note_resolver={"c1": "note"},
        keep_recent=0,
        min_content_chars=100,
    )
    assert too_short.evicted == 0

    evicted = evict_tool_results(
        messages,
        note_resolver={"c1": "note"},
        keep_recent=0,
        min_content_chars=5,
    )
    assert evicted.evicted == 1


def test_evict_is_idempotent():
    messages = [
        Message(role="assistant", content=[ToolUse(id="c1", name="Read", input={})]),
        tool_exchange()[1],
        user("a"),
        assistant("b"),
    ]
    first = evict_tool_results(messages, note_resolver={"c1": "note"})
    second = evict_tool_results(first.messages, note_resolver={"c1": "note"})
    assert second.evicted == 0
    assert second.messages == first.messages


def test_evict_without_note_resolver_is_a_noop():
    messages = [tool_exchange()[1], user("a"), assistant("b")]
    result = evict_tool_results(messages, note_resolver=None)
    assert result.evicted == 0


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def test_summarize_requires_a_callback_and_fails_actionably():
    messages = [user("a"), assistant("b"), user("c")]
    with pytest.raises(CompactionError, match="summarizer"):
        summarize(messages, summarizer=None, keep=1)


def test_summarize_pins_one_text_message_and_keeps_the_suffix():
    messages = [user("a"), assistant("b"), user("c"), assistant("d")]
    summarizer = RecordingSummarizer()
    result = summarize(messages, summarizer=summarizer, keep=2)

    assert result.messages[0].role == "user"
    assert "SUMMARY" in result.messages[0].content[0].text
    assert result.messages[1:] == tuple(messages[-2:])
    assert summarizer.seen == [messages[:2]]
    action = result.actions[0]
    assert action.summarized_messages == 2
    assert action.tokens == 7


def test_summarize_empty_summary_is_rejected():
    summarizer = RecordingSummarizer(
        SummaryArtifact(text="   ", source_count=2)
    )
    with pytest.raises(CompactionError, match="empty"):
        summarize([user("a"), assistant("b"), user("c")], summarizer=summarizer, keep=1)


def test_summarize_keep_at_least_length_is_a_noop():
    messages = [user("a"), assistant("b")]
    summarizer = RecordingSummarizer()
    result = summarize(messages, summarizer=summarizer, keep=5)
    assert result.messages == tuple(messages)
    assert summarizer.seen == []


# ---------------------------------------------------------------------------
# hybrid
# ---------------------------------------------------------------------------


def test_hybrid_evicts_then_summarizes_then_drops():
    long_result = tool_exchange(call_id="c1", body="R" * 300)[1]
    messages = [
        Message(role="assistant", content=[ToolUse(id="c1", name="Read", input={})]),
        long_result,
        user("middle"),
        assistant("answer"),
        user("current"),
    ]
    summarizer = RecordingSummarizer()
    result = hybrid(
        messages,
        note_resolver={"c1": "short note"},
        summarizer=summarizer,
        keep=2,
        keep_recent=0,
    )
    assert result.strategy == "hybrid"
    assert result.evicted == 1
    assert "SUMMARY" in result.messages[0].content[0].text
    assert result.messages[-1] == messages[-1]


def test_hybrid_without_summarizer_falls_back_to_dropping():
    messages = [user("a"), assistant("b"), user("c"), assistant("d")]
    result = hybrid(messages, keep=2)
    assert result.strategy == "hybrid"
    assert result.messages == tuple(messages[-2:])
    assert isinstance(result.actions[-1], type(drop_oldest(messages, keep=2).actions[0]))


# ---------------------------------------------------------------------------
# dispatch and original-immutability across every strategy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["drop_oldest", "evict_tool_results", "hybrid"])
def test_every_strategy_leaves_originals_untouched(strategy):
    messages = [
        Message(role="assistant", content=[ToolUse(id="c1", name="Read", input={})]),
        tool_exchange(call_id="c1", body="B" * 100)[1],
        user("middle"),
        assistant("answer"),
        user("current"),
    ]
    snapshot = [
        (message.role, tuple(repr(block) for block in message.content))
        for message in messages
    ]
    result = compact(
        messages,
        strategy,
        note_resolver=MappingNoteResolver({"c1": "note"}),
        keep=2,
        keep_recent=2,
    )
    after = [
        (message.role, tuple(repr(block) for block in message.content))
        for message in messages
    ]
    assert after == snapshot
    assert isinstance(result.messages, tuple)


def test_compact_rejects_an_unknown_strategy():
    with pytest.raises(ValueError, match="unknown"):
        compact([user("a")], "nonsense")


def test_compaction_metadata_is_content_free():
    messages = [user("CONTENT-A"), assistant("CONTENT-B"), user("CONTENT-C")]
    result = drop_oldest(messages, keep=1)
    metadata = result.metadata()
    assert metadata["strategy"] == "drop_oldest"
    assert metadata["dropped"] == 2
    assert "CONTENT-A" not in str(metadata)
    assert "CONTENT-B" not in str(metadata)


# ---------------------------------------------------------------------------
# Determinism and evict-before-drop ordering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["drop_oldest", "evict_tool_results", "summarize", "hybrid"])
def test_every_strategy_is_deterministic(strategy):
    messages = [
        Message(role="assistant", content=[ToolUse(id="c1", name="Read", input={})]),
        tool_exchange(call_id="c1", body="B" * 200)[1],
        user("middle"),
        assistant("answer"),
        user("current"),
    ]
    kwargs = {
        "note_resolver": MappingNoteResolver({"c1": "note"}),
        "summarizer": RecordingSummarizer(),
        "keep": 2,
        "keep_recent": 0,
    }
    first = compact(messages, strategy, **kwargs)
    second = compact(messages, strategy, **kwargs)
    assert first.messages == second.messages
    assert first.metadata() == second.metadata()


def test_hybrid_evicts_before_dropping():
    # Every message in the dropped prefix that carries an evictable result has
    # its content replaced by the note before the prefix is summarized/dropped.
    messages = [
        Message(role="assistant", content=[ToolUse(id="c1", name="Read", input={})]),
        tool_exchange(call_id="c1", body="B" * 300)[1],
        Message(role="assistant", content=[ToolUse(id="c2", name="Grep", input={})]),
        tool_exchange(call_id="c2", body="C" * 300)[1],
        user("middle"),
        assistant("answer"),
        user("current"),
    ]
    result = hybrid(
        messages,
        note_resolver={"c1": "note-1", "c2": "note-2"},
        summarizer=RecordingSummarizer(),
        keep=1,
        keep_recent=0,
    )
    # The kept suffix is one message; the summary pins the prefix. Every note
    # was applied to the drop candidate before it was condensed.
    assert result.evicted == 2
    assert result.dropped > 0
    action_types = [type(a).__name__ for a in result.actions]
    assert "EvictAction" in action_types
    assert action_types.index("EvictAction") < action_types.index("SummarizeAction")
