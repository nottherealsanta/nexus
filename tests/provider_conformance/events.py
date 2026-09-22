"""Normalized-event helpers: builders, extractors, and structural comparison.

Cases describe their expected outcome in terms of *semantics* (concatenated
text, tool-call inputs, the final usage total, the stop reason) rather than an
exact event sequence, because a legitimate adapter may chunk deltas differently
or emit usage at a different point. Exact-sequence comparison is available for
cases that need it.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from nexus.model.stream import (
    MessageStart,
    MessageStop,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ThinkingEnd,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

__all__ = [
    "canonical_events",
    "event_types",
    "is_subset",
    "reply_parallel_tools",
    "reply_text",
    "reply_thinking",
    "reply_tool",
    "signatures_of",
    "stop_reason_of",
    "text_of",
    "thinking_of",
    "tool_calls_of",
    "usage_of",
]


# ---------------------------------------------------------------------------
# Builders for planned responses
# ---------------------------------------------------------------------------


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def reply_text(
    *chunks: str,
    usage: Usage | None = None,
    stop_reason: str = "end_turn",
) -> tuple[StreamEvent, ...]:
    """A text response, one delta per chunk."""
    events: list[StreamEvent] = [MessageStart()]
    events.extend(TextDelta(text=chunk) for chunk in chunks)
    if usage is not None:
        events.append(usage)
    events.append(MessageStop(stop_reason=stop_reason))  # type: ignore[arg-type]
    return tuple(events)


def reply_tool(
    call_id: str,
    name: str,
    *,
    input: dict[str, Any],
    arg_chunks: Sequence[str] | None = None,
    usage: Usage | None = None,
    stop_reason: str = "tool_use",
) -> tuple[StreamEvent, ...]:
    """A response with one tool call, its argument JSON split across chunks."""
    if arg_chunks is None:
        arg_chunks = (_json(input),)
    events: list[StreamEvent] = [
        MessageStart(),
        ToolCallStart(id=call_id, name=name),
    ]
    events.extend(ToolCallDelta(id=call_id, partial_json=chunk) for chunk in arg_chunks)
    events.append(ToolCallEnd(id=call_id, input=dict(input)))
    if usage is not None:
        events.append(usage)
    events.append(MessageStop(stop_reason=stop_reason))  # type: ignore[arg-type]
    return tuple(events)


def reply_parallel_tools(
    *calls: tuple[str, str, dict[str, Any]],
    usage: Usage | None = None,
    stop_reason: str = "tool_use",
) -> tuple[StreamEvent, ...]:
    """A response with sequential tool-call blocks (one per ``(id, name, input)``)."""
    events: list[StreamEvent] = [MessageStart()]
    for call_id, name, input in calls:
        events.append(ToolCallStart(id=call_id, name=name))
        events.append(ToolCallDelta(id=call_id, partial_json=_json(input)))
        events.append(ToolCallEnd(id=call_id, input=dict(input)))
    if usage is not None:
        events.append(usage)
    events.append(MessageStop(stop_reason=stop_reason))  # type: ignore[arg-type]
    return tuple(events)


def reply_thinking(
    *thinking_chunks: str,
    signature: str,
    text: str = "",
    usage: Usage | None = None,
    stop_reason: str = "end_turn",
) -> tuple[StreamEvent, ...]:
    """A response with a thinking block and an optional text follow-up."""
    events: list[StreamEvent] = [MessageStart()]
    events.extend(ThinkingDelta(text=chunk) for chunk in thinking_chunks)
    events.append(ThinkingEnd(signature=signature))
    if text:
        events.append(TextDelta(text=text))
    if usage is not None:
        events.append(usage)
    events.append(MessageStop(stop_reason=stop_reason))  # type: ignore[arg-type]
    return tuple(events)


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def text_of(events: Iterable[StreamEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


def thinking_of(events: Iterable[StreamEvent]) -> list[str]:
    return [e.text for e in events if isinstance(e, ThinkingDelta)]


def signatures_of(events: Iterable[StreamEvent]) -> list[str]:
    return [e.signature for e in events if isinstance(e, ThinkingEnd)]


def tool_calls_of(events: Iterable[StreamEvent]) -> list[tuple[str, str, dict[str, Any]]]:
    """Finalized tool calls, paired with the name seen at ``tool_call_start``."""
    names: dict[str, str] = {}
    calls: list[tuple[str, str, dict[str, Any]]] = []
    for event in events:
        if isinstance(event, ToolCallStart):
            names[event.id] = event.name
        elif isinstance(event, ToolCallEnd):
            calls.append(
                (event.id, names.get(event.id, ""), dict(event.input))
            )
    return calls


def usage_of(events: Iterable[StreamEvent]) -> Usage | None:
    last: Usage | None = None
    for event in events:
        if isinstance(event, Usage):
            last = event
    return last


def stop_reason_of(events: Iterable[StreamEvent]) -> str | None:
    for event in reversed(list(events)):
        if isinstance(event, MessageStop):
            return event.stop_reason
    return None


_TAGS: dict[type, str] = {
    MessageStart: "message_start",
    TextDelta: "text_delta",
    ThinkingDelta: "thinking_delta",
    ThinkingEnd: "thinking_end",
    ToolCallStart: "tool_call_start",
    ToolCallDelta: "tool_call_delta",
    ToolCallEnd: "tool_call_end",
    Usage: "usage",
    MessageStop: "message_stop",
}


def event_types(events: Iterable[StreamEvent]) -> list[str]:
    return [_TAGS.get(type(event), type(event).__name__) for event in events]


def canonical_events(events: Iterable[StreamEvent]) -> tuple[Any, ...]:
    """A provider-neutral projection for exact-sequence assertions.

    ``MessageStart`` collapses to its presence because model/provider names are
    legitimately adapter-specific. Tool-call end inputs are kept as sorted
    dicts, so key order never matters.
    """
    out: list[Any] = []
    for event in events:
        if isinstance(event, MessageStart):
            out.append(("message_start",))
        elif isinstance(event, TextDelta):
            out.append(("text_delta", event.text))
        elif isinstance(event, ThinkingDelta):
            out.append(("thinking_delta", event.text))
        elif isinstance(event, ThinkingEnd):
            out.append(("thinking_end", event.signature))
        elif isinstance(event, ToolCallStart):
            out.append(("tool_call_start", event.id, event.name))
        elif isinstance(event, ToolCallDelta):
            out.append(("tool_call_delta", event.id, event.partial_json))
        elif isinstance(event, ToolCallEnd):
            out.append(("tool_call_end", event.id, tuple(sorted(event.input.items()))))
        elif isinstance(event, Usage):
            out.append(
                (
                    "usage",
                    event.input,
                    event.output,
                    event.cache_read,
                    event.cache_write,
                    event.reasoning,
                )
            )
        elif isinstance(event, MessageStop):
            out.append(("message_stop", event.stop_reason))
        else:
            out.append(("other", type(event).__name__))
    return tuple(out)


def is_subset(expected: Any, actual: Any) -> bool:
    """Recursive subset: dict keys must exist and match; list is a prefix match.

    Used to assert that an outgoing wire body *contains* a required shape
    without pinning fields the adapter is free to add.
    """
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and is_subset(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) < len(expected):
            return False
        return all(is_subset(e, a) for e, a in zip(expected, actual))
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    return expected == actual
