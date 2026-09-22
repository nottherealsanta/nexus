"""Concrete adapters under test and the shared factory registry.

Three adapters ship:

* :class:`AnthropicAdapter` — the reference HTTP adapter, driven entirely by
  ``httpx.MockTransport``. It can express every wire step: generated SSE, raw
  responses, status/retry faults, mid-stream disconnects, and park (cancel)
  streams. Nothing here touches the network.
* :class:`GeminiAdapter` — the second HTTP dialect (``generateContent``), which
  proves the case catalogue is dialect-neutral: the same normalized cases drive
  it through a different wire shape.
* :class:`ScriptedAdapter` — the in-process deterministic provider. Its "wire"
  is the normalized event list itself, which makes it the oracle for the
  harness: it proves the case catalogue is expressible without any HTTP.

Dialect and capability tags are separate axes. A future OpenAI adapter is one
class plus one ``AdapterSpec`` entry, declaring its dialect and tags.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from nexus.model.http import RetryPolicy
from nexus.model.provider import Provider
from nexus.model.providers.anthropic import AnthropicProvider
from nexus.model.providers.gemini import GeminiProvider
from nexus.model.providers.openai import OpenAIProvider
from nexus.model.providers.scripted import ScriptedProvider, Wait
from nexus.model.stream import StreamEvent

from .contract import (
    ANTHROPIC_DIALECT,
    CANCEL,
    CLOSE,
    COUNT_TOKENS,
    DEGRADATION,
    DOCUMENTS,
    GEMINI_DIALECT,
    HTTP,
    IR_DIALECT,
    OPENAI_CHAT_DIALECT,
    OPENAI_RESPONSES_DIALECT,
    SECRETS,
    STREAM,
    THINKING,
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
from .encoding import (
    encode_anthropic_sse,
    encode_gemini_sse,
    encode_openai_chat_sse,
    encode_openai_responses_sse,
)
from .ollama import OLLAMA_SPECS
from .opencode_acp import OPENCODE_ACP_SPEC

__all__ = [
    "ADAPTER_SPECS",
    "AnthropicAdapter",
    "GeminiAdapter",
    "OpenAIChatAdapter",
    "OpenAIResponsesAdapter",
    "ScriptedAdapter",
]

ANTHROPIC_BASE_URL = "https://anthropic.conformance"
GEMINI_BASE_URL = "https://gemini.conformance"
#: The official host, so the ``count_tokens`` path (which is gated to it) can be
#: exercised offline through ``httpx.MockTransport``. No network is touched.
OPENAI_RESPONSES_BASE_URL = "https://api.openai.com/v1"
OPENAI_CHAT_BASE_URL = "https://api.openai.com/v1"

ANTHROPIC_TAGS = frozenset(
    {
        STREAM,
        TOOLS,
        THINKING,
        WIRE_REQUEST,
        HTTP,
        SECRETS,
        DEGRADATION,
        VISION,
        DOCUMENTS,
        COUNT_TOKENS,
        CLOSE,
        CANCEL,
    }
)
GEMINI_TAGS = frozenset(
    {
        STREAM,
        TOOLS,
        THINKING,
        WIRE_REQUEST,
        HTTP,
        SECRETS,
        DEGRADATION,
        VISION,
        DOCUMENTS,
        COUNT_TOKENS,
        CLOSE,
        CANCEL,
    }
)
SCRIPTED_TAGS = frozenset({STREAM, TOOLS, THINKING, CLOSE, CANCEL})

#: The Responses adapter carries documents (``input_file``) and exposes the
#: official input-token counter.
OPENAI_RESPONSES_TAGS = frozenset(
    {
        STREAM,
        TOOLS,
        THINKING,
        WIRE_REQUEST,
        HTTP,
        SECRETS,
        DEGRADATION,
        VISION,
        DOCUMENTS,
        COUNT_TOKENS,
        CLOSE,
        CANCEL,
    }
)
#: Chat Completions has no document carrier and no official counter, so those
#: tags are deliberately absent and the corresponding cases are skipped.
OPENAI_CHAT_TAGS = frozenset(
    {
        STREAM,
        TOOLS,
        THINKING,
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


class _HTTPAdapter:
    """Shared HTTP behavior: a mock transport, a response queue, request capture.

    Subclasses supply the wire encoder, the dialect's ``count_tokens`` response
    shape, and the concrete provider. Every response is queued up front; the
    handler pops one per request, so a retry consumes a second queued response
    exactly like a real endpoint.
    """

    name = ""
    model = ""
    dialect = ""
    tags: frozenset[str] = frozenset()
    api_key_header = ""

    def __init__(
        self, *, api_key: str = "conformance-key", base_url: str
    ) -> None:
        self._api_key = api_key
        self._queue: list[httpx.Response] = []
        self._requests: list[CapturedRequest] = []
        self._delays: list[float] = []
        self._provider: Provider = self._build_provider(api_key, base_url)

    # -- subclass hooks ----------------------------------------------------

    def _build_provider(self, api_key: str, base_url: str) -> Provider:
        raise NotImplementedError

    def _encode(self, events: tuple[StreamEvent, ...]) -> bytes:
        raise NotImplementedError

    def _count_response(self, value: int | None) -> httpx.Response:
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
                self._queue.append(
                    httpx.Response(200, stream=_BrokenStream(prefix))
                )
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
        self._queue.append(self._count_response(value))

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


class AnthropicAdapter(_HTTPAdapter):
    """The reference adapter, backed by a ``MockTransport`` and no network."""

    name = "anthropic"
    model = "claude-conformance"
    dialect = ANTHROPIC_DIALECT
    tags = ANTHROPIC_TAGS
    api_key_header = "x-api-key"

    def __init__(
        self, *, api_key: str = "conformance-key"
    ) -> None:
        super().__init__(api_key=api_key, base_url=ANTHROPIC_BASE_URL)

    def _build_provider(self, api_key: str, base_url: str) -> AnthropicProvider:
        return AnthropicProvider(
            api_key=api_key,
            model=self.model,
            base_url=base_url,
            retry=_RETRY,
            sleep=self._sleep,
            jitter=lambda: 0.0,
            environ={},
            http_transport=httpx.MockTransport(self._handle),
        )

    def _encode(self, events: tuple[StreamEvent, ...]) -> bytes:
        return encode_anthropic_sse(events)

    def _count_response(self, value: int | None) -> httpx.Response:
        payload = {} if value is None else {"input_tokens": value}
        return httpx.Response(200, json=payload)


class GeminiAdapter(_HTTPAdapter):
    """The ``generateContent`` dialect, backed by a ``MockTransport``."""

    name = "gemini"
    model = "gemini-conformance"
    dialect = GEMINI_DIALECT
    tags = GEMINI_TAGS
    api_key_header = "x-goog-api-key"

    def __init__(
        self, *, api_key: str = "conformance-key"
    ) -> None:
        super().__init__(api_key=api_key, base_url=GEMINI_BASE_URL)

    def _build_provider(self, api_key: str, base_url: str) -> GeminiProvider:
        return GeminiProvider(
            api_key=api_key,
            model=self.model,
            base_url=base_url,
            retry=_RETRY,
            sleep=self._sleep,
            jitter=lambda: 0.0,
            environ={},
            http_transport=httpx.MockTransport(self._handle),
        )

    def _encode(self, events: tuple[StreamEvent, ...]) -> bytes:
        return encode_gemini_sse(events)

    def _count_response(self, value: int | None) -> httpx.Response:
        payload = {} if value is None else {"totalTokens": value}
        return httpx.Response(200, json=payload)


class OpenAIResponsesAdapter(_HTTPAdapter):
    """The Responses API dialect, backed by a ``MockTransport``.

    The configured ``base_url`` is the official host so the input-token counter
    path (which is gated to official endpoints) is exercised offline; no network
    request is ever made because the transport is a ``MockTransport``.
    """

    name = "openai_responses"
    model = "gpt-5-conformance"
    dialect = OPENAI_RESPONSES_DIALECT
    tags = OPENAI_RESPONSES_TAGS
    api_key_header = "authorization"

    def __init__(
        self, *, api_key: str = "conformance-key"
    ) -> None:
        super().__init__(api_key=api_key, base_url=OPENAI_RESPONSES_BASE_URL)

    def _build_provider(self, api_key: str, base_url: str) -> OpenAIProvider:
        return OpenAIProvider(
            api_key=api_key,
            model=self.model,
            base_url=base_url,
            api="responses",
            retry=_RETRY,
            sleep=self._sleep,
            jitter=lambda: 0.0,
            environ={},
            http_transport=httpx.MockTransport(self._handle),
        )

    def _encode(self, events: tuple[StreamEvent, ...]) -> bytes:
        return encode_openai_responses_sse(events)

    def _count_response(self, value: int | None) -> httpx.Response:
        payload = (
            {}
            if value is None
            else {"input_tokens": value, "object": "response.input_tokens"}
        )
        return httpx.Response(200, json=payload)


class OpenAIChatAdapter(_HTTPAdapter):
    """The Chat Completions dialect, backed by a ``MockTransport``."""

    name = "openai_chat"
    model = "gpt-4o-conformance"
    dialect = OPENAI_CHAT_DIALECT
    tags = OPENAI_CHAT_TAGS
    api_key_header = "authorization"

    def __init__(
        self, *, api_key: str = "conformance-key"
    ) -> None:
        super().__init__(api_key=api_key, base_url=OPENAI_CHAT_BASE_URL)

    def _build_provider(self, api_key: str, base_url: str) -> OpenAIProvider:
        return OpenAIProvider(
            api_key=api_key,
            model=self.model,
            base_url=base_url,
            api="chat",
            retry=_RETRY,
            sleep=self._sleep,
            jitter=lambda: 0.0,
            environ={},
            http_transport=httpx.MockTransport(self._handle),
        )

    def _encode(self, events: tuple[StreamEvent, ...]) -> bytes:
        return encode_openai_chat_sse(events)

    def _count_response(self, value: int | None) -> httpx.Response:
        raise UnsupportedStep(
            "openai_chat: Chat Completions has no official count endpoint"
        )


class ScriptedAdapter:
    """The deterministic in-process provider, replaying normalized events.

    The provider is built eagerly and scripts are appended to its live script
    list, so ``queue`` works whether it is called before *or after* ``provider``
    is first touched. (Building lazily from a snapshot silently dropped a script
    queued after the first access.)
    """

    name = "scripted"
    model = "scripted-conformance"
    dialect = IR_DIALECT
    tags = SCRIPTED_TAGS

    def __init__(self) -> None:
        self._provider = ScriptedProvider(name=self.name, model=self.model)

    # -- contract ----------------------------------------------------------

    @property
    def provider(self) -> ScriptedProvider:
        return self._provider

    def queue(self, step: WireStep) -> None:
        if isinstance(step, EventsStep):
            self._append(list(step.events))
        elif isinstance(step, ParkStep):
            self._append([*step.events, Wait()])
        elif isinstance(step, (StatusStep, FaultStep)):
            raise UnsupportedStep(
                f"scripted: transport steps ({type(step).__name__}) are HTTP-only"
            )
        else:  # pragma: no cover - exhaustive union guard
            raise UnsupportedStep(f"scripted: unsupported step {step!r}")

    def queue_count_tokens(self, value: int | None) -> None:
        raise UnsupportedStep("scripted: count_tokens is not implemented")

    def captured_requests(self) -> list[CapturedRequest]:
        return [CapturedRequest(request=req) for req in self._provider.requests]

    @property
    def retry_delays(self) -> list[float]:
        return []

    async def aclose(self) -> None:
        await self._provider.aclose()

    # -- internals ---------------------------------------------------------

    def _append(self, script: list[Any]) -> None:
        # ``ScriptedProvider`` has no public "add script"; the script list is the
        # one piece of state the queue must mutate in place.
        self._provider._scripts.append(script)


ADAPTER_SPECS: tuple[AdapterSpec, ...] = (
    AdapterSpec(
        name="anthropic",
        tags=ANTHROPIC_TAGS,
        make=AnthropicAdapter,
        dialect=ANTHROPIC_DIALECT,
        description="Anthropic Messages adapter over httpx.MockTransport",
    ),
    AdapterSpec(
        name="gemini",
        tags=GEMINI_TAGS,
        make=GeminiAdapter,
        dialect=GEMINI_DIALECT,
        description="Gemini generateContent adapter over httpx.MockTransport",
    ),
    AdapterSpec(
        name="openai_responses",
        tags=OPENAI_RESPONSES_TAGS,
        make=OpenAIResponsesAdapter,
        dialect=OPENAI_RESPONSES_DIALECT,
        description="OpenAI Responses adapter over httpx.MockTransport",
    ),
    AdapterSpec(
        name="openai_chat",
        tags=OPENAI_CHAT_TAGS,
        make=OpenAIChatAdapter,
        dialect=OPENAI_CHAT_DIALECT,
        description="OpenAI Chat Completions adapter over httpx.MockTransport",
    ),
    *OLLAMA_SPECS,
    OPENCODE_ACP_SPEC,
    AdapterSpec(
        name="scripted",
        tags=SCRIPTED_TAGS,
        make=ScriptedAdapter,
        dialect=IR_DIALECT,
        description="deterministic in-process provider (event-list wire)",
    ),
)
