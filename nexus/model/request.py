"""Structured model requests and sampling parameters (plan section 3.2)."""
from __future__ import annotations

from typing import Any

import msgspec

from .message import Message

REASONING_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
REASONING_EFFORTS = frozenset(REASONING_EFFORT_ORDER)


class ToolSchema(msgspec.Struct, frozen=True):
    """A tool declaration sent to the model. Harness behaviour lives elsewhere."""

    name: str
    description: str
    input_schema: dict[str, Any]


class SamplingParams(msgspec.Struct, frozen=True):
    temperature: float | None = None
    max_output_tokens: int | None = None
    top_p: float | None = None
    stop_sequences: list[str] = msgspec.field(default_factory=list)
    thinking_budget: int | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in REASONING_EFFORTS
        ):
            choices = ", ".join(sorted(REASONING_EFFORTS))
            raise ValueError(
                f"reasoning_effort must be one of {choices}; got {self.reasoning_effort!r}"
            )


class ModelRequest(msgspec.Struct, frozen=True):
    """Everything a provider needs for one model call."""

    messages: list[Message]
    system: str | None = None
    tools: list[ToolSchema] = msgspec.field(default_factory=list)
    params: SamplingParams = msgspec.field(default_factory=SamplingParams)
    model: str | None = None
    provider: str | None = None
    metadata: dict[str, Any] = msgspec.field(default_factory=dict)


__all__ = [
    "REASONING_EFFORT_ORDER",
    "REASONING_EFFORTS",
    "ToolSchema",
    "SamplingParams",
    "ModelRequest",
]
