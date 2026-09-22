"""Conformance adapter for the Ollama / llama.cpp local provider (Phase 7b).

This is a *separate* adapter file: it adds the local adapter to the reusable
conformance harness without editing any of the shared conformance modules. The
same normalized case catalogue drives it, and every wire dialect is translated
here:

* :func:`encode_ollama_ndjson` renders a normalized event plan as the native
  ``/api/chat`` NDJSON stream (one full JSON object per line, atomic tool calls,
  ``done_reason`` / ``prompt_eval_count`` / ``eval_count``).
* :func:`encode_openai_sse` renders it as an OpenAI-compatible
  ``/v1/chat/completions`` SSE stream (partial-JSON tool arguments, ``usage`` in
  the final chunk, ``[DONE]`` sentinel) for llama.cpp.

Both adapters run entirely over ``httpx.MockTransport``; nothing touches the
network. Capability tags match what the local wire can actually express: native
Ollama has no thinking signature and no document type, so ``THINKING`` and
``DOCUMENTS`` are deliberately absent and those cases report a skip.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import httpx

from nexus.model.http import RetryPolicy
from nexus.model.provider import Provider
from nexus.model.providers.ollama import (
    DEFAULT_BASE_URL,
    MODE_OLLAMA,
    MODE_OPENAI,
    OllamaProvider,
)
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

from .contract import (
    CANCEL,
    CLOSE,
    DEGRADATION,
    HTTP,
    SECRETS,
    STREAM,
    TOOLS,
    VISION,
    WIRE_REQUEST,
    AdapterSpec,
    CapturedRequest,
    EventsStep,
    FaultStep,
    ParkStep,
    StatusStep,
    UnsupportedStep,
    WireStep,
)

__all__ = [
    "OLLAMA_DIALECT",
    "OLLAMA_SPECS",
    "OPENAI_DIALECT",
    "OllamaAdapter",
    "OllamaOpenAIAdapter",
    "encode_ollama_ndjson",
    "encode_openai_sse",
]

OLLAMA_DIALECT = "ollama"
OPENAI_DIALECT = "openai"
DEFAULT_MODEL = "qwen3:32b"

#: What the local wire can express. ``THINKING`` (no signature) and
#: ``DOCUMENTS`` (no document type) are intentionally missing, and the adapter
#: has no count_tokens endpoint, so those cases are reported as skips.
OLLAMA_TAGS = frozenset(
    {
        STREAM,
        TOOLS,
        WIRE_REQUEST,
        HTTP,
        SECRETS,
        DEGRADATION,
        VISION,
        CLOSE,
        CANCEL,
    }
)

_RETRY = RetryPolicy(max_attempts=3, base_delay=0.01, jitter_ratio=0.0)

#: Normalized stop reason -> native ``done_reason``.
_OLLAMA_DONE: dict[str, str] = {
    "end_turn": "stop",
    "tool_use": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "refusal": "refusal",
    "error": "stop",
}

#: Normalized stop reason -> OpenAI-compatible ``finish_reason``.
_OPENAI_FINISH: dict[str, str] = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "refusal": "content_filter",
    "error": "content_filter",
}


# ---------------------------------------------------------------------------
# Native Ollama NDJSON encoder
# ---------------------------------------------------------------------------


def _ndjson_line(
    model: str,
    *,
    content: str | None = None,
    thinking: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    done: bool = False,
    done_reason: str | None = None,
    usage: Usage | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if thinking is not None:
        message["thinking"] = thinking
    if tool_calls:
        message["tool_calls"] = tool_calls
    line: dict[str, Any] = {
        "model": model,
        "created_at": "2026-01-01T00:00:00Z",
        "message": message,
        "done": done,
    }
    if done_reason is not None:
        line["done_reason"] = done_reason
    if usage is not None:
        line["prompt_eval_count"] = usage.input
        line["eval_count"] = usage.output
    return line


def encode_ollama_ndjson(
    events: Sequence[StreamEvent], *, model: str = DEFAULT_MODEL
) -> bytes:
    """Render a normalized event plan as native ``/api/chat`` NDJSON."""
    ordered = list(events)
    start = next((e for e in ordered if isinstance(e, MessageStart)), None)
    actual = (start.model if start else None) or model
    usage = next((e for e in ordered if isinstance(e, Usage)), None)
    has_stop = any(isinstance(e, MessageStop) for e in ordered)
    stop_reason = next(
        (e.stop_reason for e in ordered if isinstance(e, MessageStop)), "end_turn"
    )

    lines: list[dict[str, Any]] = [_ndjson_line(actual, content="", done=False)]
    pending: tuple[str, str] | None = None
    for event in ordered:
        if isinstance(event, (MessageStart, Usage, MessageStop)):
            continue
        if isinstance(event, TextDelta):
            lines.append(_ndjson_line(actual, content=event.text, done=False))
        elif isinstance(event, ThinkingDelta):
            lines.append(_ndjson_line(actual, thinking=event.text, done=False))
        elif isinstance(event, ThinkingEnd):
            continue  # native Ollama has no thinking signature
        elif isinstance(event, ToolCallStart):
            pending = (event.id, event.name)
        elif isinstance(event, ToolCallDelta):
            continue  # native tool calls are atomic
        elif isinstance(event, ToolCallEnd):
            call_id, name = pending or (event.id, "")
            call: dict[str, Any] = {
                "function": {"name": name, "arguments": dict(event.input)}
            }
            if call_id:
                call["id"] = call_id
            lines.append(_ndjson_line(actual, tool_calls=[call], done=False))
            pending = None

    if has_stop:
        lines.append(
            _ndjson_line(
                actual,
                content="",
                done=True,
                done_reason=_OLLAMA_DONE.get(stop_reason, "stop"),
                usage=usage,
            )
        )
    return ("\n".join(json.dumps(line, separators=(",", ":")) for line in lines) + "\n").encode(
        "utf-8"
    )


# ---------------------------------------------------------------------------
# OpenAI-compatible SSE encoder
# ---------------------------------------------------------------------------


def _sse_chunk(
    model: str,
    *,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    usage: Usage | None = None,
) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "model": model,
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ],
    }
    if usage is not None:
        chunk["usage"] = {
            "prompt_tokens": usage.input,
            "completion_tokens": usage.output,
        }
    return chunk


def encode_openai_sse(
    events: Sequence[StreamEvent], *, model: str = DEFAULT_MODEL
) -> bytes:
    """Render a normalized event plan as an OpenAI-compatible chat SSE body."""
    ordered = list(events)
    start = next((e for e in ordered if isinstance(e, MessageStart)), None)
    actual = (start.model if start else None) or model
    usage = next((e for e in ordered if isinstance(e, Usage)), None)
    has_stop = any(isinstance(e, MessageStop) for e in ordered)
    stop_reason = next(
        (e.stop_reason for e in ordered if isinstance(e, MessageStop)), "end_turn"
    )

    parts: list[str] = []

    def emit(chunk: dict[str, Any]) -> None:
        parts.append(f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n")

    emit(_sse_chunk(actual, delta={"role": "assistant", "content": ""}))
    indices: dict[str, int] = {}
    for event in ordered:
        if isinstance(event, (MessageStart, Usage, MessageStop)):
            continue
        if isinstance(event, TextDelta):
            emit(_sse_chunk(actual, delta={"content": event.text}))
        elif isinstance(event, ThinkingDelta):
            emit(_sse_chunk(actual, delta={"reasoning_content": event.text}))
        elif isinstance(event, ThinkingEnd):
            continue
        elif isinstance(event, ToolCallStart):
            index = len(indices)
            indices[event.id] = index
            emit(
                _sse_chunk(
                    actual,
                    delta={
                        "tool_calls": [
                            {
                                "index": index,
                                "id": event.id,
                                "type": "function",
                                "function": {"name": event.name, "arguments": ""},
                            }
                        ]
                    },
                )
            )
        elif isinstance(event, ToolCallDelta):
            index = indices.get(event.id, 0)
            emit(
                _sse_chunk(
                    actual,
                    delta={
                        "tool_calls": [
                            {
                                "index": index,
                                "function": {"arguments": event.partial_json},
                            }
                        ]
                    },
                )
            )
        elif isinstance(event, ToolCallEnd):
            # Arguments already streamed as deltas; the finish reason finalizes.
            continue

    if has_stop:
        emit(
            _sse_chunk(
                actual,
                delta={},
                finish_reason=_OPENAI_FINISH.get(stop_reason, "stop"),
                usage=usage,
            )
        )
        parts.append("data: [DONE]\n\n")
    return "".join(parts).encode("utf-8")


# ---------------------------------------------------------------------------
# Adapters under test
# ---------------------------------------------------------------------------


class _BrokenStream(httpx.AsyncByteStream):
    """Yields ``prefix``, then fails like a dropped connection."""

    def __init__(self, prefix: bytes) -> None:
        self._prefix = prefix

    async def __aiter__(self):  # type: ignore[override]
        if self._prefix:
            yield self._prefix
        raise httpx.ReadError("conformance: midstream disconnect")

    async def aclose(self) -> None:
        return None


class _ParkStream(httpx.AsyncByteStream):
    """Yields ``prefix``, then parks until the response is closed."""

    def __init__(self, prefix: bytes) -> None:
        self._prefix = prefix
        self._release = asyncio.Event()

    async def __aiter__(self):  # type: ignore[override]
        if self._prefix:
            yield self._prefix
        await self._release.wait()

    async def aclose(self) -> None:
        self._release.set()


class _LocalAdapter:
    """Shared MockTransport plumbing for the two local wire dialects."""

    name = "ollama"
    model = DEFAULT_MODEL
    dialect = OLLAMA_DIALECT
    tags = OLLAMA_TAGS
    mode = MODE_OLLAMA
    api_key_header = "authorization"

    def __init__(self, *, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._queue: list[httpx.Response] = []
        self._requests: list[CapturedRequest] = []
        self._delays: list[float] = []
        self._provider: Provider = OllamaProvider(
            api_key=api_key,
            model=self.model,
            api=self.mode,
            base_url=DEFAULT_BASE_URL,
            retry=_RETRY,
            sleep=self._sleep,
            jitter=lambda: 0.0,
            environ={},
            http_transport=httpx.MockTransport(self._handle),
        )

    # -- subclass hook -----------------------------------------------------

    def _encode(self, events: tuple[StreamEvent, ...]) -> bytes:
        raise NotImplementedError

    # -- contract ----------------------------------------------------------

    @property
    def provider(self) -> Provider:
        return self._provider

    def queue(self, step: WireStep) -> None:
        if isinstance(step, EventsStep):
            self._queue.append(httpx.Response(200, content=self._encode(step.events)))
        elif isinstance(step, StatusStep):
            self._queue.append(
                httpx.Response(
                    step.status,
                    content=step.body,
                    headers=dict(step.headers),
                )
            )
        elif isinstance(step, FaultStep):
            if step.kind == "midstream_disconnect":
                prefix = self._encode(step.events) if step.events else b""
                self._queue.append(httpx.Response(200, stream=_BrokenStream(prefix)))
            elif step.kind == "malformed_wire":
                self._queue.append(
                    httpx.Response(200, content=b"data: {not json}\n\n")
                )
            else:
                raise UnsupportedStep(f"{self.name}: unknown fault {step.kind!r}")
        elif isinstance(step, ParkStep):
            self._queue.append(
                httpx.Response(200, stream=_ParkStream(self._encode(step.events)))
            )
        else:  # pragma: no cover - exhaustive union guard
            raise UnsupportedStep(f"{self.name}: unsupported step {step!r}")

    def queue_count_tokens(self, value: int | None) -> None:
        raise UnsupportedStep("ollama: count_tokens uses the shared heuristic")

    def captured_requests(self) -> list[CapturedRequest]:
        return list(self._requests)

    @property
    def retry_delays(self) -> list[float]:
        return list(self._delays)

    async def aclose(self) -> None:
        await self._provider.aclose()

    # -- internals ---------------------------------------------------------

    async def _sleep(self, delay: float) -> None:
        self._delays.append(delay)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] | None = None
        if request.content:
            try:
                parsed = json.loads(request.content)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                body = parsed
        self._requests.append(
            CapturedRequest(
                method=request.method,
                path=request.url.path,
                headers=dict(request.headers),
                json=body,
            )
        )
        if self._queue:
            return self._queue.pop(0)
        return httpx.Response(
            400, text="conformance: no queued response for this request"
        )


class OllamaAdapter(_LocalAdapter):
    """The native ``/api/chat`` NDJSON dialect, over a MockTransport."""

    name = "ollama"
    dialect = OLLAMA_DIALECT
    tags = OLLAMA_TAGS
    mode = MODE_OLLAMA

    def _encode(self, events: tuple[StreamEvent, ...]) -> bytes:
        return encode_ollama_ndjson(events, model=self.model)


class OllamaOpenAIAdapter(_LocalAdapter):
    """The OpenAI-compatible llama.cpp dialect, over a MockTransport."""

    name = "ollama-openai"
    dialect = OPENAI_DIALECT
    tags = OLLAMA_TAGS
    mode = MODE_OPENAI

    def _encode(self, events: tuple[StreamEvent, ...]) -> bytes:
        return encode_openai_sse(events, model=self.model)


OLLAMA_SPECS: tuple[AdapterSpec, ...] = (
    AdapterSpec(
        name="ollama",
        tags=OLLAMA_TAGS,
        make=OllamaAdapter,
        dialect=OLLAMA_DIALECT,
        description="native Ollama /api/chat NDJSON over httpx.MockTransport",
    ),
    AdapterSpec(
        name="ollama-openai",
        tags=OLLAMA_TAGS,
        make=OllamaOpenAIAdapter,
        dialect=OPENAI_DIALECT,
        description="llama.cpp OpenAI-compatible SSE over httpx.MockTransport",
    ),
)
