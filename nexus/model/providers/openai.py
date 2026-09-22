"""OpenAI adapter: Responses API and Chat Completions behind one class (Phase 7b).

Plan section 8 specifies one adapter per *wire protocol*, not per vendor. The
OpenAI ecosystem has two live wire protocols that share a dialect family but not
a shape:

* the newer **Responses API** (``POST /v1/responses``), whose request carries
  ``instructions`` and a flat ``input`` item list and whose stream is a typed
  event log (``response.output_item.added``, ``response.output_text.delta``,
  ``response.function_call_arguments.delta``, ...); and
* the older **Chat Completions API** (``POST /v1/chat/completions``), whose
  request carries a ``messages`` list and whose stream is a flat list of
  ``choices[].delta`` chunks terminated by ``data: [DONE]``.

Both are selectable by config (``api = "responses" | "chat"``) and both are also
the transport for every **OpenAI-compatible** endpoint (Groq, Together,
OpenRouter, vLLM, LM Studio, Fireworks, and OpenAI's own Codex models, which are
Responses-API models reached with a normal key and therefore need no separate
CLI adapter). ``base_url`` is the extensibility lever: adding a vendor is a
``[providers.<name>]`` block, never a code change.

What this adapter owns
----------------------

* schema translation in (:func:`build_request_body`) and stream translation out
  (:meth:`OpenAIProvider.stream`), with a separate decoder per dialect;
* partial-JSON tool arguments, accumulated by the **shared**
  :class:`~nexus.model.stream.ToolCallAccumulator` and parsed at call end, so a
  malformed call surfaces the same typed :class:`~nexus.errors.MalformedToolCall`
  as every other adapter and the loop turns it into a model-visible result;
* usage accounting across the dialect's fields — cache reads, reasoning tokens;
* reasoning: Responses reasoning-summary deltas become ``ThinkingDelta`` and the
  opaque ``encrypted_content`` becomes the ``ThinkingEnd`` signature; a
  compatible endpoint that reports ``reasoning_content`` (and, where it exists,
  a ``reasoning_signature``) maps the same way.

Deliberate decisions, matching the reference adapter
---------------------------------------------------

* **Backed by the IR, not the vendor SDK.** Only :mod:`nexus.model` contracts
  are imported; ``httpx``, retry/backoff, and SSE framing stay in
  :mod:`nexus.model.http`.
* **Credentials resolve at request time.** The configured value may be a literal
  or an ``${env:VAR}`` reference; it is sent as an ``Authorization: Bearer``
  header, never in a URL, and never appears in ``repr`` or an error. Custom
  request headers configured for a gateway are likewise never rendered in an
  error, a repr, or a log.
* **Capabilities come from an injected source** (the registry, plan section
  15.5) when one is supplied; the in-module table is only the fallback.
* **Retries stay in the shared transport**, and only before the first event;
  a mid-stream failure is surfaced, never replayed.
* **``count_tokens`` is real only where OpenAI publishes an endpoint.** The
  Responses input-token endpoint (``POST /v1/responses/input_tokens``) exists;
  Chat Completions has no equivalent. On any non-official ``base_url`` (a
  compatible gateway) this adapter returns ``None`` without a network call so
  the caller falls back to the tokenizer heuristic rather than guessing at an
  endpoint the vendor may not implement.

Known mapping limits, stated plainly rather than papered over:

* Chat Completions has no document block; a :class:`~nexus.model.message.Document`
  degrades to a short text note (declared ``documents: to_text``).
* Neither dialect has a portable thinking *budget* field the IR can fill from
  ``SamplingParams.thinking_budget``; reasoning effort is model-selected, so the
  budget is not sent.
* Prompt caching is automatic server-side; there are no explicit breakpoints to
  place, so the ``cache`` metadata other adapters read is ignored here.
* **Reasoning replay is not implemented.** A Responses reasoning item's
  opaque ``encrypted_content`` is captured as the thinking signature when the
  stream is decoded, but :func:`_responses_input` never emits a reasoning input
  item on the way back, so *all* thinking blocks -- a native Responses signature
  as well as a foreign one -- are dropped from a replayed request. The declared
  degradation is ``thinking: drop`` (see ``_fallback_capabilities``), and the
  conformance case ``openai_responses_reasoning_replay_drops_foreign`` plus the
  unit test ``test_responses_reasoning_is_not_replayed`` pin the behaviour.
"""
from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import aclosing
from typing import Any

