"""Normalized streaming events and the shared tool-call accumulator.

Adapters translate their wire format into exactly these events; the loop and
UIs consume them (plan section 3.3).
"""
from __future__ import annotations

import json
from typing import Any, Literal

import msgspec

from ..errors import MalformedToolCall

StopReason = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "refusal",
    "error",
]


class MessageStart(msgspec.Struct, tag="message_start", frozen=True):
    model: str | None = None
    provider: str | None = None


class TextDelta(msgspec.Struct, tag="text_delta", frozen=True):
    text: str


class ThinkingDelta(msgspec.Struct, tag="thinking_delta", frozen=True):
    text: str


class ThinkingEnd(msgspec.Struct, tag="thinking_end", frozen=True):
    signature: str


class ToolCallStart(msgspec.Struct, tag="tool_call_start", frozen=True):
    id: str
    name: str


class ToolCallDelta(msgspec.Struct, tag="tool_call_delta", frozen=True):
    id: str
    partial_json: str


class ToolCallEnd(msgspec.Struct, tag="tool_call_end", frozen=True):
    id: str
    input: dict[str, Any]


class Usage(msgspec.Struct, tag="usage", frozen=True):
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0


class MessageStop(msgspec.Struct, tag="message_stop", frozen=True):
    stop_reason: StopReason


class Raw(msgspec.Struct, tag="raw", frozen=True):
    """Opt-in passthrough of an un-normalized provider payload."""

    data: dict[str, Any]


StreamEvent = (
    MessageStart
    | TextDelta
    | ThinkingDelta
    | ThinkingEnd
    | ToolCallStart
    | ToolCallDelta
    | ToolCallEnd
    | Usage
    | MessageStop
    | Raw
)


class ToolCallAccumulator:
    """Buffer streamed tool-call argument JSON, keyed by call id.

    Shared infrastructure, not per-adapter code: OpenAI streams partial JSON,
    Anthropic streams ``input_json_delta``; both feed this. Interleaved calls
    are supported because every buffer is keyed by call id. Parsing happens at
    tool-call end and a typed :class:`MalformedToolCall` is raised when the
    buffered text is not a JSON object.

    A duplicate id — one that is already active or was already finished in the
    same message — is also a typed :class:`MalformedToolCall` on
    :meth:`start`. Overwriting the buffer would make the two calls
    indistinguishable and could let a denied call be executed under an allowed
    call's decision, so the ambiguity is refused instead.
    """

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._parts: dict[str, list[str]] = {}
        # Every id ever seen in this message, active or already finished. A
        # duplicate id is ambiguous (two calls would share one result slot) so it
        # is rejected deterministically rather than silently overwriting a
        # buffer, which could otherwise let a denied call be answered by another.
        self._seen: set[str] = set()

    def start(self, call_id: str, name: str) -> None:
        if call_id in self._seen:
            raise MalformedToolCall(
                call_id,
                "duplicate tool-call id; the whole duplicated-id group is rejected",
            )
        self._seen.add(call_id)
        self._names[call_id] = name
        self._parts[call_id] = []

    def delta(self, call_id: str, partial_json: str) -> None:
        if call_id not in self._parts:
            raise MalformedToolCall(call_id, "delta before start", raw=partial_json)
        self._parts[call_id].append(partial_json)

    def finish(self, call_id: str, input: dict[str, Any] | None = None) -> dict[str, Any]:
        """Finalize a call. ``input`` is authoritative when buffered JSON is bad.

        If the provider supplies a final parsed ``input`` and the streamed
        partial JSON is incomplete or malformed, fall back to that input rather
        than failing the turn. When no authoritative input exists, malformed
        buffered data still raises :class:`MalformedToolCall` so it is never
        silently masked.
        """
        parts = self._parts.pop(call_id, None)
        self._names.pop(call_id, None)
        raw = "".join(parts or []).strip()
        if not raw:
            return dict(input) if input is not None else {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            if input is not None:
                return dict(input)
            raise MalformedToolCall(call_id, str(exc), raw=raw) from exc
        if not isinstance(parsed, dict):
            if input is not None:
                return dict(input)
            raise MalformedToolCall(
                call_id, "tool arguments must be a JSON object", raw=raw
            )
        return parsed

    def handle(self, event: StreamEvent) -> ToolCallEnd | None:
        """Feed one stream event; return a finalized call on ``tool_call_end``."""
        if isinstance(event, ToolCallStart):
            self.start(event.id, event.name)
        elif isinstance(event, ToolCallDelta):
            self.delta(event.id, event.partial_json)
        elif isinstance(event, ToolCallEnd):
            return ToolCallEnd(id=event.id, input=self.finish(event.id, event.input))
        return None

    @property
    def pending(self) -> list[str]:
        return sorted(self._parts)


__all__ = [
    "MessageStart",
    "MessageStop",
    "Raw",
    "StopReason",
    "StreamEvent",
    "TextDelta",
    "ThinkingDelta",
    "ThinkingEnd",
    "ToolCallAccumulator",
    "ToolCallDelta",
    "ToolCallEnd",
    "ToolCallStart",
    "Usage",
]
