"""Google Gemini ``generateContent`` streaming adapter (plan section 8, Phase 7a).

Gemini is deliberately the first non-Anthropic adapter because its wire shape is
genuinely different -- ``contents``/``Part`` instead of role/content blocks,
``systemInstruction`` instead of a top-level ``system`` string,
``functionDeclarations`` instead of ``tools``, and no first-class tool-call ids.
Building it second (after the reference adapter) is what proves the IR can carry
a second dialect rather than merely restating Anthropic's.

What this adapter owns:

* schema translation in (:func:`build_request_body`) and stream translation out
  (:meth:`GeminiProvider.stream`);
* the Gemini-specific answers to IR gaps, which are local to this file rather
  than core changes:
  - **tool-call ids.** Gemini identifies a ``functionCall`` by name; when it
    does supply an ``id`` it is echoed back on the matching
    ``functionResponse``. When it does not, the adapter mints a unique synthetic
    id (``nexus_<name>_<uuid>``, using :func:`nexus.util.new_id`) and recovers
    the function name from the id on the way back. Uniqueness holds **across
    turns**, not just within one message, so a replayed history never confuses
    two same-named calls. The name always wins from the originating ``ToolUse``
    block, so the mapping survives provider switches and replay.
  - **thought signatures (heuristic).** Gemini attaches an opaque
    ``thoughtSignature`` to parts; it is surfaced as ``ThinkingEnd.signature``
    verbatim. On the way back it is replayed only when the originating message
    was produced by this provider (``Message.meta.provider`` is ``None`` or
    ``"gemini"``) -- a signature from another vendor is dropped rather than sent
    and rejected. Because the IR stores signatures on ``Thinking`` while Gemini
    validates them on the first ``functionCall`` of a turn, the signature is
    re-attached to the first ``functionCall`` when one is present, and to the
    thought text part otherwise. A thought part with no signature is dropped.
  - **non-object ``functionCall.args``.** A malformed wire frame (``args`` that
    is not a JSON object) becomes a :class:`~nexus.errors.MalformedToolCall`,
    which the loop turns into a model-visible error result rather than silently
    executing the call with ``{}``.

Deliberate decisions, matching the reference adapter:

* **Backed by the IR, not a vendor SDK.** Only :mod:`nexus.model` contracts are
  imported, so ``httpx``/retry/SSE framing stay in :mod:`nexus.model.http`.
* **Credentials resolve at request time.** The configured value may be a
  literal or an ``${env:VAR}`` reference; it is sent in the ``x-goog-api-key``
  header, never in a URL, and never appears in ``repr`` or an error.
* **Capabilities come from an injected source** (a registry or config layer)
  when one is supplied; the in-module table is only the fallback. The registry
  is authoritative (plan section 15.5).
* **Retry stays in the shared transport.** This module never retries; a
  mid-stream failure is surfaced as :class:`~nexus.errors.ProviderError`.

Known mapping limitation: :attr:`~nexus.model.request.SamplingParams.thinking_budget`
is sent as Gemini 2.5's ``thinkingConfig.thinkingBudget``. Gemini 3 selects
reasoning effort with ``thinkingLevel`` instead, which the IR has no field for;
a Gemini 3 model that rejects ``thinkingBudget`` is handled by the loop's
capability-degradation retry (the request is resent with the budget cleared).
"""
from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import aclosing
from typing import Any

from ...errors import MalformedToolCall, ProviderError
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
    ThinkingEnd,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

__all__ = [
    "API_VERSION",
    "DEFAULT_BASE_URL",
    "GeminiProvider",
    "build_count_tokens_body",
    "build_request_body",
    "normalize_finish_reason",
]

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
API_VERSION = "v1beta"

#: Prefix that marks a tool-call id this adapter minted rather than one Gemini
#: supplied. The wire never sees it, so it cannot collide with a provider id in
#: a meaningful way; it exists so a replayed history can tell the two apart.
SYNTHETIC_CALL_PREFIX = "nexus_"

