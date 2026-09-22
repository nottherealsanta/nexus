"""Anthropic Messages streaming adapter (plan section 8).

This is the reference provider: schema translation in (:func:`build_request_body`),
stream translation out (:meth:`AnthropicProvider.stream`). It speaks the
Messages API over the shared :mod:`nexus.model.http` transport, so retries,
SSE framing, and connection reuse stay out of this file.

Deliberate decisions:

* **Backed by the IR, not Anthropic's SDK.** Only :mod:`nexus.model` contracts
  are imported. An adapter that cannot express a block applies the declared
  degradation policy; here unsigned thinking blocks are dropped because
  Anthropic cannot replay them without their signature.
* **Credentials resolve at request time.** The configured value may be a
  literal string or an ``${env:VAR}`` reference. It is never stored in a
  logged, repr'd, or error-visible place.
* **Usage is combined across ``message_start`` and ``message_delta``** and
  emitted once, immediately before ``message_stop``, so a consumer sees a
  single authoritative total.
"""
from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing
from typing import Any

from ...errors import ProviderError
from ..capabilities import Capabilities
from ..http import HTTPTransport
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
    ThinkingEnd,
    ToolCallAccumulator,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "AnthropicProvider",
    "build_request_body",
    "normalize_stop_reason",
]

DEFAULT_MAX_TOKENS = 4096

# Anthropic stop reasons map onto the normalized vocabulary (plan section 3.3).
# ``pause_turn`` has no normalized equivalent; it is treated as a completed turn.
_STOP_REASONS: dict[str, str] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "refusal": "refusal",
    "pause_turn": "end_turn",
}

_USAGE_KEYS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cache_write",
    "cache_read_input_tokens": "cache_read",
}


def normalize_stop_reason(reason: object) -> str | None:
    """Map an Anthropic stop reason to the normalized vocabulary, or ``None``."""
    if reason is None:
        return None
    return _STOP_REASONS.get(str(reason), "end_turn")


def _image_to_wire(block: Image) -> dict[str, Any] | None:
    if block.data is not None:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": block.media_type,
                "data": base64.b64encode(block.data).decode("ascii"),
            },
        }
    if block.url:
        return {"type": "image", "source": {"type": "url", "url": block.url}}
    return None


def _document_to_wire(block: Document) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": block.media_type,
            "data": base64.b64encode(block.data).decode("ascii"),
        },
    }
    if block.title:
        wire["title"] = block.title
    return wire


def _tool_result_content(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, Text):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, Image):
            wire = _image_to_wire(block)
            if wire is not None:
                content.append(wire)
    if not content:
        # Anthropic requires tool_result content to be non-empty.
        content.append({"type": "text", "text": ""})
    return content


def _content_to_wire(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, Text):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, Thinking):
            # Degradation policy: an unsigned thinking block cannot be replayed
            # to Anthropic, so it is dropped rather than sent and rejected.
            if block.signature is None:
                continue
            out.append(
                {
                    "type": "thinking",
                    "thinking": block.text,
                    "signature": block.signature,
                }
            )
        elif isinstance(block, ToolUse):
            out.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input),
                }
            )
        elif isinstance(block, ToolResult):
            out.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": _tool_result_content(block.content),
                    "is_error": block.is_error,
                }
            )
        elif isinstance(block, Image):
            wire = _image_to_wire(block)
            if wire is not None:
                out.append(wire)
        elif isinstance(block, Document):
            out.append(_document_to_wire(block))
    return out


