import msgspec
import pytest

from nexus.core.turn import (
    TERMINAL_PHASES,
    TurnLimits,
    TurnOutcome,
    TurnState,
    TurnStateError,
    TurnUsage,
)
from nexus.model.stream import Usage as StreamUsage


def _started(turn_id="t1"):
    return TurnState.new(turn_id=turn_id, session_id="s1").start()


def test_new_state_defaults_and_identity():
    state = TurnState.new(turn_id="t1", session_id="s1")
    assert state.phase == "new"
    assert state.iteration == 0
    assert state.usage == TurnUsage()
    assert state.stop_reason is None
    assert state.is_terminal is False


def test_turn_id_and_iteration_validation():
    with pytest.raises(ValueError):
        TurnState.new(turn_id="")
    with pytest.raises(ValueError):
        TurnState(turn_id="t1", iteration=-1)


def test_full_tool_loop_transition_sequence():
    state = _started()
    assert state.phase == "awaiting_model"
    assert state.started_at is not None

    state = state.model_responded(has_tool_use=True, stop_reason="tool_use")
    assert state.phase == "awaiting_tools"

    state = state.begin_iteration()
    assert state.phase == "awaiting_model"
    assert state.iteration == 1

    state = state.model_responded(has_tool_use=False, stop_reason="end_turn")
    assert state.phase == "completed"
    assert state.stop_reason == "end_turn"
    assert state.is_terminal
    assert state.ok


def test_model_without_tools_completes_with_reason():
    state = _started().model_responded(has_tool_use=False, stop_reason="max_tokens")
    assert state.phase == "completed"
    assert state.stop_reason == "max_tokens"


def test_refusal_completes_without_being_ok():
    state = _started().model_responded(has_tool_use=False, stop_reason="refusal")
    assert state.phase == "completed"
    assert state.ok is False


def test_illegal_transitions_raise():
    state = _started()
    with pytest.raises(TurnStateError):
        state.begin_iteration()  # awaiting_model -> awaiting_model is not allowed
    with pytest.raises(TurnStateError):
        state.model_responded(has_tool_use=True, stop_reason="error")

    completed = state.model_responded(has_tool_use=False, stop_reason="end_turn")
    with pytest.raises(TurnStateError):
        completed.start()
    with pytest.raises(TurnStateError):
        completed.begin_iteration()
    with pytest.raises(TurnStateError):
        completed.record_usage(TurnUsage(input_tokens=1))


def test_terminal_phases_are_sealed():
    assert TERMINAL_PHASES == {"completed", "failed", "cancelled"}
    failed = _started().fail("boom")
    assert failed.phase == "failed"
    assert failed.stop_reason == "error"
    assert failed.error == "boom"
    with pytest.raises(TurnStateError):
        failed.complete("end_turn")

    cancelled = _started().cancel("user")
    assert cancelled.phase == "cancelled"
    assert cancelled.stop_reason == "cancelled"


def test_usage_merge_is_pure_and_additive():
    first = TurnUsage(input_tokens=10, output_tokens=2, cache_read_tokens=3)
    second = TurnUsage(input_tokens=1, output_tokens=4, reasoning_tokens=5)
    total = first.merge(second)
    assert total == TurnUsage(
        input_tokens=11,
        output_tokens=6,
        cache_read_tokens=3,
        cache_write_tokens=0,
        reasoning_tokens=5,
    )
    assert first.input_tokens == 10  # unchanged
    assert total.total_tokens == 17
    assert (first + second) == total
    with pytest.raises(TypeError):
        first.merge("nope")


def test_usage_rejects_negative_and_maps_stream_usage():
    with pytest.raises(ValueError):
        TurnUsage(input_tokens=-1)
    mapped = TurnUsage.from_stream_usage(
        StreamUsage(input=5, output=6, cache_read=1, cache_write=2, reasoning=3)
    )
    assert mapped == TurnUsage(
        input_tokens=5,
        output_tokens=6,
        cache_read_tokens=1,
        cache_write_tokens=2,
        reasoning_tokens=3,
    )


def test_state_records_usage_across_iterations():
    state = _started()
    state = state.record_usage(TurnUsage(input_tokens=4, output_tokens=1))
    state = state.record_usage(TurnUsage(input_tokens=6, output_tokens=2))
    assert state.usage.input_tokens == 10
    assert state.usage.output_tokens == 3


def test_limits_validation_and_exceeded():
    with pytest.raises(ValueError):
        TurnLimits(max_iterations=-1)
    with pytest.raises(ValueError):
        TurnLimits(max_seconds=float("nan"))
    with pytest.raises(ValueError):
        TurnLimits(max_total_tokens=0)

    limits = TurnLimits(max_iterations=3, max_seconds=10.0, max_total_tokens=100)
    assert limits.exceeded(TurnUsage(input_tokens=1, output_tokens=1), 0.0) is None
    assert limits.exceeded(TurnUsage(), 0.0, iterations=3) == "max_iterations"
    assert limits.exceeded(TurnUsage(), 10.0) == "max_seconds"
    assert TurnLimits().exceeded(TurnUsage(), 0.0, iterations=10_000) is None  # 0 = unlimited
    assert TurnLimits().exceeded(TurnUsage(), 86_400.0) is None  # 0 seconds = unlimited
    with pytest.raises(ValueError):
        TurnLimits(max_seconds=-1)
    assert limits.exceeded(TurnUsage(input_tokens=60, output_tokens=40), 0.0) == "max_total_tokens"
    assert TurnLimits(max_output_tokens=5).exceeded(
        TurnUsage(output_tokens=5), 0.0
    ) == "max_output_tokens"


def test_outcome_requires_terminal_state_and_reports_ok():
    with pytest.raises(TurnStateError):
        TurnOutcome.from_state(_started())

    state = _started().model_responded(has_tool_use=False, stop_reason="end_turn")
    outcome = TurnOutcome.from_state(state)
    assert outcome.ok
    assert outcome.stop_reason == "end_turn"
    assert outcome.phase == "completed"
    assert outcome.session_id == "s1"

    budgeted = TurnOutcome.from_state(
        _started().complete("budget"), stop_reason="budget"
    )
    assert budgeted.ok is False
    assert budgeted.stop_reason == "budget"


def test_state_and_outcome_round_trip_through_json():
    state = (
        _started()
        .record_usage(TurnUsage(input_tokens=7, output_tokens=8))
        .model_responded(has_tool_use=True, stop_reason="tool_use")
        .begin_iteration()
        .complete("max_iterations")
    )
    decoded = msgspec.json.decode(msgspec.json.encode(state), type=TurnState)
    assert decoded == state

    outcome = TurnOutcome.from_state(state)
    decoded_outcome = msgspec.json.decode(msgspec.json.encode(outcome), type=TurnOutcome)
    assert decoded_outcome == outcome
    assert outcome.to_dict()["stop_reason"] == "max_iterations"
