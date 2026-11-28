import pytest

from nexus.llmhub.anthropic import MessagesState
from nexus.llmhub.types import Done, ProviderError, Usage


def start(state):
    state.consume({"type": "message_start", "message": {"id": "msg", "usage": {"input_tokens": 10}}})


def block(index=0):
    return {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": "hello"}}


def test_usage_only_delta_does_not_erase_stop_and_usage_fields():
    state = MessagesState("claude-direct")
    start(state)
    state.consume({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}})
    state.consume({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 3}})
    assert state.consume({"type": "message_stop"}) == [Usage(10, 0, 3), Done("end_turn", "msg")]


def test_duplicate_message_start_does_not_overwrite_identity():
    state = MessagesState("claude-direct")
    start(state)
    with pytest.raises(ProviderError, match="duplicate message_start"):
        start(state)
    assert state.response_id == "msg"


def test_reused_closed_block_index_fails_instead_of_replacing_context():
    state = MessagesState("claude-direct")
    state.consume(block())
    state.consume({"type": "content_block_stop", "index": 0})
    with pytest.raises(ProviderError, match="duplicate content block"):
        state.consume(block())


@pytest.mark.parametrize("index", [-1, True, "0", 1.5])
def test_invalid_block_indices_are_rejected(index):
    with pytest.raises(ProviderError, match="invalid content block index"):
        MessagesState("claude-direct").consume(block(index))


@pytest.mark.parametrize("kind", ["content_block_delta", "content_block_stop"])
def test_events_for_unknown_blocks_fail_explicitly(kind):
    with pytest.raises(ProviderError, match="unknown content block"):
        MessagesState("claude-direct").consume({"type": kind, "index": 0})


@pytest.mark.parametrize("value", [-1, True, "1", 1.5])
def test_bad_usage_is_not_accounted(value):
    state = MessagesState("claude-direct")
    with pytest.raises(ProviderError, match="malformed Messages usage"):
        state.consume({"type": "message_delta", "delta": {}, "usage": {"output_tokens": value}})


def test_conflicting_stops_fail_instead_of_overwriting():
    state = MessagesState("claude-direct")
    state.consume({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
    with pytest.raises(ProviderError, match="conflicting stop reasons"):
        state.consume({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})


def test_initial_nonempty_content_is_not_silently_discarded():
    with pytest.raises(ProviderError, match="unexpected initial message content"):
        MessagesState("claude-direct").consume({"type": "message_start", "message": {"id": "msg", "content": [{"type": "text", "text": "hidden"}]}})


@pytest.mark.parametrize("reason", ["refusal", "sensitive", "pause_turn", "compaction", "future_reason"])
def test_nonstandard_stops_retain_exact_reason(reason):
    state = MessagesState("claude-direct")
    start(state)
    state.consume({"type": "message_delta", "delta": {"stop_reason": reason}})
    assert state.consume({"type": "message_stop"})[-1] == Done("other", "msg", reason)


def test_missing_message_start_cannot_produce_success():
    state = MessagesState("claude-direct")
    state.consume({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
    with pytest.raises(ProviderError, match="incomplete Messages response"):
        state.consume({"type": "message_stop"})


@pytest.mark.parametrize("message", [[], None, {}, {"id": ""}, {"id": 1}])
def test_bad_initial_envelopes_fail_explicitly(message):
    with pytest.raises(ProviderError, match="malformed message_start"):
        MessagesState("claude-direct").consume({"type": "message_start", "message": message})


def test_initial_tool_input_cannot_be_overwritten_by_streamed_input():
    state = MessagesState("claude-direct")
    state.consume({"type": "content_block_start", "index": 0, "content_block": {
        "type": "tool_use", "id": "call", "name": "probe", "input": {"original": True}}})
    with pytest.raises(ProviderError, match="conflicting initial and streamed tool input"):
        state.consume({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "input_json_delta", "partial_json": "{}"}})
    assert state.blocks[0]["input"] == {"original": True}