def _messages_to_wire(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate messages, coalescing adjacent same-role turns.

    Durable history is append-only and may legitimately contain consecutive user
    messages (for example a recovered ``ToolResult`` message followed by the next
    user input). Anthropic requires alternating roles, so adjacent same-role
    messages are merged by concatenating their content in order — a recovered
    ``tool_result`` then the new text stays in that order. History is unchanged.
    """
    wire: list[dict[str, Any]] = []
    for message in messages:
        content = _content_to_wire(message.content)
        if not content:
            continue  # Anthropic rejects empty content arrays
        if wire and wire[-1]["role"] == message.role:
            wire[-1]["content"].extend(content)
        else:
            wire.append({"role": message.role, "content": content})
    return wire


def build_request_body(
    req: ModelRequest,
    *,
    model: str,
    default_max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Translate a :class:`ModelRequest` into an Anthropic Messages body."""
    params = req.params
    body: dict[str, Any] = {"model": model, "stream": True}
    body["max_tokens"] = int(params.max_output_tokens or default_max_tokens)
    if req.system:
        body["system"] = req.system
    body["messages"] = _messages_to_wire(req.messages)
    if req.tools:
        body["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in req.tools
        ]
    if params.thinking_budget:
        body["thinking"] = {
            "type": "enabled",
            "budget_tokens": int(params.thinking_budget),
        }
        # Anthropic requires temperature == 1 when thinking is enabled, so the
        # sampling overrides are omitted instead of sent and rejected.
    else:
        if params.temperature is not None:
            body["temperature"] = params.temperature
        if params.top_p is not None:
            body["top_p"] = params.top_p
    if params.stop_sequences:
        body["stop_sequences"] = list(params.stop_sequences)
    return body


def _max_output_for(model: str | None) -> int:
    if "haiku" in (model or "").lower():
        return 8192
    return 64000


class _UsageTotals:
    """Cumulative usage, last-write-wins per field across stream events."""

    __slots__ = ("cache_read", "cache_write", "input", "output")

    def __init__(self) -> None:
        self.input: int | None = None
        self.output: int | None = None
        self.cache_read: int | None = None
        self.cache_write: int | None = None

    def update(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        for key, attr in _USAGE_KEYS.items():
            value = payload.get(key)
            if value is None:
                continue
            try:
                setattr(self, attr, int(value))
            except (TypeError, ValueError):
                continue

    @property
    def has_values(self) -> bool:
        return any(
            getattr(self, attr) is not None
            for attr in ("input", "output", "cache_read", "cache_write")
        )

    def to_event(self) -> Usage:
        return Usage(
            input=self.input or 0,
            output=self.output or 0,
            cache_read=self.cache_read or 0,
            cache_write=self.cache_write or 0,
        )


def _parse_payload(event_data: str) -> dict[str, Any]:
    try:
        payload = json.loads(event_data)
    except json.JSONDecodeError as exc:
        raise ProviderError("anthropic: malformed SSE JSON payload") from exc
    if not isinstance(payload, dict):
        raise ProviderError("anthropic: SSE payload is not a JSON object")
    return payload


def _event_index(payload: Mapping[str, Any]) -> int:
    try:
        return int(payload.get("index"))
    except (TypeError, ValueError):
        return 0


class AnthropicProvider:
    """A :class:`~nexus.model.provider.Provider` for the Anthropic Messages API."""

    name = "anthropic"
    DEFAULT_BASE_URL = "https://api.anthropic.com"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
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
        self._api_key_spec = api_key
        self._model = model
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._environ = environ
        self._default_max_tokens = default_max_tokens
        self._extra_headers = dict(extra_headers or {})
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
        # Never include the API key spec, literal or reference.
        return f"AnthropicProvider(model={self._model!r}, base_url={self._base_url!r})"

    @property
    def transport(self) -> HTTPTransport:
        return self._transport

    @classmethod
    def from_config(cls, config: object, **overrides: Any) -> AnthropicProvider:
        """Build from a loaded :class:`nexus.config.Config` v2 provider section."""
        section = None
        v2 = getattr(config, "v2", None)
        providers = getattr(v2, "providers", None) if v2 is not None else None
        if isinstance(providers, Mapping):
            section = providers.get("anthropic")
        api_key = getattr(section, "api_key", None)
        base_url = getattr(section, "base_url", None) or None
        model = None
        default = getattr(config, "model", None)
        if isinstance(default, str) and default.startswith("anthropic/"):
            model = default.split("/", 1)[1]
        overrides.setdefault("api_key", api_key)
        overrides.setdefault("base_url", base_url)
        overrides.setdefault("model", model)
        return cls(**overrides)

    def _resolve_api_key(self) -> str:
        spec = self._api_key_spec
        if not spec:
            raise ProviderError("anthropic: no API key configured")
        if spec.startswith("${"):
            if spec.startswith("${env:") and spec.endswith("}"):
                var = spec[len("${env:") : -1].strip()
                if not var:
                    raise ProviderError(
                        "anthropic: empty ${env:...} API key reference"
                    )
                environ = os.environ if self._environ is None else self._environ
                value = environ.get(var)
                if not value:
                    raise ProviderError(
                        f"anthropic: environment variable {var!r} is not set"
                    )
                return value
            raise ProviderError(
                "anthropic: unsupported API key reference "
                "(expected ${env:VAR})"
            )
        return spec

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "anthropic-version": self.ANTHROPIC_VERSION,
        }
        headers.update(self._extra_headers)
        headers["x-api-key"] = self._resolve_api_key()
        return headers

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            tools=True,
            parallel_tool_calls=True,
            streaming=True,
            thinking=True,
            prompt_caching=True,
            vision=True,
            documents=True,
            json_schema_strict=False,
            max_context_tokens=200_000,
            max_output_tokens=_max_output_for(model),
            degradation={"thinking": "drop"},
        )

    async def count_tokens(self, req: ModelRequest) -> int | None:
        # Phase 1: the caller falls back to the shared Tokenizer heuristic.
        return None

    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(req)

    async def _stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        if self._closed:
            raise ProviderError("anthropic: provider is closed")
        model = req.model or self._model
        if not model:
            raise ProviderError("anthropic: no model specified")
        body = build_request_body(
            req, model=model, default_max_tokens=self._default_max_tokens
        )
        headers = self._headers()

        started = False
        stop_reason: str | None = None
        usage = _UsageTotals()
        blocks: dict[int, dict[str, Any]] = {}
        signatures: dict[int, list[str]] = {}
        accumulator = ToolCallAccumulator()

        def start_event() -> MessageStart | None:
            nonlocal started, model
            if started:
                return None
            started = True
            return MessageStart(model=model, provider=self.name)

        async with aclosing(
            self._transport.aiter_sse(
                "POST",
                f"{self._base_url}/v1/messages",
                headers=headers,
                json=body,
            )
        ) as events:
            async for event in events:
                if not event.data:
                    continue
                payload = _parse_payload(event.data)
                etype = str(payload.get("type") or event.event or "")

                if etype == "message_start":
                    message = payload.get("message")
                    if isinstance(message, Mapping):
                        actual = message.get("model")
                        if actual:
                            model = str(actual)
                        usage.update(message.get("usage"))
                    initial = start_event()
                    if initial is not None:
                        yield initial
                elif etype == "content_block_start":
                    initial = start_event()
                    if initial is not None:
                        yield initial
                    index = _event_index(payload)
                    block = payload.get("content_block")
                    block = block if isinstance(block, Mapping) else {}
                    btype = block.get("type")
                    if btype == "tool_use":
                        call_id = str(block.get("id") or f"toolu_{index}")
                        name = str(block.get("name") or "")
                        raw_input = block.get("input")
                        initial_input = (
                            dict(raw_input) if isinstance(raw_input, dict) else {}
                        )
                        blocks[index] = {
                            "type": "tool_use",
                            "id": call_id,
                            "input": initial_input,
                        }
                        accumulator.start(call_id, name)
                        yield ToolCallStart(id=call_id, name=name)
                    elif btype == "thinking":
                        blocks[index] = {"type": "thinking"}
                        signatures[index] = []
                        text = block.get("thinking")
                        if text:
                            yield ThinkingDelta(text=str(text))
                    else:
                        blocks[index] = {"type": "text"}
                        text = block.get("text")
                        if text:
                            yield TextDelta(text=str(text))
                elif etype == "content_block_delta":
                    index = _event_index(payload)
                    delta = payload.get("delta")
                    delta = delta if isinstance(delta, Mapping) else {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        yield TextDelta(text=str(delta.get("text") or ""))
                    elif dtype == "thinking_delta":
                        yield ThinkingDelta(text=str(delta.get("thinking") or ""))
                    elif dtype == "signature_delta":
                        signatures.setdefault(index, []).append(
                            str(delta.get("signature") or "")
                        )
                    elif dtype == "input_json_delta":
                        entry = blocks.get(index)
                        call_id = (
                            entry["id"] if entry else f"toolu_{index}"
                        )
                        partial = str(delta.get("partial_json") or "")
                        accumulator.delta(call_id, partial)
                        yield ToolCallDelta(id=call_id, partial_json=partial)
                elif etype == "content_block_stop":
                    index = _event_index(payload)
                    entry = blocks.pop(index, None)
                    if entry is None:
                        continue
                    if entry["type"] == "tool_use":
                        call_id = entry["id"]
                        authoritative = entry.get("input") or None
                        final = accumulator.finish(call_id, authoritative)
                        yield ToolCallEnd(id=call_id, input=final)
                    elif entry["type"] == "thinking":
                        signature = "".join(signatures.pop(index, []))
                        yield ThinkingEnd(signature=signature)
                elif etype == "message_delta":
                    delta = payload.get("delta")
                    reason = normalize_stop_reason(
                        delta.get("stop_reason")
                        if isinstance(delta, Mapping)
                        else None
                    )
                    if reason:
                        stop_reason = reason
                    usage.update(payload.get("usage"))
                elif etype == "message_stop" or etype == "ping":
                    pass
                elif etype == "error":
                    error = payload.get("error")
                    error = error if isinstance(error, Mapping) else {}
                    kind = error.get("type") or "error"
                    message = error.get("message") or ""
                    raise ProviderError(
                        f"anthropic: stream error {kind}: {message}"
                    )
                # Unknown event types are ignored for forward compatibility.

        if usage.has_values:
            yield usage.to_event()
        yield MessageStop(stop_reason=stop_reason or "end_turn")  # type: ignore[arg-type]

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_transport:
            await self._transport.aclose()
