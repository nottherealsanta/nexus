"""Turn state machine, limits, usage, and outcome (plan sections 3.3, 4).

The loop owns exactly one :class:`TurnState` per turn. The state is immutable
and msgspec-serializable so it can be snapshotted into a session log or handed to
a UI without aliasing bugs. Transitions are validated: a terminal turn cannot be
resurrected, and ``awaiting_tools`` can only be re-entered through
``begin_iteration`` (which is what advances the loop counter).

Nothing here imports a manager, a provider, or the loop; ``core/loop.py`` drives
these types and ``session/`` persists them.
"""
from __future__ import annotations

import math
import time
from typing import Literal

import msgspec

from ..errors import NexusError
from ..model.stream import Usage as StreamUsage


class TurnStateError(NexusError, ValueError):
    """An illegal turn transition was attempted."""


#: Lifecycle phases. ``new`` is the factory state; the two ``awaiting_*`` phases
#: are the only non-terminal working phases.
TurnPhase = Literal[
    "new",
    "awaiting_model",
    "awaiting_tools",
    "completed",
    "failed",
    "cancelled",
]

#: ``stop_reason`` normalizes the provider vocabulary (plan section 3.3) and adds
#: the harness-level terminal causes the loop can produce.
TurnStopReason = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "refusal",
    "error",
    "budget",
    "max_iterations",
    "cancelled",
]

TERMINAL_PHASES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

