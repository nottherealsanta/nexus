"""Ollama / llama.cpp local adapter (plan section 8, Phase 7b).

The local adapter is the counterpoint to the hosted ones: no credential is
required by default, the endpoint is a loopback URL, and the honest capability
answer for an arbitrary local model is *conservative*. Many small local models
do not support tool calling at all, so ``Capabilities.tools`` is ``False``
unless an injected registry (plan section 15.5) says otherwise. The loop already
adapts to that -- it drops the schemas and answers any call the model still
emits with a capability error result -- so this adapter only has to tell the
truth.

Two wire protocols are supported behind one class, selected by ``api``:

* ``"ollama"`` (default) -- the native ``POST /api/chat`` endpoint. Its stream
  is newline-delimited JSON (NDJSON), **not** SSE: every line is a complete
  ``{"message": ..., "done": bool}`` object, images ride in a per-message
  ``images`` array, and tool calls arrive atomically in ``message.tool_calls``.
* ``"openai"`` -- an OpenAI-compatible ``POST /v1/chat/completions`` SSE stream,
  for the ``llama.cpp`` server (or Ollama's own compatibility endpoint). Tool
  arguments stream as partial JSON and are accumulated with the shared
  :class:`~nexus.model.stream.ToolCallAccumulator` exactly as the OpenAI adapter
  will.

Deliberate decisions, matching the reference adapters:

* **No key by default.** A credential is optional and, when configured, is sent
  as a ``Bearer`` header. It is never placed in a URL, a ``repr``, an event, or
  a log; the shared transport redacts an echoed secret in an error detail.
* **Localhost by default, not localhost-only.** ``base_url`` defaults to
  ``http://localhost:11434`` and may be pointed at any host (a remote box or a
  reverse proxy). The default carries no credential, so an accidentally
  configured key is never sent anywhere unless the operator also changed the
  host.
* **Capabilities come from an injected source** (the registry) when one is
  supplied; the in-module fallback is the weakest common denominator rather than
  an optimistic guess.
* **Framing stays in the transport.** This module parses each NDJSON line or SSE
  data frame and nothing else.
"""
from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import aclosing
from typing import Any

from ...errors import ConfigError, MalformedToolCall, ProviderError
from ...util import new_id
from ..capabilities import Capabilities
from ..http import HTTPTransport, redact_secrets
from ..message import (
    ContentBlock,
    Document,
    Image,
    Message,
    Text,
    Thinking,
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
    ToolCallAccumulator,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "MODE_OLLAMA",
    "MODE_OPENAI",
    "SYNTHETIC_CALL_PREFIX",
    "OllamaProvider",
    "build_native_request_body",
    "build_openai_request_body",
    "build_request_body",
    "normalize_done_reason",
    "normalize_finish_reason",
]

#: The loopback endpoint the native Ollama server listens on. Not a credential
#: host: nothing secret is attached unless the operator configures a key too.
DEFAULT_BASE_URL = "http://localhost:11434"

MODE_OLLAMA = "ollama"
MODE_OPENAI = "openai"

#: Prefix that marks a tool-call id this adapter minted because the wire
#: protocol carried none (Ollama's native tool calls have no id). The wire never
#: sees it, and it carries the function name so a replayed history can recover
#: the name without per-session state.
SYNTHETIC_CALL_PREFIX = "ollama_"

#: Native Ollama ``done_reason`` values mapped onto the normalized vocabulary
#: (plan section 3.3). ``load``/``unload`` mean the generation stopped for a
#: model-lifecycle reason, which is a completed turn rather than an error.
#: ``refusal`` is accepted for forward compatibility; a refusing model normally
#: reports ``stop``.
_DONE_REASONS: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "load": "end_turn",
    "unload": "end_turn",
    "tool_calls": "tool_use",
    "refusal": "refusal",
}

#: OpenAI-compatible ``finish_reason`` values mapped onto the normalized
#: vocabulary.
_FINISH_REASONS: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

#: Default cap on tokens generated per response when the request omits one and
#: the caller configured none. ``None`` means "let the server decide".
DEFAULT_MAX_TOKENS: int | None = None

#: A capability source lets a runtime inject the registry/config-derived
#: descriptor (plan section 15.5) without this adapter importing either.
CapabilitySource = Callable[[str], "Capabilities | None"]


