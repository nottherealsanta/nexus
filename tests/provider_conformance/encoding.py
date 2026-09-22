"""Wire encoders used to generate adapter fixtures from normalized events.

The conformance suite prefers *generated* wire over checked-in bytes for the
round-trip cases: the same logical plan drives every adapter, so a new adapter
is added by implementing an encoder, not by recording a fixture. Checked-in
fixtures remain valuable for decode fidelity and are exercised separately.

``encode_anthropic_sse`` turns a normalized event plan into an Anthropic
Messages SSE body. It is faithful enough to be decoded by the real adapter:
content blocks are emitted sequentially (which is what Anthropic does), usage
is split across ``message_start`` and ``message_delta`` exactly as the wire
format requires, and a tool block with unparseable streamed JSON is deliberately
left open until the end so the shared accumulator observes the malformed
``content_block_stop``.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
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
    "DEFAULT_MODEL",
    "encode_anthropic_sse",
    "encode_gemini_sse",
    "encode_openai_chat_sse",
    "encode_openai_responses_sse",
]

DEFAULT_MODEL = "conformance-model"

#: Normalized stop reason -> Gemini ``finishReason``. ``tool_use`` has no Gemini
#: equivalent; ``STOP`` is sent and the adapter promotes it to ``tool_use`` when
#: the stream carried a function call.
_GEMINI_FINISH: dict[str, str] = {
    "end_turn": "STOP",
    "tool_use": "STOP",
    "max_tokens": "MAX_TOKENS",
    "stop_sequence": "STOP",
    "refusal": "SAFETY",
    "error": "MALFORMED_FUNCTION_CALL",
}


class _Encoder:
    def __init__(self, events: Sequence[StreamEvent]) -> None:
        self.events = list(events)
        self._parts: list[str] = []
        self._index = 0
        self._current: tuple[str, int] | None = None
        self._tool_index: dict[str, int] = {}
        self._usage = next(
            (e for e in self.events if isinstance(e, Usage)), None
        )
        start = next((e for e in self.events if isinstance(e, MessageStart)), None)
        self._model = (start.model if start else None) or DEFAULT_MODEL
        self._stop_reason = "end_turn"

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._parts.append(
            f"event: {event_type}\n"
            f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
        )

    def _close_current(self) -> None:
        if self._current is not None:
            self.emit(
                "content_block_stop",
                {"type": "content_block_stop", "index": self._current[1]},
            )
            self._current = None

    def _open(self, kind: str, block: dict[str, Any]) -> int:
        self._close_current()
        index = self._index
        self._index += 1
        self._current = (kind, index)
        self.emit(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": index,
                "content_block": block,
            },
        )
        return index

    def encode(self) -> bytes:
        start_usage: dict[str, Any] = {}
        if self._usage is not None:
            start_usage["input_tokens"] = self._usage.input
            if self._usage.cache_read:
                start_usage["cache_read_input_tokens"] = self._usage.cache_read
            if self._usage.cache_write:
                start_usage["cache_creation_input_tokens"] = self._usage.cache_write
        self.emit(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_conformance",
                    "type": "message",
                    "role": "assistant",
                    "model": self._model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": start_usage,
                },
            },
        )

        for event in self.events:
            self._encode_event(event)
        self._close_current()

        # A plan without a MessageStop is a *prefix* (used by the disconnect and
        # park steps): stop after rendered content so the consumer sees only what
        # was sent before the fault.
        if not any(isinstance(e, MessageStop) for e in self.events):
            return "".join(self._parts).encode("utf-8")

        delta_usage: dict[str, Any] = {}
        if self._usage is not None:
            delta_usage["output_tokens"] = self._usage.output
        self.emit(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": self._stop_reason,
                    "stop_sequence": None,
                },
                "usage": delta_usage,
            },
        )
        self.emit("message_stop", {"type": "message_stop"})
        return "".join(self._parts).encode("utf-8")

    def _encode_event(self, event: StreamEvent) -> None:
        if isinstance(event, (MessageStart, Usage)):
            return
        if isinstance(event, MessageStop):
            self._stop_reason = event.stop_reason
            return
        if isinstance(event, TextDelta):
            if self._current is None or self._current[0] != "text":
                self._open("text", {"type": "text", "text": ""})
            assert self._current is not None
            self.emit(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._current[1],
                    "delta": {"type": "text_delta", "text": event.text},
                },
            )
        elif isinstance(event, ThinkingDelta):
            if self._current is None or self._current[0] != "thinking":
                self._open("thinking", {"type": "thinking", "thinking": ""})
            assert self._current is not None
            self.emit(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._current[1],
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": event.text,
                    },
                },
            )
        elif isinstance(event, ThinkingEnd):
            if self._current is None or self._current[0] != "thinking":
                self._open("thinking", {"type": "thinking", "thinking": ""})
            assert self._current is not None
            if event.signature:
                self.emit(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._current[1],
                        "delta": {
                            "type": "signature_delta",
                            "signature": event.signature,
                        },
                    },
                )
            self._close_current()
        elif isinstance(event, ToolCallStart):
            index = self._open(
                "tool",
                {
                    "type": "tool_use",
                    "id": event.id,
                    "name": event.name,
                    "input": {},
                },
            )
            self._tool_index[event.id] = index
        elif isinstance(event, ToolCallDelta):
            if (
                self._current is None
                or self._current[0] != "tool"
                or self._tool_index.get(event.id) != self._current[1]
            ):
                index = self._open(
                    "tool",
                    {
                        "type": "tool_use",
                        "id": event.id,
                        "name": "",
                        "input": {},
                    },
                )
                self._tool_index[event.id] = index
            assert self._current is not None
            self.emit(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._current[1],
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": event.partial_json,
                    },
                },
            )
        elif isinstance(event, ToolCallEnd):
            if self._current is None or self._current[0] != "tool":
                index = self._open(
                    "tool",
                    {
                        "type": "tool_use",
                        "id": event.id,
                        "name": "",
                        "input": {},
                    },
                )
                self._tool_index[event.id] = index
            self._close_current()


def encode_anthropic_sse(events: Sequence[StreamEvent]) -> bytes:
    """Render a normalized event plan as an Anthropic Messages SSE body."""
    return _Encoder(events).encode()


# ---------------------------------------------------------------------------
# Gemini generateContent streaming
# ---------------------------------------------------------------------------


def _gemini_chunk(
    model: str,
    parts: list[dict[str, Any]],
    *,
    finish_reason: str | None = None,
    usage: Usage | None = None,
) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "content": {"role": "model", "parts": parts},
        "index": 0,
    }
    if finish_reason is not None:
        candidate["finishReason"] = finish_reason
    chunk: dict[str, Any] = {"candidates": [candidate], "modelVersion": model}
    if usage is not None:
        metadata: dict[str, Any] = {}
        if usage.input:
            metadata["promptTokenCount"] = usage.input
        if usage.output:
            metadata["candidatesTokenCount"] = usage.output
        if usage.cache_read:
            metadata["cachedContentTokenCount"] = usage.cache_read
        if usage.reasoning:
            metadata["thoughtsTokenCount"] = usage.reasoning
        if metadata:
            chunk["usageMetadata"] = metadata
    return chunk


def encode_gemini_sse(events: Sequence[StreamEvent]) -> bytes:
    """Render a normalized event plan as a Gemini ``streamGenerateContent`` body.

    Gemini's dialect differences are handled here rather than in the case:

    * ``functionCall`` is atomic — there is no partial-JSON delta — so
      ``tool_call_delta`` events are ignored and the finalized ``tool_call_end``
      input is sent whole. The semantic result is identical.
    * ``thoughtSignature`` rides on a part; a ``thinking_end`` becomes a
      signature-only part that the adapter finalizes before the next text or
      function call (or at end of stream).
    * ``usageMetadata`` has no cache-write field, so a plan's ``cache_write`` is
      not representable and is dropped by the dialect.
    * A plan without a ``MessageStop`` is a prefix (disconnect/park): no final
      chunk is emitted.
    """
    ordered = list(events)
    start = next((e for e in ordered if isinstance(e, MessageStart)), None)
    model = (start.model if start else None) or DEFAULT_MODEL
    usage = next((e for e in ordered if isinstance(e, Usage)), None)
    has_stop = any(isinstance(e, MessageStop) for e in ordered)
    stop_reason = next(
        (e.stop_reason for e in ordered if isinstance(e, MessageStop)), "end_turn"
    )

    chunks: list[dict[str, Any]] = [
        # First chunk establishes MessageStart even when it carries no content.
        _gemini_chunk(model, [])
    ]
    pending_call: tuple[str, str] | None = None
    for event in ordered:
        if isinstance(event, (MessageStart, Usage, MessageStop)):
            continue
        if isinstance(event, TextDelta):
            chunks.append(_gemini_chunk(model, [{"text": event.text}]))
        elif isinstance(event, ThinkingDelta):
            chunks.append(
                _gemini_chunk(model, [{"text": event.text, "thought": True}])
            )
        elif isinstance(event, ThinkingEnd):
            chunks.append(
                _gemini_chunk(model, [{"thoughtSignature": event.signature}])
            )
        elif isinstance(event, ToolCallStart):
            pending_call = (event.id, event.name)
        elif isinstance(event, ToolCallDelta):
            continue  # Gemini streams atomic functionCall args
        elif isinstance(event, ToolCallEnd):
            call_id, name = pending_call or (event.id, "")
            call: dict[str, Any] = {"name": name, "args": dict(event.input)}
            if call_id:
                call["id"] = call_id
            chunks.append(_gemini_chunk(model, [{"functionCall": call}]))
            pending_call = None

    if has_stop:
        chunks.append(
            _gemini_chunk(
                model,
                [],
                finish_reason=_GEMINI_FINISH.get(stop_reason, "STOP"),
                usage=usage,
            )
        )
    return "".join(
        f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
        for chunk in chunks
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# OpenAI dialects (Chat Completions + Responses)
# ---------------------------------------------------------------------------

#: Normalized stop reason -> Chat Completions ``finish_reason``.
_CHAT_FINISH: dict[str, str] = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "refusal": "content_filter",
    "error": "stop",
}


def _openai_usage(usage: Usage | None, *, chat: bool) -> dict[str, Any] | None:
    if usage is None:
        return None
    if chat:
        payload: dict[str, Any] = {
            "prompt_tokens": usage.input,
            "completion_tokens": usage.output,
            "total_tokens": usage.input + usage.output,
        }
        if usage.cache_read:
            payload["prompt_tokens_details"] = {"cached_tokens": usage.cache_read}
        if usage.reasoning:
            payload["completion_tokens_details"] = {
                "reasoning_tokens": usage.reasoning
            }
        return payload
    payload = {
        "input_tokens": usage.input,
        "output_tokens": usage.output,
        "total_tokens": usage.input + usage.output,
    }
    if usage.cache_read:
        payload["input_tokens_details"] = {"cached_tokens": usage.cache_read}
    if usage.reasoning:
        payload["output_tokens_details"] = {"reasoning_tokens": usage.reasoning}
    return payload


def _openai_plan(events: Sequence[StreamEvent]) -> dict[str, Any]:
    """Shared plan facts: model, usage, stop reason, and whether it terminates."""
    ordered = list(events)
    start = next((e for e in ordered if isinstance(e, MessageStart)), None)
    model = (start.model if start else None) or DEFAULT_MODEL
    usage = next((e for e in ordered if isinstance(e, Usage)), None)
    has_stop = any(isinstance(e, MessageStop) for e in ordered)
    stop_reason = next(
        (e.stop_reason for e in ordered if isinstance(e, MessageStop)), "end_turn"
    )
    return {
        "model": model,
        "usage": usage,
        "has_stop": has_stop,
        "stop_reason": stop_reason,
        "events": ordered,
    }


def encode_openai_chat_sse(events: Sequence[StreamEvent]) -> bytes:
    """Render a normalized event plan as a Chat Completions SSE body.

    Dialect differences handled here:

    * the first chunk carries ``delta.role`` and establishes ``MessageStart``;
    * tool calls stream partial JSON through ``delta.tool_calls[].function.arguments``
      keyed by ``index``; a call without a ``ToolCallEnd`` is left unterminated so
      the shared accumulator observes the malformed arguments;
    * reasoning text rides in ``delta.reasoning_content`` and its opaque
      signature (when the endpoint supplies one) in ``delta.reasoning_signature``;
    * usage arrives in a trailing chunk with an empty ``choices`` list, and the
      ``[DONE]`` sentinel closes the stream. A plan with no ``MessageStop`` is a
      prefix (disconnect/park) and emits neither.
    """
    plan = _openai_plan(events)
    model = plan["model"]
    parts: list[str] = []

    def emit(payload: dict[str, Any]) -> None:
        parts.append(
            f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
        )

    def chunk(delta: dict[str, Any], finish: str | None = None) -> None:
        emit(
            {
                "id": "chatcmpl-conformance",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish}
                ],
            }
        )

    chunk({"role": "assistant", "content": ""})

    tool_index: dict[str, int] = {}
    pending_tools: list[str] = []

    for event in plan["events"]:
        if isinstance(event, (MessageStart, Usage, MessageStop)):
            continue
        if isinstance(event, TextDelta):
            chunk({"content": event.text})
        elif isinstance(event, ThinkingDelta):
            chunk({"reasoning_content": event.text})
        elif isinstance(event, ThinkingEnd):
            if event.signature:
                chunk({"reasoning_signature": event.signature})
        elif isinstance(event, ToolCallStart):
            index = tool_index.setdefault(event.id, len(tool_index))
            pending_tools.append(event.id)
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": event.id,
                            "type": "function",
                            "function": {"name": event.name, "arguments": ""},
                        }
                    ]
                }
            )
        elif isinstance(event, ToolCallDelta):
            index = tool_index.setdefault(event.id, len(tool_index))
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "function": {"arguments": event.partial_json},
                        }
                    ]
                }
            )
        elif isinstance(event, ToolCallEnd):
            tool_index.setdefault(event.id, len(tool_index))
            if event.id in pending_tools:
                pending_tools.remove(event.id)

    if not plan["has_stop"]:
        return "".join(parts).encode("utf-8")

    chunk({}, finish=_CHAT_FINISH.get(plan["stop_reason"], "stop"))
    usage = _openai_usage(plan["usage"], chat=True)
    if usage is not None:
        emit(
            {
                "id": "chatcmpl-conformance",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [],
                "usage": usage,
            }
        )
    parts.append("data: [DONE]\n\n")
    return "".join(parts).encode("utf-8")


def _responses_event(event_type: str, payload: dict[str, Any]) -> str:
    body = {"type": event_type, **payload}
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(body, separators=(',', ':'))}\n\n"
    )


def encode_openai_responses_sse(events: Sequence[StreamEvent]) -> bytes:
    """Render a normalized event plan as a Responses API SSE body.

    Dialect differences handled here:

    * ``response.created`` establishes ``MessageStart``;
    * text streams through message content parts (``response.output_text.delta``);
    * reasoning streams through ``response.reasoning_summary_text.delta`` and its
      opaque signature through the reasoning item's ``encrypted_content``;
    * tool calls stream through ``response.function_call_arguments.delta`` and
      are finalized by ``response.output_item.done``;
    * refusal text rides a ``response.refusal.delta``, and usage arrives on
      ``response.completed``. A plan with no ``MessageStop`` is a prefix.
    """
    plan = _openai_plan(events)
    model = plan["model"]
    parts: list[str] = []
    parts.append(
        _responses_event(
            "response.created",
            {"response": {"id": "resp_conformance", "model": model, "status": "in_progress"}},
        )
    )

    for event in plan["events"]:
        if isinstance(event, (MessageStart, Usage, MessageStop)):
            continue
        if isinstance(event, TextDelta):
            # A refusal's text is framed as a refusal delta, exactly as the API
            # does; plain text uses output_text. The adapter records the refusal
            # from the event type, so the stop reason survives either framing.
            delta_type = (
                "response.refusal.delta"
                if plan["stop_reason"] == "refusal"
                else "response.output_text.delta"
            )
            parts.append(
                _responses_event(
                    delta_type,
                    {"output_index": 0, "content_index": 0, "delta": event.text},
                )
            )
        elif isinstance(event, ThinkingDelta):
            parts.append(
                _responses_event(
                    "response.reasoning_summary_text.delta",
                    {"output_index": 0, "summary_index": 0, "delta": event.text},
                )
            )
        elif isinstance(event, ThinkingEnd):
            if event.signature:
                parts.append(
                    _responses_event(
                        "response.output_item.done",
                        {
                            "output_index": 0,
                            "item": {
                                "type": "reasoning",
                                "id": "rs_conformance",
                                "encrypted_content": event.signature,
                                "summary": [],
                            },
                        },
                    )
                )
        elif isinstance(event, ToolCallStart):
            parts.append(
                _responses_event(
                    "response.output_item.added",
                    {
                        "output_index": 1,
                        "item": {
                            "type": "function_call",
                            "id": f"fc_{event.id}",
                            "call_id": event.id,
                            "name": event.name,
                            "arguments": "",
                        },
                    },
                )
            )
        elif isinstance(event, ToolCallDelta):
            parts.append(
                _responses_event(
                    "response.function_call_arguments.delta",
                    {"item_id": f"fc_{event.id}", "delta": event.partial_json},
                )
            )
        elif isinstance(event, ToolCallEnd):
            parts.append(
                _responses_event(
                    "response.output_item.done",
                    {
                        "output_index": 1,
                        "item": {
                            "type": "function_call",
                            "id": f"fc_{event.id}",
                            "call_id": event.id,
                            "name": "",
                            "arguments": json.dumps(
                                dict(event.input), separators=(",", ":")
                            ),
                        },
                    },
                )
            )

    if not plan["has_stop"]:
        return "".join(parts).encode("utf-8")

    stop_reason = plan["stop_reason"]
    usage = _openai_usage(plan["usage"], chat=False)
    if stop_reason == "max_tokens":
        parts.append(
            _responses_event(
                "response.incomplete",
                {
                    "response": {
                        "id": "resp_conformance",
                        "model": model,
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "usage": usage,
                    }
                },
            )
        )
    elif stop_reason == "error":
        parts.append(
            _responses_event(
                "response.failed",
                {
                    "response": {
                        "id": "resp_conformance",
                        "model": model,
                        "status": "failed",
                        "error": {"code": "server_error", "message": "failed"},
                    }
                },
            )
        )
    else:
        parts.append(
            _responses_event(
                "response.completed",
                {
                    "response": {
                        "id": "resp_conformance",
                        "model": model,
                        "status": "completed",
                        "usage": usage,
                    }
                },
            )
        )
    return "".join(parts).encode("utf-8")