#: Which phases a phase may move to. Terminal phases are deliberately absent.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"awaiting_model", "failed", "cancelled"}),
    "awaiting_model": frozenset({"awaiting_tools", "completed", "failed", "cancelled"}),
    "awaiting_tools": frozenset({"awaiting_model", "completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

#: Stop reasons that mean the turn ended without a harness-level failure.
_SUCCESS_STOP_REASONS: frozenset[str] = frozenset(
    {"end_turn", "tool_use", "max_tokens", "stop_sequence"}
)


def _is_int(value: object) -> bool:
    return type(value) is int


class TurnUsage(msgspec.Struct, frozen=True):
    """Token accounting for one turn, additive across iterations."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            value = getattr(self, name)
            if not _is_int(value) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def merge(self, other: TurnUsage) -> TurnUsage:
        """Return a new usage with ``other`` folded in (pure; never mutates)."""
        if not isinstance(other, TurnUsage):
            raise TypeError("can only merge TurnUsage")
        return TurnUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    def __add__(self, other: TurnUsage) -> TurnUsage:
        return self.merge(other)

    @classmethod
    def from_stream_usage(cls, usage: StreamUsage) -> TurnUsage:
        """Adapt a normalized provider ``Usage`` event into turn accounting."""
        return cls(
            input_tokens=usage.input,
            output_tokens=usage.output,
            cache_read_tokens=usage.cache_read,
            cache_write_tokens=usage.cache_write,
            reasoning_tokens=usage.reasoning,
        )


class TurnLimits(msgspec.Struct, frozen=True):
    """Per-turn ceilings. ``exceeded`` returns a reason string or ``None``."""

    max_iterations: int = 0  # 0 = unlimited
    max_seconds: float = 1800.0
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None

    def __post_init__(self) -> None:
        if not _is_int(self.max_iterations) or self.max_iterations < 0:
            raise ValueError("max_iterations must be an integer >= 0 (0 = unlimited)")
        if (
            type(self.max_seconds) not in (int, float)
            or not math.isfinite(self.max_seconds)
            or self.max_seconds <= 0
        ):
            raise ValueError("max_seconds must be a positive finite number")
        for name in ("max_input_tokens", "max_output_tokens", "max_total_tokens"):
            value = getattr(self, name)
            if value is not None and (not _is_int(value) or value < 1):
                raise ValueError(f"{name} must be None or an integer >= 1")

    def exceeded(
        self,
        usage: TurnUsage,
        elapsed_seconds: float,
        *,
        iterations: int = 0,
    ) -> str | None:
        """Return the first exceeded limit's name, or ``None`` when inside all.

        The signature is intentionally boolean-friendly: the planned loop writes
        ``if limits.exceeded(total, wall_clock): ...`` and treats the truthy
        string as the reason to record.
        """
        if not isinstance(usage, TurnUsage):
            raise TypeError("usage must be a TurnUsage")
        if self.max_iterations and iterations >= self.max_iterations:
            return "max_iterations"
        if elapsed_seconds >= self.max_seconds:
            return "max_seconds"
        if self.max_total_tokens is not None and usage.total_tokens >= self.max_total_tokens:
            return "max_total_tokens"
        if self.max_output_tokens is not None and usage.output_tokens >= self.max_output_tokens:
            return "max_output_tokens"
        if self.max_input_tokens is not None and usage.input_tokens >= self.max_input_tokens:
            return "max_input_tokens"
        return None


class TurnState(msgspec.Struct, frozen=True):
    """Immutable, serializable state of a single turn."""

    turn_id: str
    session_id: str | None = None
    phase: TurnPhase = "new"
    iteration: int = 0
    usage: TurnUsage = msgspec.field(default_factory=TurnUsage)
    stop_reason: TurnStopReason | None = None
    started_at: float | None = None
    updated_at: float | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise ValueError("turn_id must be a nonempty string")
        if not _is_int(self.iteration) or self.iteration < 0:
            raise ValueError("iteration must be a non-negative integer")
        if self.phase not in _ALLOWED_TRANSITIONS:
            raise ValueError(f"unknown turn phase: {self.phase!r}")
        if self.phase in TERMINAL_PHASES and self.stop_reason is None:
            raise ValueError(f"terminal phase {self.phase!r} requires a stop_reason")

    @classmethod
    def new(cls, *, turn_id: str, session_id: str | None = None) -> TurnState:
        return cls(turn_id=turn_id, session_id=session_id)

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def ok(self) -> bool:
        return self.phase == "completed" and self.stop_reason in _SUCCESS_STOP_REASONS

    def _transition(self, phase: TurnPhase, **changes) -> TurnState:
        if phase not in _ALLOWED_TRANSITIONS[self.phase]:
            raise TurnStateError(
                f"Illegal turn transition {self.phase!r} -> {phase!r} (turn {self.turn_id})"
            )
        changes.setdefault("updated_at", time.time())
        return msgspec.structs.replace(self, phase=phase, **changes)

    def start(self) -> TurnState:
        """``new`` -> ``awaiting_model``; stamps ``started_at``."""
        now = time.time()
        return self._transition("awaiting_model", started_at=self.started_at or now, updated_at=now)

    def model_responded(
        self,
        *,
        has_tool_use: bool,
        stop_reason: TurnStopReason = "end_turn",
    ) -> TurnState:
        """Record a completed model call.

        Tool calls move the turn to ``awaiting_tools``; anything else completes
        it with the provider's normalized ``stop_reason``.
        """
        if self.phase != "awaiting_model":
            raise TurnStateError(
                f"model_responded requires phase 'awaiting_model', not {self.phase!r}"
            )
        if has_tool_use:
            if stop_reason != "tool_use":
                raise TurnStateError(
                    f"tool-call turns must stop with 'tool_use', not {stop_reason!r}"
                )
            return self._transition("awaiting_tools")
        if stop_reason not in _SUCCESS_STOP_REASONS and stop_reason != "refusal":
            raise TurnStateError(f"cannot complete a turn with stop_reason {stop_reason!r}")
        return self._transition("completed", stop_reason=stop_reason)

    def begin_iteration(self) -> TurnState:
        """``awaiting_tools`` -> ``awaiting_model`` after results are persisted."""
        if self.phase != "awaiting_tools":
            raise TurnStateError(
                f"begin_iteration requires phase 'awaiting_tools', not {self.phase!r}"
            )
        return self._transition("awaiting_model", iteration=self.iteration + 1)

    def record_usage(self, usage: TurnUsage) -> TurnState:
        """Fold one model call's usage into the turn total."""
        if self.is_terminal:
            raise TurnStateError("cannot record usage on a terminal turn")
        return msgspec.structs.replace(
            self, usage=self.usage.merge(usage), updated_at=time.time()
        )

    def complete(self, stop_reason: TurnStopReason = "end_turn") -> TurnState:
        """Terminate successfully with an explicit reason (incl. budget/max_iterations)."""
        if stop_reason not in _SUCCESS_STOP_REASONS and stop_reason not in {
            "refusal",
            "budget",
            "max_iterations",
        }:
            raise TurnStateError(f"invalid completion stop_reason {stop_reason!r}")
        return self._transition("completed", stop_reason=stop_reason)

    def fail(self, error: str) -> TurnState:
        if not isinstance(error, str) or not error.strip():
            raise ValueError("error must be a nonempty string")
        return self._transition("failed", stop_reason="error", error=error)

    def cancel(self, reason: str | None = None) -> TurnState:
        return self._transition("cancelled", stop_reason="cancelled", error=reason)


class TurnOutcome(msgspec.Struct, frozen=True):
    """The value the loop returns when a turn ends."""

    turn_id: str
    stop_reason: TurnStopReason
    phase: TurnPhase
    session_id: str | None = None
    iterations: int = 0
    usage: TurnUsage = msgspec.field(default_factory=TurnUsage)
    error: str | None = None

    @classmethod
    def from_state(
        cls,
        state: TurnState,
        *,
        stop_reason: TurnStopReason | None = None,
    ) -> TurnOutcome:
        if not state.is_terminal:
            raise TurnStateError(
                f"turn {state.turn_id} is not terminal (phase {state.phase!r})"
            )
        return cls(
            turn_id=state.turn_id,
            session_id=state.session_id,
            stop_reason=stop_reason or state.stop_reason or "error",
            phase=state.phase,
            iterations=state.iteration,
            usage=state.usage,
            error=state.error,
        )

    @property
    def ok(self) -> bool:
        return self.phase == "completed" and self.stop_reason in _SUCCESS_STOP_REASONS

    def to_dict(self) -> dict:
        return msgspec.structs.asdict(self)


__all__ = [
    "TERMINAL_PHASES",
    "TurnLimits",
    "TurnOutcome",
    "TurnPhase",
    "TurnState",
    "TurnStateError",
    "TurnStopReason",
    "TurnUsage",
]