#: Gemini finish reasons mapped onto the normalized vocabulary (plan section
#: 3.3). Reasons that mean "the model declined" are ``refusal``; a malformed
#: function call is an ``error`` so the loop can surface it.
_STOP_REASONS: dict[str, str] = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "refusal",
    "RECITATION": "refusal",
    "BLOCKLIST": "refusal",
    "PROHIBITED_CONTENT": "refusal",
    "SPII": "refusal",
    "IMAGE_SAFETY": "refusal",
    "LANGUAGE": "refusal",
    "MALFORMED_FUNCTION_CALL": "error",
    "FINISH_REASON_UNSPECIFIED": "end_turn",
    "OTHER": "end_turn",
}

#: JSON Schema keywords Gemini's ``Schema`` message does not model. They are
#: stripped recursively so a spec written for Anthropic (notably
#: ``additionalProperties: false``, which every builtin tool carries) does not
#: make the whole tool declaration invalid.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "patternProperties",
        "propertyNames",
        "dependentSchemas",
        "dependentRequired",
        "prefixItems",
        "contentMediaType",
        "contentEncoding",
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "$comment",
        "definitions",
        "examples",
        "const",
        "oneOf",
        "allOf",
        "not",
        "if",
        "then",
        "else",
    }
)

#: A capability source lets a runtime inject the registry/config-derived
#: descriptor (plan section 15.5) without this adapter importing either.
CapabilitySource = Callable[[str], "Capabilities | None"]


def normalize_finish_reason(reason: object) -> str | None:
    """Map a Gemini ``finishReason`` to the normalized vocabulary, or ``None``."""
    if reason is None:
        return None
    return _STOP_REASONS.get(str(reason), "end_turn")


def _sanitize_schema(schema: object) -> object:
    """Recursively drop JSON Schema keywords Gemini's ``Schema`` rejects."""
    if not isinstance(schema, Mapping):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, Mapping):
            out[key] = {str(k): _sanitize_schema(v) for k, v in value.items()}
        elif key == "items":
            out[key] = _sanitize_schema(value)
        elif key == "anyOf" and isinstance(value, list):
            out[key] = [_sanitize_schema(v) for v in value]
        else:
            out[key] = value
    return out


def _function_declarations(tools: list[Any]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        declaration: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
        }
        raw = getattr(tool, "input_schema", None)
        parameters = _sanitize_schema(raw) if isinstance(raw, Mapping) else None
        if isinstance(parameters, Mapping) and (
            parameters.get("properties") or parameters.get("required")
        ):
            declaration["parameters"] = parameters
        declarations.append(declaration)
    return [{"functionDeclarations": declarations}]


def _request_content(req: ModelRequest) -> dict[str, Any]:
    """The fields shared by ``generateContent`` and ``countTokens``."""
    body: dict[str, Any] = {"contents": _messages_to_wire(req.messages)}
    if req.system:
        body["systemInstruction"] = {"parts": [{"text": req.system}]}
    if req.tools:
        body["tools"] = _function_declarations(list(req.tools))
    return body


def build_request_body(
    req: ModelRequest,
    *,
    model: str,
    default_max_tokens: int | None = None,
    stream: bool = True,
) -> dict[str, Any]:
    """Translate a :class:`ModelRequest` into a Gemini request body.

    ``stream=False`` only affects a caller's choice of endpoint; the body is
    otherwise identical, which is why ``countTokens`` reuses it behind
    ``generateContentRequest``.
    """
    body = _request_content(req)
    params = req.params
    generation: dict[str, Any] = {}
    if params.temperature is not None:
        generation["temperature"] = params.temperature
    max_output = params.max_output_tokens or default_max_tokens
    if max_output:
        generation["maxOutputTokens"] = int(max_output)
    if params.top_p is not None:
        generation["topP"] = params.top_p
    if params.stop_sequences:
        generation["stopSequences"] = list(params.stop_sequences)
    if params.thinking_budget is not None:
        budget = int(params.thinking_budget)
        generation["thinkingConfig"] = {
            "thinkingBudget": budget,
            "includeThoughts": budget > 0,
        }
    if generation:
        body["generationConfig"] = generation
    return body