def _normalize_mode(api: str | None) -> str:
    value = (api or "").strip().lower()
    if value in ("", MODE_OLLAMA, "native"):
        return MODE_OLLAMA
    if value in (MODE_OPENAI, "openai_compatible", "compatible", "chat"):
        return MODE_OPENAI
    raise ConfigError(
        f"ollama: unknown api {api!r} (expected {MODE_OLLAMA!r} or {MODE_OPENAI!r})"
    )


def normalize_done_reason(reason: object) -> str | None:
    """Map a native Ollama ``done_reason`` to the normalized vocabulary."""
    if reason is None:
        return None
    return _DONE_REASONS.get(str(reason), "end_turn")


def normalize_finish_reason(reason: object) -> str | None:
    """Map an OpenAI-compatible ``finish_reason`` to the normalized vocabulary."""
    if reason is None:
        return None
    return _FINISH_REASONS.get(str(reason), "end_turn")


def _synthetic_call_id(name: str) -> str:
    """Mint a unique id that still carries the function name for replay."""
    return f"{SYNTHETIC_CALL_PREFIX}{name}_{new_id()}"


def _is_synthetic(call_id: str) -> bool:
    return call_id.startswith(SYNTHETIC_CALL_PREFIX)


def _name_from_synthetic(call_id: str) -> str | None:
    if not _is_synthetic(call_id):
        return None
    body = call_id[len(SYNTHETIC_CALL_PREFIX) :]
    name, separator, token = body.rpartition("_")
    if name and separator and token:
        return name
    return body or None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _image_base64(block: Image) -> str | None:
    """Base64 data for the native ``images`` array, or ``None`` if unavailable.

    Native Ollama accepts base64 bytes (or a local path), never a remote URL,
    so a URL-only image is dropped here; the loop's ``vision`` degradation turns
    it into a text note if the model rejects the request.
    """
    if block.data is not None:
        return base64.b64encode(block.data).decode("ascii")
    return None


def _image_data_url(block: Image) -> str | None:
    """A ``data:`` URL for the OpenAI-compatible part form, or a passthrough URL."""
    if block.data is not None:
        encoded = base64.b64encode(block.data).decode("ascii")
        return f"data:{block.media_type};base64,{encoded}"
    return block.url


def _document_note(block: Document) -> str:
    title = block.title or block.media_type
    return f"[document omitted ({title}): local providers do not accept documents]"


def _tool_use_note(block: ToolUse) -> str:
    return f"[tool call {block.name}({_json_dumps(dict(block.input))})]"


def _tool_result_text(block: ToolResult) -> str:
    pieces: list[str] = []
    omitted = False
    for item in block.content:
        if isinstance(item, Text):
            if item.text:
                pieces.append(item.text)
        else:
            omitted = True
    if omitted:
        pieces.append("[attachment omitted: tool results are text-only]")
    if not pieces:
        pieces.append("")
    return "\n".join(pieces)


def _tool_result_note(block: ToolResult) -> str:
    return f"[tool result: {_tool_result_text(block)}]"


class _WireState:
    """Mutable translation state shared across one request's messages.

    ``names`` maps a tool-use id to its function name. Native Ollama identifies
    a tool result by name rather than id, so the name is recovered from the
    originating :class:`~nexus.model.message.ToolUse` block, then from a
    synthetic id, then sanitized from whatever id is left.
    """

    __slots__ = ("names",)

    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def name_for(self, tool_use_id: str) -> str:
        name = self.names.get(tool_use_id)
        if name:
            return name
        parsed = _name_from_synthetic(tool_use_id)
        if parsed:
            return parsed
        sanitized = "".join(
            ch if ch.isalnum() or ch in "_-" else "_" for ch in tool_use_id
        )
        return sanitized or "unknown_tool"


# ---------------------------------------------------------------------------
# Native Ollama /api/chat translation
# ---------------------------------------------------------------------------


def _native_tool_definition(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _native_assistant(message: Message, state: _WireState, tools_enabled: bool):
    text_parts: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, Text):
            if block.text:
                text_parts.append(block.text)
        elif isinstance(block, ToolUse):
            if tools_enabled:
                call: dict[str, Any] = {
                    "function": {
                        "name": block.name,
                        "arguments": dict(block.input),
                    }
                }
                # Native Ollama issues no id; if one is present it is echoed so
                # an id-aware server round-trips. Synthetic ids stay off the wire.
                if block.id and not _is_synthetic(block.id):
                    call["id"] = block.id
                calls.append(call)
                state.names[block.id] = block.name
            else:
                text_parts.append(_tool_use_note(block))
        elif isinstance(block, Thinking):
            # A native local model has no replayable thinking signature, so the
            # declared degradation policy is "drop".
            continue
    entry: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts),
    }
    if calls:
        entry["tool_calls"] = calls
    return entry