from ...errors import ProviderError
from ..capabilities import Capabilities
from ..http import HTTPTransport, redact_secrets
from ..message import (
    ContentBlock,
    Document,
    Image,
    Message,
    Text,
    ToolResult,
    ToolUse,
)
from ..request import ModelRequest
from ..stream import (
    MessageStart,
    MessageStop,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ThinkingEnd,
    ToolCallAccumulator,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

__all__ = [
    "API_CHAT",
    "API_RESPONSES",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_TOKENS",
    "OFFICIAL_HOSTS",
    "OpenAIProvider",
    "build_count_tokens_body",
    "build_request_body",
    "normalize_chat_finish_reason",
    "normalize_responses_status",
]

DEFAULT_BASE_URL = "https://api.openai.com/v1"

#: API dialects, selected by ``[providers.<name>].api``.
API_RESPONSES = "responses"
API_CHAT = "chat"

#: Hosts at which OpenAI's own endpoints (including the Responses input-token
#: counter) are known to exist. A compatible gateway is deliberately not trusted
#: to implement them.
OFFICIAL_HOSTS = frozenset({"api.openai.com"})

DEFAULT_MAX_TOKENS = 4096

#: Bound on a redacted in-stream error detail, matching the transport's cap.
_STREAM_ERROR_LIMIT = 500

#: Chat Completions ``finish_reason`` -> normalized vocabulary (plan section 3.3).
_CHAT_STOP_REASONS: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

#: Responses ``incomplete_details.reason`` -> normalized vocabulary.
_RESPONSES_INCOMPLETE_REASONS: dict[str, str] = {
    "max_output_tokens": "max_tokens",
    "content_filter": "refusal",
}

#: A capability source lets a runtime inject the registry/config-derived
#: descriptor (plan section 15.5) without this adapter importing either.
CapabilitySource = Callable[[str], "Capabilities | None"]


def normalize_chat_finish_reason(reason: object) -> str | None:
    """Map a Chat Completions ``finish_reason`` to the normalized vocabulary."""
    if reason is None:
        return None
    return _CHAT_STOP_REASONS.get(str(reason), "end_turn")


def normalize_responses_status(
    status: object, *, incomplete_reason: object = None
) -> str | None:
    """Map a Responses terminal status (and incomplete reason) to normalized."""
    if status is None:
        return None
    text = str(status)
    if text == "incomplete":
        if incomplete_reason is not None:
            return _RESPONSES_INCOMPLETE_REASONS.get(str(incomplete_reason), "end_turn")
        return "end_turn"
    if text == "failed":
        return "error"
    return "end_turn"


def _is_openai_host(base_url: str) -> bool:
    try:
        host = str(base_url).split("://", 1)[-1].split("/", 1)[0]
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return False
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    return host in OFFICIAL_HOSTS


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


def _data_url(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def _image_url(block: Image) -> str | None:
    if block.data is not None:
        return _data_url(block.media_type, block.data)
    return block.url or None


def _tool_result_text(block: ToolResult) -> str:
    """Flatten a tool result's block list to text for a text-only carrier.

    Images cannot ride in a Chat Completions ``tool`` message or a Responses
    ``function_call_output`` string, so they become a short note; the durable
    history keeps the original blocks.
    """
    pieces: list[str] = []
    omitted = False
    for item in block.content:
        if isinstance(item, Text):
            pieces.append(item.text)
        elif isinstance(item, (Image, Document)):
            omitted = True
    if omitted:
        pieces.append("[attachment omitted: tool results are text-only on this dialect]")
    return "\n".join(pieces)


# -- Chat Completions input -------------------------------------------------


def _chat_content_parts(
    blocks: list[ContentBlock],
) -> tuple[str, list[dict[str, Any]]]:
    """Split a user message into a plain-text part list for chat.

    Returns ``(leading_text, parts)``: ``leading_text`` is the concatenation of
    :class:`Text` blocks and ``parts`` is the multimodal part list used only when
    a non-text block is present. Documents degrade to text (declared policy).
    """
    text_pieces: list[str] = []
    parts: list[dict[str, Any]] = []
    saw_media = False
    for block in blocks:
        if isinstance(block, Text):
            text_pieces.append(block.text)
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, Image):
            url = _image_url(block)
            if url is not None:
                saw_media = True
                parts.append({"type": "image_url", "image_url": {"url": url}})
        elif isinstance(block, Document):
            saw_media = True
            title = f" {block.title}" if block.title else ""
            note = f"[document {block.media_type}{title} omitted: not supported]"
            text_pieces.append(note)
            parts.append({"type": "text", "text": note})
        # Thinking has no chat input carrier; dropped by policy.
    if not saw_media:
        return "".join(text_pieces), []
    return "".join(text_pieces), parts


def _chat_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate IR messages into a Chat Completions ``messages`` list.

    Tool-result blocks become dedicated ``role: tool`` messages (one per
    ``tool_call_id``), which is what the API requires; every other block stays
    with the message's own role. Durable history is never rewritten.
    """
    wire: list[dict[str, Any]] = []
    for message in messages:
        text, parts = _chat_content_parts(message.content)
        tool_calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, ToolUse):
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(
                                dict(block.input), separators=(",", ":")
                            ),
                        },
                    }
                )
            elif isinstance(block, ToolResult):
                results.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.tool_use_id,
                        "content": _tool_result_text(block),
                    }
                )
        has_content = bool(text) or bool(parts)
        if message.role == "assistant" and tool_calls:
            entry: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
            if has_content:
                entry["content"] = parts if parts else text
            else:
                entry["content"] = None
            wire.append(entry)
        elif has_content:
            wire.append({"role": message.role, "content": parts if parts else text})
        wire.extend(results)
    return wire


def _chat_tools(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.input_schema),
            },
        }
        for tool in tools
    ]


def _build_chat_body(
    req: ModelRequest, *, model: str, default_max_tokens: int
) -> dict[str, Any]:
    params = req.params
    body: dict[str, Any] = {
        "model": model,
        "stream": True,
        # Ask the endpoint to include a final usage frame. Endpoints that do not
        # understand it ignore the field, so they are not broken by it.
        "stream_options": {"include_usage": True},
        "messages": _chat_messages(req.messages),
    }
    if req.system:
        body["messages"].insert(0, {"role": "system", "content": req.system})
    if req.tools:
        body["tools"] = _chat_tools(list(req.tools))
        body["tool_choice"] = "auto"
    max_output = params.max_output_tokens or default_max_tokens
    if max_output:
        # ``max_tokens`` rather than ``max_completion_tokens``: the latter is
        # OpenAI-newer-only, and broad OpenAI-compatible compatibility is this
        # adapter's purpose.
        body["max_tokens"] = int(max_output)
    if params.temperature is not None:
        body["temperature"] = params.temperature
    if params.top_p is not None:
        body["top_p"] = params.top_p
    if params.stop_sequences:
        body["stop"] = list(params.stop_sequences)
    return body


# -- Responses input --------------------------------------------------------


def _responses_part(block: ContentBlock, *, text_type: str) -> dict[str, Any] | None:
    """One Responses content part for a single block, or ``None`` to skip it."""
    if isinstance(block, Text):
        return {"type": text_type, "text": block.text}
    if isinstance(block, Image):
        url = _image_url(block)
        if url is not None:
            return {"type": "input_image", "image_url": url}
        return None
    if isinstance(block, Document):
        return {
            "type": "input_file",
            "filename": block.title or "document",
            "file_data": _data_url(block.media_type, block.data),
        }
    return None


def _responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate IR messages into a Responses ``input`` item list.

    Block order is preserved: a message whose content interleaves text and tool
    calls becomes a sequence of ``message`` items (one per contiguous text/media
    run) and ``function_call``/``function_call_output`` items in their original
    order, rather than all calls first and one merged message last. Responses
    input is an ordered item list, so this keeps a replayed turn faithful.
    """
    items: list[dict[str, Any]] = []
    for message in messages:
        text_type = "output_text" if message.role == "assistant" else "input_text"
        pending: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, ToolUse):
                if pending:
                    items.append(
                        {"role": message.role, "content": list(pending)}
                    )
                    pending = []
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": json.dumps(
                            dict(block.input), separators=(",", ":")
                        ),
                    }
                )
            elif isinstance(block, ToolResult):
                if pending:
                    items.append(
                        {"role": message.role, "content": list(pending)}
                    )
                    pending = []
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": block.tool_use_id,
                        "output": _tool_result_text(block),
                    }
                )
            else:
                part = _responses_part(block, text_type=text_type)
                if part is not None:
                    pending.append(part)
        if pending:
            items.append({"role": message.role, "content": list(pending)})
    return items