def build_count_tokens_body(req: ModelRequest, *, model: str) -> dict[str, Any]:
    """The ``models.countTokens`` payload for ``req``.

    The endpoint accepts either ``contents`` or a full ``generateContentRequest``;
    the latter is used so ``systemInstruction`` and ``tools`` are counted too.
    Generation-only fields are omitted because they cannot change the count.
    """
    return {"generateContentRequest": _request_content(req)}


def _native(message: Message, provider: str) -> bool:
    """Whether a message's provider-specific blobs may be replayed.

    A message produced by this adapter (or built without provenance, as tests
    and imports do) is trusted; a message from another vendor is not, so its
    thought signatures are dropped rather than rejected by Gemini.
    """
    return message.meta.provider in (None, provider)


def _synthetic_call_id(name: str) -> str:
    """Mint a unique synthetic id that still carries the function name.

    ``new_id`` is a UUID, so two turns that both call ``Read`` never collide;
    the name is recoverable on replay without any per-session state.
    """
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


def _image_to_part(block: Image) -> dict[str, Any] | None:
    if block.data is not None:
        return {
            "inlineData": {
                "mimeType": block.media_type,
                "data": base64.b64encode(block.data).decode("ascii"),
            }
        }
    if block.url:
        # Gemini accepts ``fileData`` URIs (Files API, GCS, YouTube, or a public
        # https URL). An arbitrary image URL is best effort; if Gemini refuses,
        # the loop's ``vision`` degradation converts it to text.
        return {"fileData": {"mimeType": block.media_type, "fileUri": block.url}}
    return None


def _document_to_part(block: Document) -> dict[str, Any]:
    # Gemini has no document-title field; the bytes are sent inline and the
    # title is dropped.
    return {
        "inlineData": {
            "mimeType": block.media_type,
            "data": base64.b64encode(block.data).decode("ascii"),
        }
    }


def _tool_result_response(block: ToolResult) -> dict[str, Any]:
    """Render a tool result as the object Gemini's ``functionResponse`` wants.

    ``response`` must be a JSON object. Tool-result images/documents cannot be
    represented there, so they become a short note; the durable history keeps
    the original blocks.
    """
    pieces: list[str] = []
    omitted = False
    for item in block.content:
        if isinstance(item, Text):
            if item.text:
                pieces.append(item.text)
        elif isinstance(item, (Image, Document)):
            omitted = True
    if omitted:
        pieces.append("[attachment omitted: Gemini function responses are text-only]")
    response: dict[str, Any] = {"content": "\n".join(pieces)}
    if block.is_error:
        response["isError"] = True
    return response


class _WireState:
    """Mutable translation state shared across one request's messages.

    ``names`` maps a tool-use id to its function name and ``native_ids`` records
    which ids Gemini itself supplied (so they are echoed on the matching
    ``functionResponse``). Synthetic ids are self-describing, so no counter or
    cross-turn state is needed to recover a name.
    """

    __slots__ = ("names", "native_ids")

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.native_ids: set[str] = set()

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