def _native_user(message: Message, state: _WireState, tools_enabled: bool):
    """Translate a user turn into one or more native messages, in order.

    A durable history may put a recovered ``ToolResult`` and the next user input
    in the same message. Native Ollama needs each tool result as its own
    ``role: "tool"`` message, so they are flushed in order and the remaining
    text/images become the following user message.
    """
    out: list[dict[str, Any]] = []
    text_parts: list[str] = []
    images: list[str] = []

    def flush() -> None:
        if not text_parts and not images:
            return
        entry: dict[str, Any] = {"role": "user", "content": "\n".join(text_parts)}
        if images:
            entry["images"] = list(images)
        out.append(entry)
        text_parts.clear()
        images.clear()

    for block in message.content:
        if isinstance(block, Text):
            if block.text:
                text_parts.append(block.text)
        elif isinstance(block, Image):
            encoded = _image_base64(block)
            if encoded is not None:
                images.append(encoded)
        elif isinstance(block, Document):
            text_parts.append(_document_note(block))
        elif isinstance(block, ToolResult):
            if tools_enabled:
                flush()
                entry: dict[str, Any] = {
                    "role": "tool",
                    "content": _tool_result_text(block),
                    "tool_name": state.name_for(block.tool_use_id),
                }
                out.append(entry)
            else:
                text_parts.append(_tool_result_note(block))
        elif isinstance(block, ToolUse):
            text_parts.append(_tool_use_note(block))
    flush()
    return out


def _native_messages_to_wire(
    messages: list[Message], *, tools_enabled: bool
) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    state = _WireState()
    for message in messages:
        if message.role == "assistant":
            entry = _native_assistant(message, state, tools_enabled)
            if entry["content"] or entry.get("tool_calls"):
                wire.append(entry)
        else:
            wire.extend(_native_user(message, state, tools_enabled))
    return wire