def _responses_tools(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.input_schema),
        }
        for tool in tools
    ]


def _responses_content(
    req: ModelRequest,
    *,
    model: str,
) -> dict[str, Any]:
    """The fields shared by ``responses`` and ``responses/input_tokens``."""
    body: dict[str, Any] = {
        "model": model,
        "input": _responses_input(req.messages),
    }
    if req.system:
        body["instructions"] = req.system
    if req.tools:
        body["tools"] = _responses_tools(list(req.tools))
    return body


def _build_responses_body(
    req: ModelRequest, *, model: str, default_max_tokens: int
) -> dict[str, Any]:
    body = _responses_content(req, model=model)
    body["stream"] = True
    params = req.params
    max_output = params.max_output_tokens or default_max_tokens
    if max_output:
        body["max_output_tokens"] = int(max_output)
    if params.temperature is not None:
        body["temperature"] = params.temperature
    if params.top_p is not None:
        body["top_p"] = params.top_p
    # Responses has no ``stop`` parameter; ``stop_sequences`` is dropped rather
    # than sent and rejected.
    return body


def build_request_body(
    req: ModelRequest,
    *,
    model: str,
    api: str = API_RESPONSES,
    default_max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Translate a :class:`ModelRequest` into an OpenAI request body.

    The dialect is chosen by ``api``: ``"responses"`` or ``"chat"``.
    """
    if api == API_CHAT:
        return _build_chat_body(req, model=model, default_max_tokens=default_max_tokens)
    if api == API_RESPONSES:
        return _build_responses_body(
            req, model=model, default_max_tokens=default_max_tokens
        )
    raise ProviderError(f"openai: unknown api dialect {api!r}")


def build_count_tokens_body(req: ModelRequest, *, model: str) -> dict[str, Any]:
    """The ``/v1/responses/input_tokens`` payload for ``req``.

    The endpoint accepts ``responses.create`` parameters and returns an exact
    input count. Generation-only fields (``stream``, ``max_output_tokens``,
    ``temperature``, ``top_p``) cannot change the count and are omitted.
    """
    return _responses_content(req, model=model)


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------


class _UsageTotals:
    """Cumulative usage, last-write-wins per field across stream frames."""

    __slots__ = ("cache_read", "input", "output", "reasoning")

    def __init__(self) -> None:
        self.input: int | None = None
        self.output: int | None = None
        self.cache_read: int | None = None
        self.reasoning: int | None = None

    @staticmethod
    def _int(payload: Mapping[str, Any], key: str) -> int | None:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def update_chat(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        self.input = self._int(payload, "prompt_tokens") or self.input
        self.output = self._int(payload, "completion_tokens") or self.output
        details = payload.get("prompt_tokens_details")
        if isinstance(details, Mapping):
            self.cache_read = self._int(details, "cached_tokens") or self.cache_read
        completion = payload.get("completion_tokens_details")
        if isinstance(completion, Mapping):
            self.reasoning = (
                self._int(completion, "reasoning_tokens") or self.reasoning
            )

    def update_responses(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        self.input = self._int(payload, "input_tokens") or self.input
        self.output = self._int(payload, "output_tokens") or self.output
        details = payload.get("input_tokens_details")
        if isinstance(details, Mapping):
            self.cache_read = self._int(details, "cached_tokens") or self.cache_read
        output_details = payload.get("output_tokens_details")
        if isinstance(output_details, Mapping):
            self.reasoning = (
                self._int(output_details, "reasoning_tokens") or self.reasoning
            )

    @property
    def has_values(self) -> bool:
        return any(
            getattr(self, attr) is not None
            for attr in ("input", "output", "cache_read", "reasoning")
        )

    def to_event(self) -> Usage:
        return Usage(
            input=self.input or 0,
            output=self.output or 0,
            cache_read=self.cache_read or 0,
            cache_write=0,
            reasoning=self.reasoning or 0,
        )


def _parse_payload(event_data: str) -> dict[str, Any]:
    try:
        payload = json.loads(event_data)
    except json.JSONDecodeError as exc:
        raise ProviderError("openai: malformed SSE JSON payload") from exc
    if not isinstance(payload, dict):
        raise ProviderError("openai: SSE payload is not a JSON object")
    return payload


def _stream_error_label(error: Mapping[str, Any], *, fallback: str = "error") -> str:
    """A stable label for a provider error object.

    Both ``type`` and ``code`` are included when they differ so a caller can
    match either the category (``invalid_request_error``) or the specific
    condition (``context_length_exceeded``). The label carries no free text, so
    it can never leak a credential.
    """
    labels: list[str] = []
    for key in ("type", "code"):
        value = error.get(key)
        if value:
            text = str(value)
            if text not in labels:
                labels.append(text)
    return "/".join(labels) if labels else fallback


# ---------------------------------------------------------------------------
# Chat Completions streaming
# ---------------------------------------------------------------------------


class _ChatState:
    __slots__ = ("has_tools", "started", "stop_reason", "tool_ids")

    def __init__(self) -> None:
        self.started = False
        self.has_tools = False
        self.stop_reason: str | None = None
        #: Chat Completions keys a streamed call by ``index`` and only carries
        #: the id on the opening chunk, so the mapping is remembered here and
        #: used to resolve later argument-only chunks.
        self.tool_ids: dict[int, str] = {}


def _chat_tool_deltas(
    delta: Mapping[str, Any],
    state: _ChatState,
    accumulator: ToolCallAccumulator,
) -> Iterator[StreamEvent]:
    raw_calls = delta.get("tool_calls")
    if not isinstance(raw_calls, list):
        return
    for entry in raw_calls:
        if not isinstance(entry, Mapping):
            continue
        index = entry.get("index")
        index_key = index if isinstance(index, int) else None
        call_id = str(entry.get("id") or "")
        if call_id and index_key is not None:
            state.tool_ids[index_key] = call_id
        if not call_id and index_key is not None:
            call_id = state.tool_ids.get(index_key, "")
        function = entry.get("function")
        function = function if isinstance(function, Mapping) else {}
        name = str(function.get("name") or "")
        # A provider that omits an id gets a deterministic, non-colliding one
        # derived from the chunk index so the accumulator can still key it.
        if not call_id:
            call_id = f"call_{index}" if isinstance(index, int) else "call_0"
        arguments = function.get("arguments")
        # ``tool_call_start`` is only safe to emit once per id: the accumulator
        # rejects a duplicate start, and a benign re-send of the id in a later
        # chunk must not be treated as malformed.
        if call_id not in accumulator.pending and name:
            accumulator.start(call_id, name)
            yield ToolCallStart(id=call_id, name=name)
        if isinstance(arguments, str) and arguments:
            accumulator.delta(call_id, arguments)
            yield ToolCallDelta(id=call_id, partial_json=arguments)


def _events_for_chat_chunk(
    payload: Mapping[str, Any],
    state: _ChatState,
    accumulator: ToolCallAccumulator,
) -> Iterator[StreamEvent]:
    if isinstance(payload.get("error"), Mapping):
        error = payload["error"]
        kind = _stream_error_label(error)
        message = str(error.get("message") or "")
        detail = redact_secrets(message.strip())[:_STREAM_ERROR_LIMIT]
        raise ProviderError(f"openai: stream error {kind}: {detail}")
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        delta = choice.get("delta")
        if isinstance(delta, Mapping):
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield TextDelta(text=content)
            reasoning = delta.get("reasoning_content")
            if not isinstance(reasoning, str):
                reasoning = delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                yield ThinkingDelta(text=reasoning)
            signature = delta.get("reasoning_signature")
            if isinstance(signature, str) and signature:
                yield ThinkingEnd(signature=signature)
            yield from _chat_tool_deltas(delta, state, accumulator)
            if delta.get("tool_calls"):
                state.has_tools = True
        reason = choice.get("finish_reason")
        if reason:
            normalized = normalize_chat_finish_reason(reason)
            if normalized:
                state.stop_reason = normalized


def _finalize_pending_tools(
    accumulator: ToolCallAccumulator,
) -> Iterator[ToolCallEnd]:
    """Finalize any call the stream ended without closing.

    A well-formed Chat or Responses stream closes every call (a ``finish_reason``
    or ``response.output_item.done``), but a truncated one may not. Finalizing
    here surfaces the accumulated arguments — or the typed
    :class:`~nexus.errors.MalformedToolCall` — rather than silently dropping the
    call.
    """
    for call_id in list(accumulator.pending):
        yield ToolCallEnd(id=call_id, input=accumulator.finish(call_id))


# ---------------------------------------------------------------------------
# Responses streaming
# ---------------------------------------------------------------------------


class _ResponsesState:
    __slots__ = (
        "call_ids_by_item",
        "has_refusal",
        "has_tools",
        "incomplete_reason",
        "pending_call_id",
        "reasoning_signature",
        "started",
        "status",
        "stop_reason",
        "thinking_open",
    )

    def __init__(self) -> None:
        self.started = False
        self.has_tools = False
        self.has_refusal = False
        self.thinking_open = False
        self.reasoning_signature: str | None = None
        self.pending_call_id: str | None = None
        #: Responses addresses a function-call item by ``item.id`` on argument
        #: deltas but by ``call_id`` on the added/done items. Remember the
        #: mapping so every frame finalizes the *same* accumulator entry even if
        #: a gateway omits ``call_id`` on one of them.
        self.call_ids_by_item: dict[str, str] = {}
        self.status: str | None = None
        self.incomplete_reason: str | None = None
        self.stop_reason: str | None = None


def _responses_flush_thinking(state: _ResponsesState) -> Iterator[StreamEvent]:
    if not state.thinking_open and state.reasoning_signature is None:
        return
    signature = state.reasoning_signature or ""
    state.reasoning_signature = None
    state.thinking_open = False
    yield ThinkingEnd(signature=signature)


def _events_for_responses_event(
    event_type: str,
    payload: Mapping[str, Any],
    state: _ResponsesState,
    accumulator: ToolCallAccumulator,
) -> Iterator[StreamEvent]:
    if event_type in ("error", "response.failed"):
        error = payload.get("error")
        if not isinstance(error, Mapping):
            response = payload.get("response")
            error = (
                response.get("error")
                if isinstance(response, Mapping)
                else None
            )
        error = error if isinstance(error, Mapping) else {}
        kind = _stream_error_label(error, fallback=event_type or "error")
        message = str(error.get("message") or payload.get("message") or "")
        detail = redact_secrets(message.strip())[:_STREAM_ERROR_LIMIT]
        raise ProviderError(f"openai: stream error {kind}: {detail}")

    if event_type == "response.output_item.added":
        item = payload.get("item")
        if not isinstance(item, Mapping):
            return
        item_type = item.get("type")
        if item_type == "function_call":
            item_id = str(item.get("id") or "")
            call_id = str(item.get("call_id") or item_id)
            if not call_id:
                return
            if item_id:
                state.call_ids_by_item[item_id] = call_id
            accumulator.start(call_id, str(item.get("name") or ""))
            state.pending_call_id = call_id
            state.has_tools = True
            yield from _responses_flush_thinking(state)
            yield ToolCallStart(id=call_id, name=str(item.get("name") or ""))
        elif item_type == "reasoning":
            state.thinking_open = True
    elif event_type == "response.reasoning_summary_text.delta":
        delta = payload.get("delta")
        if isinstance(delta, str) and delta:
            state.thinking_open = True
            yield ThinkingDelta(text=delta)
    elif event_type == "response.reasoning_summary_text.done":
        # The summary text already streamed as ``...text.delta`` events; the
        # ``.done`` frame is terminal confirmation and re-emitting it would
        # duplicate the reasoning in the normalized stream.
        return
    elif event_type == "response.output_text.delta":
        delta = payload.get("delta")
        if isinstance(delta, str) and delta:
            yield from _responses_flush_thinking(state)
            yield TextDelta(text=delta)
    elif event_type == "response.refusal.delta":
        # The event type itself is the refusal signal; a provider may frame the
        # text as an earlier output_text delta and send an empty refusal marker,
        # so refusal is recorded even when this frame carries no text.
        state.has_refusal = True
        delta = payload.get("delta")
        if isinstance(delta, str) and delta:
            yield from _responses_flush_thinking(state)
            yield TextDelta(text=delta)
    elif event_type == "response.function_call_arguments.delta":
        delta = payload.get("delta")
        if isinstance(delta, str) and delta:
            item_id = str(payload.get("item_id") or "")
            call_id = (
                state.pending_call_id
                or state.call_ids_by_item.get(item_id)
                or item_id
            )
            if call_id:
                accumulator.delta(call_id, delta)
                yield ToolCallDelta(id=call_id, partial_json=delta)
    elif event_type == "response.output_item.done":
        item = payload.get("item")
        if not isinstance(item, Mapping):
            return
        item_type = item.get("type")
        if item_type == "function_call":
            item_id = str(item.get("id") or "")
            call_id = (
                str(item.get("call_id") or "")
                or state.call_ids_by_item.get(item_id)
                or state.pending_call_id
                or item_id
            )
            authoritative: dict[str, Any] | None = None
            raw_arguments = item.get("arguments")
            if isinstance(raw_arguments, str) and raw_arguments.strip():
                try:
                    parsed = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    authoritative = parsed
            final = accumulator.finish(call_id, authoritative)
            state.call_ids_by_item.pop(item_id, None)
            state.pending_call_id = None
            yield ToolCallEnd(id=call_id, input=final)
        elif item_type == "reasoning":
            encrypted = item.get("encrypted_content")
            if isinstance(encrypted, str) and encrypted:
                state.reasoning_signature = encrypted
            yield from _responses_flush_thinking(state)
    elif event_type == "response.completed":
        response = payload.get("response")
        if isinstance(response, Mapping):
            state.status = str(response.get("status") or "completed")
    elif event_type in ("response.incomplete", "response.failed"):
        response = payload.get("response")
        if isinstance(response, Mapping):
            state.status = str(response.get("status") or "incomplete")
            details = response.get("incomplete_details")
            if isinstance(details, Mapping):
                reason = details.get("reason")
                if reason is not None:
                    state.incomplete_reason = str(reason)
    # response.created / content_part.added / *.done carry no normalized delta.


# ---------------------------------------------------------------------------
# Fallback capabilities
# ---------------------------------------------------------------------------


def _fallback_capabilities(model: str) -> Capabilities:
    """A reasonable descriptor when no registry source is injected.

    The registry (plan section 15.5) is authoritative whenever available. The
    degradation policies are the cross-provider contract: reasoning this adapter
    cannot replay is dropped, an image or document a model cannot take is
    replaced by a short text note, so the loop's one-retry path always changes
    the request.
    """
    return Capabilities(
        tools=True,
        parallel_tool_calls=True,
        streaming=True,
        thinking=True,
        prompt_caching=True,
        vision=True,
        documents=True,
        json_schema_strict=True,
        max_context_tokens=400_000,
        max_output_tokens=128_000,
        degradation={
            "thinking": "drop",
            "vision": "to_text",
            "documents": "to_text",
        },
    )


class OpenAIProvider:
    """A :class:`~nexus.model.provider.Provider` for OpenAI and its dialects."""

    name = "openai"
    DEFAULT_BASE_URL = DEFAULT_BASE_URL

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        api: str = API_RESPONSES,
        capabilities: Capabilities | None = None,
        capability_source: CapabilitySource | None = None,
        transport: HTTPTransport | None = None,
        client: Any | None = None,
        http_transport: Any | None = None,
        owns_transport: bool | None = None,
        timeout: Any | None = None,
        retry: Any | None = None,
        sleep: Any | None = None,
        jitter: Any | None = None,
        environ: Mapping[str, str] | None = None,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        if api not in (API_RESPONSES, API_CHAT):
            raise ProviderError(f"openai: unknown api dialect {api!r}")
        self._api_key_spec = api_key
        self._model = model
        self._api = api
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._environ = environ
        self._default_max_tokens = default_max_tokens
        self._extra_headers = dict(extra_headers or {})
        self._capabilities = capabilities
        self._capability_source = capability_source
        self._closed = False
        if transport is not None:
            self._transport = transport
            self._owns_transport = (
                False if owns_transport is None else owns_transport
            )
        else:
            self._transport = HTTPTransport(
                base_url=self._base_url,
                client=client,
                http_transport=http_transport,
                owns_client=owns_transport,
                timeout=timeout,
                retry=retry,
                sleep=sleep,
                jitter=jitter,
                headers=self._extra_headers or None,
            )
            self._owns_transport = (
                True if owns_transport is None else owns_transport
            )

    def __repr__(self) -> str:
        # Never include the API key spec or custom headers (both may carry
        # credentials). The dialect is safe; the endpoint has any userinfo/token
        # pattern scrubbed.
        return (
            f"OpenAIProvider(model={self._model!r}, api={self._api!r}, "
            f"base_url={redact_secrets(self._base_url)!r})"
        )

    @property
    def transport(self) -> HTTPTransport:
        return self._transport

    @property
    def api(self) -> str:
        return self._api

    @classmethod
    def from_config(cls, config: object, **overrides: Any) -> OpenAIProvider:
        """Build from a loaded config v2 provider section.

        Reads credentials and the endpoint/dialect only; capabilities are
        injected by the runtime through ``capability_source`` (the registry is
        authoritative, plan section 15.5).
        """
        v2 = getattr(config, "v2", None)
        providers = getattr(v2, "providers", None) if v2 is not None else None
        section = None
        if isinstance(providers, Mapping):
            for key in ("openai",):
                if key in providers:
                    section = providers[key]
                    break
        api_key = getattr(section, "api_key", None)
        base_url = getattr(section, "base_url", None) or None
        api = getattr(section, "api", None) or API_RESPONSES
        model = None
        default = getattr(config, "model", None)
        if isinstance(default, str) and default.startswith("openai/"):
            model = default.split("/", 1)[1]
        overrides.setdefault("api_key", api_key)
        overrides.setdefault("base_url", base_url)
        overrides.setdefault("api", api)
        overrides.setdefault("model", model)
        return cls(**overrides)

    def _resolve_api_key(self) -> str:
        spec = self._api_key_spec
        if not spec:
            raise ProviderError("openai: no API key configured")
        if spec.startswith("${"):
            if spec.startswith("${env:") and spec.endswith("}"):
                var = spec[len("${env:") : -1].strip()
                if not var:
                    raise ProviderError(
                        "openai: empty ${env:...} API key reference"
                    )
                environ = os.environ if self._environ is None else self._environ
                value = environ.get(var)
                if not value:
                    raise ProviderError(
                        f"openai: environment variable {var!r} is not set"
                    )
                return value
            raise ProviderError(
                "openai: unsupported API key reference (expected ${env:VAR})"
            )
        return spec

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        headers.update(self._extra_headers)
        headers["authorization"] = f"Bearer {self._resolve_api_key()}"
        return headers

    def _endpoint(self, suffix: str) -> str:
        if self._api == API_CHAT:
            return f"{self._base_url}/chat/completions"
        return f"{self._base_url}/{suffix}"

    def _stream_endpoint(self) -> str:
        if self._api == API_CHAT:
            return f"{self._base_url}/chat/completions"
        return f"{self._base_url}/responses"

    def capabilities(self, model: str) -> Capabilities:
        base = self._capabilities
        if base is None:
            base = _fallback_capabilities(model)
        if self._capability_source is not None:
            try:
                injected = self._capability_source(model)
            except Exception:  # noqa: BLE001 - a bad source must not break a turn
                injected = None
            if injected is not None:
                # Merge the registry-owned fields over the adapter's descriptor
                # so the transport-only fields and degradation policy survive
                # and ``req.tools`` matches the router's resolved caps.
                return base.overridden_by(injected)
        return base

    def supports_count_tokens(self) -> bool:
        """Whether the configured endpoint publishes an input-token counter."""
        return self._api == API_RESPONSES and _is_openai_host(self._base_url)

    async def count_tokens(self, req: ModelRequest) -> int | None:
        """Count input tokens via ``/v1/responses/input_tokens``.

        Returns ``None`` without a network call on any endpoint other than the
        official one, or when the response carries no usable ``input_tokens``
        value, so the caller falls back to the tokenizer heuristic.
        """
        if self._closed:
            raise ProviderError("openai: provider is closed")
        if not self.supports_count_tokens():
            return None
        model = req.model or self._model
        if not model:
            return None
        body = build_count_tokens_body(req, model=model)
        response = await self._transport.request(
            "POST",
            self._endpoint("responses/input_tokens"),
            headers=self._headers(),
            json=body,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                "openai: malformed input_tokens response"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ProviderError(
                "openai: input_tokens response is not a JSON object"
            )
        value = payload.get("input_tokens")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(req)

    async def _stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        if self._closed:
            raise ProviderError("openai: provider is closed")
        model = req.model or self._model
        if not model:
            raise ProviderError("openai: no model specified")
        body = build_request_body(
            req,
            model=model,
            api=self._api,
            default_max_tokens=self._default_max_tokens,
        )
        headers = self._headers()
        usage = _UsageTotals()
        accumulator = ToolCallAccumulator()

        if self._api == API_CHAT:
            events = self._stream_chat(body, headers, model, usage, accumulator)
        else:
            events = self._stream_responses(
                body, headers, model, usage, accumulator
            )
        async for event in events:
            yield event

    async def _stream_chat(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
        model: str,
        usage: _UsageTotals,
        accumulator: ToolCallAccumulator,
    ) -> AsyncIterator[StreamEvent]:
        state = _ChatState()

        def start() -> MessageStart | None:
            if state.started:
                return None
            state.started = True
            return MessageStart(model=model, provider=self.name)

        async with aclosing(
            self._transport.aiter_sse(
                "POST",
                self._stream_endpoint(),
                headers=headers,
                json=body,
                stop_on_done=False,
            )
        ) as events:
            async for event in events:
                if not event.data:
                    continue
                if event.data.strip() == "[DONE]":
                    break
                payload = _parse_payload(event.data)
                initial = start()
                if initial is not None:
                    yield initial
                for translated in _events_for_chat_chunk(
                    payload, state, accumulator
                ):
                    yield translated
                usage.update_chat(payload.get("usage"))

        if not state.started:
            yield MessageStart(model=model, provider=self.name)
        for trailing in _finalize_pending_tools(accumulator):
            yield trailing
        if state.has_tools and state.stop_reason in (None, "end_turn"):
            state.stop_reason = "tool_use"
        if usage.has_values:
            yield usage.to_event()
        yield MessageStop(stop_reason=state.stop_reason or "end_turn")  # type: ignore[arg-type]

    async def _stream_responses(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
        model: str,
        usage: _UsageTotals,
        accumulator: ToolCallAccumulator,
    ) -> AsyncIterator[StreamEvent]:
        state = _ResponsesState()

        async with aclosing(
            self._transport.aiter_sse(
                "POST",
                self._stream_endpoint(),
                headers=headers,
                json=body,
            )
        ) as events:
            async for event in events:
                if not event.data:
                    continue
                payload = _parse_payload(event.data)
                event_type = str(payload.get("type") or event.event or "")
                if event_type == "response.created" and not state.started:
                    response = payload.get("response")
                    actual = (
                        response.get("model")
                        if isinstance(response, Mapping)
                        else None
                    )
                    started_model = str(actual) if actual else model
                    state.started = True
                    yield MessageStart(model=started_model, provider=self.name)
                for translated in _events_for_responses_event(
                    event_type, payload, state, accumulator
                ):
                    yield translated
                if event_type in (
                    "response.completed",
                    "response.incomplete",
                ):
                    response = payload.get("response")
                    if isinstance(response, Mapping):
                        usage.update_responses(response.get("usage"))

        if not state.started:
            yield MessageStart(model=model, provider=self.name)
        for trailing in _responses_flush_thinking(state):
            yield trailing
        for trailing in _finalize_pending_tools(accumulator):
            yield trailing
        if state.has_tools and state.stop_reason in (None, "end_turn"):
            state.stop_reason = "tool_use"
        if state.has_refusal and state.stop_reason is None:
            state.stop_reason = "refusal"
        if state.stop_reason is None and state.status is not None:
            state.stop_reason = normalize_responses_status(
                state.status, incomplete_reason=state.incomplete_reason
            )
        if usage.has_values:
            yield usage.to_event()
        yield MessageStop(stop_reason=state.stop_reason or "end_turn")  # type: ignore[arg-type]

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_transport:
            await self._transport.aclose()