def _content_to_parts(
    blocks: list[ContentBlock],
    state: _WireState,
    *,
    native: bool,
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    pending_signature: str | None = None
    carrier: dict[str, Any] | None = None
    for block in blocks:
        if isinstance(block, Text):
            if block.text:
                parts.append({"text": block.text})
        elif isinstance(block, Thinking):
            if not native or block.signature is None:
                # Degradation policy "drop": a foreign or unsigned thought
                # cannot be replayed to Gemini.
                continue
            pending_signature = block.signature
            if block.text:
                carrier = {"text": block.text, "thought": True}
                parts.append(carrier)
        elif isinstance(block, ToolUse):
            call: dict[str, Any] = {
                "name": block.name,
                "args": dict(block.input),
            }
            if native and not _is_synthetic(block.id):
                call["id"] = block.id
                state.native_ids.add(block.id)
            part: dict[str, Any] = {"functionCall": call}
            # Gemini validates the thought signature on the first functionCall
            # of the current turn; attach it there when one is available.
            if pending_signature is not None:
                part["thoughtSignature"] = pending_signature
                pending_signature = None
                carrier = None
            parts.append(part)
            state.names[block.id] = block.name
        elif isinstance(block, ToolResult):
            response = _tool_result_response(block)
            function_response: dict[str, Any] = {
                "name": state.name_for(block.tool_use_id),
                "response": response,
            }
            if block.tool_use_id in state.native_ids:
                function_response["id"] = block.tool_use_id
            parts.append({"functionResponse": function_response})
        elif isinstance(block, Image):
            part = _image_to_part(block)
            if part is not None:
                parts.append(part)
        elif isinstance(block, Document):
            parts.append(_document_to_part(block))
    if pending_signature is not None:
        # No functionCall consumed the signature; attach it to the thought text
        # part, or emit the empty carrier Gemini documents for this case.
        if carrier is not None:
            carrier["thoughtSignature"] = pending_signature
        else:
            parts.append(
                {"text": "", "thought": True, "thoughtSignature": pending_signature}
            )
    return parts


def _messages_to_wire(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate IR messages, coalescing adjacent same-role turns.

    Durable history is append-only and may contain consecutive user messages (a
    recovered ``ToolResult`` followed by the next user input). Gemini expects
    alternating roles, so adjacent same-role messages are merged in order; the
    durable history is never rewritten.
    """
    wire: list[dict[str, Any]] = []
    state = _WireState()
    for message in messages:
        native = _native(message, GeminiProvider.name)
        parts = _content_to_parts(message.content, state, native=native)
        if not parts:
            continue
        role = "model" if message.role == "assistant" else "user"
        if wire and wire[-1]["role"] == role:
            wire[-1]["parts"].extend(parts)
        else:
            wire.append({"role": role, "parts": parts})
    return wire


class _UsageTotals:
    """Cumulative usage, last-write-wins per field across stream chunks."""

    __slots__ = ("cache_read", "input", "output", "reasoning")

    def __init__(self) -> None:
        self.input: int | None = None
        self.output: int | None = None
        self.cache_read: int | None = None
        self.reasoning: int | None = None

    def update(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        mapping = {
            "promptTokenCount": "input",
            "candidatesTokenCount": "output",
            "cachedContentTokenCount": "cache_read",
            "thoughtsTokenCount": "reasoning",
        }
        for key, attr in mapping.items():
            value = payload.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                continue
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


def _parse_payload(event_data: str) -> dict[str, Any]:
    try:
        payload = json.loads(event_data)
    except json.JSONDecodeError as exc:
        raise ProviderError("gemini: malformed SSE JSON payload") from exc
    if not isinstance(payload, dict):
        raise ProviderError("gemini: SSE payload is not a JSON object")
    return payload


class _ChunkState:
    """Per-stream state for :func:`_events_for_chunk`."""

    __slots__ = ("has_tools", "signature", "started", "stop_reason", "thinking_open")

    def __init__(self) -> None:
        self.started = False
        self.has_tools = False
        self.thinking_open = False
        self.signature: str | None = None
        self.stop_reason: str | None = None


def _flush_thinking(state: _ChunkState) -> list[StreamEvent]:
    if not state.thinking_open and state.signature is None:
        return []
    events: list[StreamEvent] = []
    if state.signature is not None:
        events.append(ThinkingEnd(signature=state.signature))
    state.signature = None
    state.thinking_open = False
    return events


def _events_for_chunk(
    payload: Mapping[str, Any], state: _ChunkState
) -> Iterator[StreamEvent]:
    """Translate one Gemini response chunk into normalized events.

    A generator so that a ``ToolCallStart`` emitted before a malformed
    ``functionCall.args`` is observed by the collector; the subsequent
    :class:`~nexus.errors.MalformedToolCall` then becomes a model-visible error
    result rather than a silently empty call.
    """
    blocked = False
    feedback = payload.get("promptFeedback")
    if isinstance(feedback, Mapping) and feedback.get("blockReason"):
        blocked = True
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            content = candidate.get("content")
            if isinstance(content, Mapping):
                raw_parts = content.get("parts")
                if isinstance(raw_parts, list):
                    for part in raw_parts:
                        if not isinstance(part, Mapping):
                            continue
                        signature = part.get("thoughtSignature")
                        if isinstance(signature, str) and signature:
                            state.signature = signature
                        function_call = part.get("functionCall")
                        if isinstance(function_call, Mapping):
                            yield from _flush_thinking(state)
                            name = str(function_call.get("name") or "")
                            raw_args = function_call.get("args")
                            provided = function_call.get("id")
                            if isinstance(provided, str) and provided:
                                call_id = provided
                            else:
                                call_id = _synthetic_call_id(name)
                            yield ToolCallStart(id=call_id, name=name)
                            if raw_args is not None and not isinstance(
                                raw_args, Mapping
                            ):
                                raise MalformedToolCall(
                                    call_id,
                                    "functionCall.args must be a JSON object",
                                )
                            args = (
                                dict(raw_args)
                                if isinstance(raw_args, Mapping)
                                else {}
                            )
                            yield ToolCallEnd(id=call_id, input=args)
                            state.has_tools = True
                            continue
                        text = part.get("text")
                        if not isinstance(text, str) or not text:
                            continue
                        if part.get("thought"):
                            state.thinking_open = True
                            yield ThinkingDelta(text=text)
                        else:
                            yield from _flush_thinking(state)
                            yield TextDelta(text=text)
            reason = candidate.get("finishReason")
            if reason:
                normalized = normalize_finish_reason(reason)
                if normalized:
                    state.stop_reason = normalized
    if blocked and state.stop_reason is None:
        state.stop_reason = "refusal"


def _fallback_capabilities(model: str) -> Capabilities:
    """A reasonable descriptor for a Gemini model when nothing is injected.

    The registry (plan section 15.5) is authoritative whenever it is available;
    this only covers the case where no capability source was supplied. The
    degradation policies are the cross-provider contract: a foreign/unsigned
    thought drops, an image or document this model cannot take is replaced by a
    short text note, so the loop's one-retry path always changes the request.
    """
    name = (model or "").lower()
    max_output = 8192 if "1.5" in name else 65536
    return Capabilities(
        tools=True,
        parallel_tool_calls=True,
        streaming=True,
        thinking=True,
        prompt_caching=False,
        vision=True,
        documents=True,
        json_schema_strict=True,
        max_context_tokens=1_000_000,
        max_output_tokens=max_output,
        degradation={
            "thinking": "drop",
            "vision": "to_text",
            "documents": "to_text",
        },
    )


class GeminiProvider:
    """A :class:`~nexus.model.provider.Provider` for the Gemini API."""

    name = "gemini"
    DEFAULT_BASE_URL = DEFAULT_BASE_URL
    API_VERSION = API_VERSION

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
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
        default_max_tokens: int | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._api_key_spec = api_key
        self._model = model
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._environ = environ
        self._default_max_tokens = default_max_tokens
        self._extra_headers = dict(extra_headers or {})
        #: Registry/config injection (plan section 15.5). ``capability_source``
        #: wins, then a static descriptor, then the in-module fallback.
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
            f"GeminiProvider(model={self._model!r}, "
            f"base_url={redact_secrets(self._base_url)!r})"
        )

    @property
    def transport(self) -> HTTPTransport:
        return self._transport

    @classmethod
    def from_config(cls, config: object, **overrides: Any) -> GeminiProvider:
        """Build from a loaded config, tolerating the catalogue's ``google`` id.

        The registry maps models.dev's ``google`` provider onto this ``gemini``
        adapter (``nexus.model.registry.map_provider``), so a section or model
        reference may use either name. The registry itself is injected by the
        runtime via ``capability_source``; this method only reads credentials
        and endpoint data.
        """
        v2 = getattr(config, "v2", None)
        providers = getattr(v2, "providers", None) if v2 is not None else None
        section = None
        if isinstance(providers, Mapping):
            for key in ("gemini", "google"):
                if key in providers:
                    section = providers[key]
                    break
        api_key = getattr(section, "api_key", None)
        base_url = getattr(section, "base_url", None) or None
        model = None
        default = getattr(config, "model", None)
        if isinstance(default, str):
            head, _, tail = default.partition("/")
            if head in ("gemini", "google") and tail:
                model = tail
        overrides.setdefault("api_key", api_key)
        overrides.setdefault("base_url", base_url)
        overrides.setdefault("model", model)
        return cls(**overrides)

    def _resolve_api_key(self) -> str:
        spec = self._api_key_spec
        if not spec:
            raise ProviderError("gemini: no API key configured")
        if spec.startswith("${"):
            if spec.startswith("${env:") and spec.endswith("}"):
                var = spec[len("${env:") : -1].strip()
                if not var:
                    raise ProviderError("gemini: empty ${env:...} API key reference")
                environ = os.environ if self._environ is None else self._environ
                value = environ.get(var)
                if not value:
                    raise ProviderError(
                        f"gemini: environment variable {var!r} is not set"
                    )
                return value
            raise ProviderError(
                "gemini: unsupported API key reference (expected ${env:VAR})"
            )
        return spec

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        headers.update(self._extra_headers)
        # The key travels in a header, never a query string, so it cannot leak
        # through a URL that ends up in a log or error detail.
        headers["x-goog-api-key"] = self._resolve_api_key()
        return headers

    def _url(self, model: str, method: str) -> str:
        return (
            f"{self._base_url}/{self.API_VERSION}/models/{model}:{method}"
        )

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

    async def count_tokens(self, req: ModelRequest) -> int | None:
        """Count input tokens via ``models.countTokens``.

        Returns ``None`` (so the caller falls back to the heuristic) when no
        model is configured or the response carries no usable ``totalTokens``.
        """
        if self._closed:
            raise ProviderError("gemini: provider is closed")
        model = req.model or self._model
        if not model:
            return None
        body = build_count_tokens_body(req, model=model)
        response = await self._transport.request(
            "POST",
            self._url(model, "countTokens"),
            headers=self._headers(),
            json=body,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("gemini: malformed countTokens response") from exc
        if not isinstance(payload, Mapping):
            raise ProviderError("gemini: countTokens response is not a JSON object")
        value = payload.get("totalTokens")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(req)

    async def _stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        if self._closed:
            raise ProviderError("gemini: provider is closed")
        model = req.model or self._model
        if not model:
            raise ProviderError("gemini: no model specified")
        body = build_request_body(
            req, model=model, default_max_tokens=self._default_max_tokens
        )
        headers = self._headers()
        state = _ChunkState()
        usage = _UsageTotals()
        stop_reason: str | None = None

        async with aclosing(
            self._transport.aiter_sse(
                "POST",
                self._url(model, "streamGenerateContent"),
                headers=headers,
                params={"alt": "sse"},
                json=body,
            )
        ) as events:
            async for event in events:
                if not event.data:
                    continue
                payload = _parse_payload(event.data)
                if isinstance(payload.get("error"), Mapping):
                    error = payload["error"]
                    kind = str(error.get("status") or error.get("code") or "error")
                    message = str(error.get("message") or "")
                    # An in-stream error frame has not passed through the shared
                    # transport's error detail, so scrub it with the same
                    # redactor before it can reach an error, event, or log.
                    detail = redact_secrets(message.strip())[:500]
                    raise ProviderError(f"gemini: stream error {kind}: {detail}")
                if not state.started:
                    state.started = True
                    version = payload.get("modelVersion")
                    actual = str(version) if version else model
                    yield MessageStart(model=actual, provider=self.name)
                for translated in _events_for_chunk(payload, state):
                    yield translated
                usage.update(payload.get("usageMetadata"))
                if state.stop_reason:
                    stop_reason = state.stop_reason

        # A thinking signature that never preceded a tool call or text is still
        # finalized so the loop can persist the signature.
        for trailing in _flush_thinking(state):
            yield trailing
        if not state.started:
            yield MessageStart(model=model, provider=self.name)

        if state.has_tools and stop_reason in (None, "end_turn"):
            stop_reason = "tool_use"
        if usage.has_values:
            yield usage.to_event()
        yield MessageStop(stop_reason=stop_reason or "end_turn")  # type: ignore[arg-type]

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_transport:
            await self._transport.aclose()