def build_native_request_body(
    req: ModelRequest,
    *,
    model: str,
    tools_enabled: bool = True,
    default_max_tokens: int | None = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Translate a request into a native ``/api/chat`` body.

    ``tools_enabled=False`` omits the tool schemas entirely (the honest answer
    when the model has no tool support) and renders any tool block already in
    history as plain text, so the request stays valid. This is the adapter-side
    half of the loop's capability adaptation.
    """
    body: dict[str, Any] = {"model": model}
    messages: list[dict[str, Any]] = []
    if req.system:
        messages.append({"role": "system", "content": req.system})
    messages.extend(
        _native_messages_to_wire(req.messages, tools_enabled=tools_enabled)
    )
    body["messages"] = messages
    body["stream"] = True
    if tools_enabled and req.tools:
        body["tools"] = [_native_tool_definition(tool) for tool in req.tools]
    options: dict[str, Any] = {}
    params = req.params
    if params.temperature is not None:
        options["temperature"] = params.temperature
    max_output = params.max_output_tokens or default_max_tokens
    if max_output:
        options["num_predict"] = int(max_output)
    if params.top_p is not None:
        options["top_p"] = params.top_p
    if params.stop_sequences:
        options["stop"] = list(params.stop_sequences)
    if options:
        body["options"] = options
    return body


# ---------------------------------------------------------------------------
# OpenAI-compatible /v1/chat/completions translation
# ---------------------------------------------------------------------------


def _openai_tool_definition(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _openai_user_parts(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, Text):
            if block.text:
                parts.append({"type": "text", "text": block.text})
        elif isinstance(block, Image):
            url = _image_data_url(block)
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
        elif isinstance(block, Document):
            parts.append({"type": "text", "text": _document_note(block)})
        elif isinstance(block, ToolUse):
            parts.append({"type": "text", "text": _tool_use_note(block)})
    return parts


def _openai_assistant(message: Message, tools_enabled: bool) -> dict[str, Any]:
    text_parts: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, Text):
            if block.text:
                text_parts.append(block.text)
        elif isinstance(block, ToolUse):
            if tools_enabled:
                call_id = block.id or _synthetic_call_id(block.name)
                calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": _json_dumps(dict(block.input)),
                        },
                    }
                )
            else:
                text_parts.append(_tool_use_note(block))
        elif isinstance(block, Thinking):
            continue
    entry: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts),
    }
    if calls:
        entry["tool_calls"] = calls
    return entry


def _openai_messages_to_wire(
    messages: list[Message], *, tools_enabled: bool
) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "assistant":
            entry = _openai_assistant(message, tools_enabled)
            if entry["content"] or entry.get("tool_calls"):
                wire.append(entry)
            continue
        text_or_parts: list[ContentBlock] = []
        for block in message.content:
            if isinstance(block, ToolResult):
                if text_or_parts:
                    _append_openai_user(wire, text_or_parts)
                    text_or_parts = []
                if tools_enabled:
                    wire.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.tool_use_id,
                            "content": _tool_result_text(block),
                        }
                    )
                else:
                    text_or_parts.append(Text(text=_tool_result_note(block)))
            else:
                text_or_parts.append(block)
        if text_or_parts:
            _append_openai_user(wire, text_or_parts)
    return wire


def _append_openai_user(wire: list[dict[str, Any]], blocks: list[ContentBlock]) -> None:
    """Append a user message unless every block degrades to nothing."""
    parts = _openai_user_parts(blocks)
    if not parts:
        return
    if len(parts) == 1 and parts[0].get("type") == "text":
        wire.append({"role": "user", "content": parts[0]["text"]})
    else:
        wire.append({"role": "user", "content": parts})


def build_openai_request_body(
    req: ModelRequest,
    *,
    model: str,
    tools_enabled: bool = True,
    default_max_tokens: int | None = DEFAULT_MAX_TOKENS,
    include_usage: bool = True,
) -> dict[str, Any]:
    """Translate a request into an OpenAI-compatible chat-completions body."""
    body: dict[str, Any] = {"model": model}
    messages: list[dict[str, Any]] = []
    if req.system:
        messages.append({"role": "system", "content": req.system})
    messages.extend(
        _openai_messages_to_wire(req.messages, tools_enabled=tools_enabled)
    )
    body["messages"] = messages
    body["stream"] = True
    if include_usage:
        body["stream_options"] = {"include_usage": True}
    if tools_enabled and req.tools:
        body["tools"] = [_openai_tool_definition(tool) for tool in req.tools]
    params = req.params
    if params.temperature is not None:
        body["temperature"] = params.temperature
    max_output = params.max_output_tokens or default_max_tokens
    if max_output:
        body["max_tokens"] = int(max_output)
    if params.top_p is not None:
        body["top_p"] = params.top_p
    if params.stop_sequences:
        body["stop"] = list(params.stop_sequences)
    return body


def build_request_body(
    req: ModelRequest,
    *,
    model: str,
    api: str = MODE_OLLAMA,
    tools_enabled: bool = True,
    default_max_tokens: int | None = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Dispatch to the selected wire dialect's request builder."""
    if _normalize_mode(api) == MODE_OPENAI:
        return build_openai_request_body(
            req,
            model=model,
            tools_enabled=tools_enabled,
            default_max_tokens=default_max_tokens,
        )
    return build_native_request_body(
        req,
        model=model,
        tools_enabled=tools_enabled,
        default_max_tokens=default_max_tokens,
    )


# ---------------------------------------------------------------------------
# Stream decoding
# ---------------------------------------------------------------------------


def _parse_payload(data: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProviderError("ollama: malformed JSON payload") from exc
    if not isinstance(payload, dict):
        raise ProviderError("ollama: payload is not a JSON object")
    return payload


def _stream_error(payload: Mapping[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    if isinstance(error, Mapping):
        message = error.get("message") or error.get("type")
        if message:
            return str(message).strip()
    return None


def _raise_stream_error(payload: Mapping[str, Any], kind: str) -> None:
    detail = _stream_error(payload)
    if detail is None:
        return
    # An in-stream error frame has not passed through the shared transport's
    # error detail, so scrub it before it can reach an error, event, or log.
    raise ProviderError(f"ollama: {kind} error: {redact_secrets(detail)[:500]}")


def _native_tool_events(tool_calls: object) -> Iterator[StreamEvent]:
    """Yield start/end pairs for native atomic tool calls.

    Native Ollama sends complete ``arguments`` as a JSON object, but a string
    (or any non-object) is tolerated: a string must parse to a JSON object and a
    malformed value raises :class:`~nexus.errors.MalformedToolCall` *after* the
    start is surfaced, so the loop turns it into a model-visible error result.
    """
    if not isinstance(tool_calls, list):
        return
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        function = function if isinstance(function, Mapping) else {}
        name = str(function.get("name") or "")
        provided = call.get("id")
        call_id = provided if isinstance(provided, str) and provided else _synthetic_call_id(name)
        yield ToolCallStart(id=call_id, name=name)
        raw_args = function.get("arguments")
        if raw_args is None:
            yield ToolCallEnd(id=call_id, input={})
        elif isinstance(raw_args, Mapping):
            yield ToolCallEnd(id=call_id, input=dict(raw_args))
        elif isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                raise MalformedToolCall(call_id, str(exc), raw=raw_args) from exc
            if not isinstance(parsed, dict):
                raise MalformedToolCall(
                    call_id, "tool arguments must be a JSON object", raw=raw_args
                )
            yield ToolCallEnd(id=call_id, input=parsed)
        else:
            raise MalformedToolCall(
                call_id, "tool arguments must be a JSON object"
            )


class _UsageTotals:
    """Cumulative usage, last-write-wins per field across stream frames.

    Native Ollama reports ``prompt_eval_count``/``eval_count``; the
    OpenAI-compatible endpoint reports ``prompt_tokens``/``completion_tokens``
    (with cached/reasoning details); llama.cpp also emits a ``timings`` block
    with ``prompt_n``/``predicted_n``. All are accepted.
    """

    __slots__ = ("cache_read", "input", "output", "reasoning")

    def __init__(self) -> None:
        self.input: int | None = None
        self.output: int | None = None
        self.cache_read: int | None = None
        self.reasoning: int | None = None

    def update(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        self._set("input", payload.get("prompt_eval_count"))
        self._set("output", payload.get("eval_count"))
        self._set("input", payload.get("prompt_tokens"))
        self._set("output", payload.get("completion_tokens"))
        details = payload.get("prompt_tokens_details")
        if isinstance(details, Mapping):
            self._set("cache_read", details.get("cached_tokens"))
        completion = payload.get("completion_tokens_details")
        if isinstance(completion, Mapping):
            self._set("reasoning", completion.get("reasoning_tokens"))
        timings = payload.get("timings")
        if isinstance(timings, Mapping):
            self._set("input", timings.get("prompt_n"))
            self._set("output", timings.get("predicted_n"))

    def _set(self, attr: str, value: object) -> None:
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return
        setattr(self, attr, value)

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


def _fallback_capabilities(model: str) -> Capabilities:
    """The honest weakest-common-denominator descriptor for a local model.

    A catalogued model is authoritative when the registry is injected; with no
    catalogue the adapter must not claim tool support a 7B model may not have.
    The degradation policies are the cross-provider contract: thinking drops,
    an image or document the model cannot take becomes a short text note.
    """
    return Capabilities(
        tools=False,
        parallel_tool_calls=False,
        streaming=True,
        thinking=False,
        prompt_caching=False,
        vision=False,
        documents=False,
        json_schema_strict=False,
        max_context_tokens=0,
        max_output_tokens=0,
        default_max_output_tokens=DEFAULT_MAX_TOKENS or 0,
        degradation={
            "thinking": "drop",
            "vision": "to_text",
            "documents": "to_text",
        },
    )


class OllamaProvider:
    """A :class:`~nexus.model.provider.Provider` for Ollama and llama.cpp."""

    name = "ollama"
    DEFAULT_BASE_URL = DEFAULT_BASE_URL

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        api: str | None = None,
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
        default_max_tokens: int | None = DEFAULT_MAX_TOKENS,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._api_key_spec = api_key
        self._model = model
        self._api = _normalize_mode(api)
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._environ = environ
        self._default_max_tokens = default_max_tokens
        self._extra_headers = dict(extra_headers or {})
        #: Registry/config injection (plan section 15.5). ``capability_source``
        #: wins, then a static descriptor, then the conservative fallback.
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
        # Never include the API key spec, literal or reference. The endpoint has
        # any userinfo/token pattern scrubbed too.
        return (
            f"OllamaProvider(api={self._api!r}, model={self._model!r}, "
            f"base_url={redact_secrets(self._base_url)!r})"
        )

    @property
    def transport(self) -> HTTPTransport:
        return self._transport

    @property
    def api(self) -> str:
        return self._api

    @classmethod
    def from_config(cls, config: object, **overrides: Any) -> OllamaProvider:
        """Build from a loaded config's ``[providers.ollama]`` section.

        ``api`` selects the wire dialect; a section with
        ``kind = "openai_compatible"`` is treated as OpenAI-compatible even when
        ``api`` is unset, mirroring the registry's ``kind`` override.
        """
        v2 = getattr(config, "v2", None)
        providers = getattr(v2, "providers", None) if v2 is not None else None
        section = None
        if isinstance(providers, Mapping):
            section = providers.get("ollama")
        api_key = getattr(section, "api_key", None)
        base_url = getattr(section, "base_url", None) or None
        api = getattr(section, "api", None)
        kind = getattr(section, "kind", None)
        if not api and kind in ("openai_compatible", "openai", "compatible"):
            api = MODE_OPENAI
        model = None
        default = getattr(config, "model", None)
        if isinstance(default, str):
            head, _, tail = default.partition("/")
            if head == "ollama" and tail:
                model = tail
        overrides.setdefault("api_key", api_key)
        overrides.setdefault("base_url", base_url)
        overrides.setdefault("api", api)
        overrides.setdefault("model", model)
        return cls(**overrides)

    def _resolve_api_key(self) -> str | None:
        """The configured credential, or ``None`` for a keyless local endpoint."""
        spec = self._api_key_spec
        if not spec:
            return None
        if spec.startswith("${"):
            if spec.startswith("${env:") and spec.endswith("}"):
                var = spec[len("${env:") : -1].strip()
                if not var:
                    raise ProviderError("ollama: empty ${env:...} API key reference")
                environ = os.environ if self._environ is None else self._environ
                value = environ.get(var)
                if not value:
                    raise ProviderError(
                        f"ollama: environment variable {var!r} is not set"
                    )
                return value
            raise ProviderError(
                "ollama: unsupported API key reference (expected ${env:VAR})"
            )
        return spec

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        headers.update(self._extra_headers)
        key = self._resolve_api_key()
        if key:
            # Sent in a header, never a URL, so it cannot leak through a log or
            # an error detail that echoes the endpoint.
            headers["authorization"] = f"Bearer {key}"
        return headers

    def _url(self) -> str:
        if self._api == MODE_OPENAI:
            if self._base_url.endswith("/v1"):
                return f"{self._base_url}/chat/completions"
            return f"{self._base_url}/v1/chat/completions"
        return f"{self._base_url}/api/chat"

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
                # Merge the registry-owned fields over the conservative local
                # descriptor so a catalogued model's tools/vision survive while
                # the transport-only fields and degradation policy do too.
                return base.overridden_by(injected)
        return base

    async def count_tokens(self, req: ModelRequest) -> int | None:
        """Always ``None``: local endpoints expose no tokenizer for this.

        Returning ``None`` lets the caller fall back to the shared heuristic
        (plan section 7), which is the honest accounting for a local server.
        """
        if self._closed:
            raise ProviderError("ollama: provider is closed")
        return None

    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(req)

    async def _stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        if self._closed:
            raise ProviderError("ollama: provider is closed")
        model = req.model or self._model
        if not model:
            raise ProviderError("ollama: no model specified")
        tools_enabled = self.capabilities(model).tools
        if self._api == MODE_OPENAI:
            async for event in self._stream_openai(req, model, tools_enabled):
                yield event
        else:
            async for event in self._stream_native(req, model, tools_enabled):
                yield event

    async def _stream_native(
        self, req: ModelRequest, model: str, tools_enabled: bool
    ) -> AsyncIterator[StreamEvent]:
        body = build_native_request_body(
            req,
            model=model,
            tools_enabled=tools_enabled,
            default_max_tokens=self._default_max_tokens,
        )
        started = False
        has_tools = False
        stop_reason: str | None = None
        usage = _UsageTotals()

        async with aclosing(
            self._transport.aiter_lines(
                "POST", self._url(), headers=self._headers(), json=body
            )
        ) as lines:
            async for line in lines:
                payload = _parse_payload(line)
                _raise_stream_error(payload, "stream")
                if not started:
                    started = True
                    actual = payload.get("model")
                    yield MessageStart(
                        model=str(actual) if actual else model, provider=self.name
                    )
                message = payload.get("message")
                if isinstance(message, Mapping):
                    thinking = message.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        yield ThinkingDelta(text=thinking)
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        yield TextDelta(text=content)
                    for event in _native_tool_events(message.get("tool_calls")):
                        if isinstance(event, ToolCallEnd):
                            has_tools = True
                        yield event
                usage.update(payload)
                done_reason = payload.get("done_reason")
                if payload.get("done") or done_reason is not None:
                    stop_reason = normalize_done_reason(done_reason) or "end_turn"

        if not started:
            yield MessageStart(model=model, provider=self.name)
        if has_tools and stop_reason in (None, "end_turn"):
            stop_reason = "tool_use"
        if usage.has_values:
            yield usage.to_event()
        yield MessageStop(stop_reason=stop_reason or "end_turn")  # type: ignore[arg-type]

    async def _stream_openai(
        self, req: ModelRequest, model: str, tools_enabled: bool
    ) -> AsyncIterator[StreamEvent]:
        body = build_openai_request_body(
            req,
            model=model,
            tools_enabled=tools_enabled,
            default_max_tokens=self._default_max_tokens,
        )
        started = False
        has_tools = False
        stop_reason: str | None = None
        usage = _UsageTotals()
        accumulator = ToolCallAccumulator()
        open_calls: dict[int, str] = {}

        async with aclosing(
            self._transport.aiter_sse(
                "POST", self._url(), headers=self._headers(), json=body
            )
        ) as events:
            async for event in events:
                if not event.data:
                    continue
                payload = _parse_payload(event.data)
                _raise_stream_error(payload, "stream")
                if not started:
                    started = True
                    actual = payload.get("model")
                    yield MessageStart(
                        model=str(actual) if actual else model, provider=self.name
                    )
                # llama.cpp reports ``timings`` at the top level; the standard
                # OpenAI ``usage`` object rides inside the final chunk.
                usage.update(payload)
                usage.update(payload.get("usage"))
                for event_out in self._openai_choice_events(
                    payload, accumulator, open_calls
                ):
                    if isinstance(event_out, ToolCallEnd):
                        has_tools = True
                    yield event_out
                reason = self._openai_finish_reason(payload)
                if reason is not None:
                    for call_id in list(open_calls.values()):
                        final = accumulator.finish(call_id)
                        yield ToolCallEnd(id=call_id, input=final)
                        has_tools = True
                    open_calls.clear()
                    normalized = normalize_finish_reason(reason)
                    if normalized:
                        stop_reason = normalized

        # A tool call whose stream ended without a finish_reason is finalized so
        # the loop can execute it rather than dropping it.
        for call_id in list(open_calls.values()):
            final = accumulator.finish(call_id)
            yield ToolCallEnd(id=call_id, input=final)
            has_tools = True
        open_calls.clear()

        if not started:
            yield MessageStart(model=model, provider=self.name)
        if has_tools and stop_reason in (None, "end_turn"):
            stop_reason = "tool_use"
        if usage.has_values:
            yield usage.to_event()
        yield MessageStop(stop_reason=stop_reason or "end_turn")  # type: ignore[arg-type]

    @staticmethod
    def _openai_choice_events(
        payload: Mapping[str, Any],
        accumulator: ToolCallAccumulator,
        open_calls: dict[int, str],
    ) -> Iterator[StreamEvent]:
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, Mapping):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield TextDelta(text=content)
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                yield ThinkingDelta(text=reasoning)
            tool_calls = delta.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for raw in tool_calls:
                if not isinstance(raw, Mapping):
                    continue
                index = raw.get("index")
                index = index if isinstance(index, int) else len(open_calls)
                function = raw.get("function")
                function = function if isinstance(function, Mapping) else {}
                call_id = open_calls.get(index)
                if call_id is None:
                    name = str(function.get("name") or "")
                    provided = raw.get("id")
                    call_id = (
                        provided
                        if isinstance(provided, str) and provided
                        else _synthetic_call_id(name)
                    )
                    open_calls[index] = call_id
                    accumulator.start(call_id, name)
                    yield ToolCallStart(id=call_id, name=name)
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    accumulator.delta(call_id, arguments)
                    yield ToolCallDelta(id=call_id, partial_json=arguments)

    @staticmethod
    def _openai_finish_reason(payload: Mapping[str, Any]) -> str | None:
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return None
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            reason = choice.get("finish_reason")
            if isinstance(reason, str) and reason:
                return reason
        return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_transport:
            await self._transport.aclose()
