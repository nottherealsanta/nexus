"""The adapter-under-test contract for the provider conformance harness.

The contract is intentionally tiny. An adapter under test is a fresh,
per-test object that can

* accept queued *wire steps* describing what the provider should see next
  (normalized events to be encoded, a raw HTTP response, a transport fault, or
  a parking stream);
* expose the live :class:`~nexus.model.provider.Provider`;
* report the requests the adapter actually emitted; and
* close.

Cases never touch ``httpx`` or an adapter's internals: they queue adapter-neutral
steps. ``AnthropicAdapter`` and ``ScriptedAdapter`` translate those steps into
their own wire. A future OpenAI/Gemini adapter implements the same protocol and
inherits the whole suite.

Capability tags let one catalogue serve adapters with different transports.
A case declares the tags it requires; the harness skips it for an adapter that
does not declare them, and reports the skip rather than hiding it.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from nexus.model.provider import Provider
from nexus.model.request import ModelRequest
from nexus.model.stream import StreamEvent

# ---------------------------------------------------------------------------
# Capability tags
# ---------------------------------------------------------------------------

#: Two-way normalized stream translation: encode events to the wire and decode
#: them back. Every real adapter declares this.
STREAM = "stream"
#: The adapter can surface tool-call start/delta/end events.
TOOLS = "tools"
#: The adapter represents thinking blocks and their signatures.
THINKING = "thinking"
#: The adapter's outgoing wire request body can be captured and inspected.
WIRE_REQUEST = "wire_request"
#: The adapter speaks HTTP and therefore supports status codes, retries-based
#: faults, and mid-stream disconnects.
HTTP = "http"
#: The adapter redacts credentials from errors, reprs, and transport logs.
SECRETS = "secrets"
#: The adapter declares a cross-provider degradation policy.
DEGRADATION = "degradation"
#: The adapter can carry image content.
VISION = "vision"
#: The adapter can carry document content.
DOCUMENTS = "documents"
#: The adapter implements a real ``count_tokens`` endpoint call.
COUNT_TOKENS = "count_tokens"
#: ``aclose`` is idempotent and blocks further streaming.
CLOSE = "close"
#: A stream can be parked and released by closing the consumer.
CANCEL = "cancel"

ALL_TAGS: frozenset[str] = frozenset(
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

#: Wire dialects. A *dialect* is a wire shape, not a capability: two adapters
#: can share capabilities but differ in how a request or a stream is framed.
#: Cases that assert a dialect-specific body are gated to that dialect so an
#: Anthropic frame never has to satisfy Gemini's and vice versa; normalized
#: semantic cases declare no dialect and run everywhere.
ANTHROPIC_DIALECT = "anthropic"
GEMINI_DIALECT = "gemini"
OPENAI_RESPONSES_DIALECT = "openai_responses"
OPENAI_CHAT_DIALECT = "openai_chat"
#: The native Ollama / llama.cpp NDJSON dialect.
OLLAMA_DIALECT = "ollama"
#: The generic OpenAI-compatible chat SSE dialect as spoken by llama.cpp.
OPENAI_COMPATIBLE_DIALECT = "openai"
#: The ACP (Agent Client Protocol) subprocess agent surface. It is not a model
#: wire protocol: tool calls are the agent's own loop, so the dialect declares no
#: tools/thinking/wire-request capabilities.
OPENCODE_DIALECT = "opencode"
IR_DIALECT = "ir"

ALL_DIALECTS: frozenset[str] = frozenset(
    {
        ANTHROPIC_DIALECT,
        GEMINI_DIALECT,
        OPENAI_RESPONSES_DIALECT,
        OPENAI_CHAT_DIALECT,
        OLLAMA_DIALECT,
        OPENAI_COMPATIBLE_DIALECT,
        OPENCODE_DIALECT,
        IR_DIALECT,
    }
)

# ---------------------------------------------------------------------------
# Wire steps: what the adapter should serve next
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventsStep:
    """Encode these normalized events as one successful provider response."""

    events: tuple[StreamEvent, ...]


@dataclass(frozen=True)
class StatusStep:
    """Serve a raw HTTP response. The body is the adapter's own wire format."""

    status: int
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FaultStep:
    """Inject a transport fault.

    Known kinds: ``midstream_disconnect`` (render ``events``, then raise a read
    error) and ``malformed_wire`` (a 200 whose body is not parseable).
    """

    kind: str
    events: tuple[StreamEvent, ...] = ()


@dataclass(frozen=True)
class ParkStep:
    """Serve ``events`` and then park until the consumer is closed.

    Exercises cancellation/early-close at a streaming wait point without any
    real sleep or clock.
    """

    events: tuple[StreamEvent, ...] = ()


WireStep = EventsStep | StatusStep | FaultStep | ParkStep

# ---------------------------------------------------------------------------
# Captured requests and the adapter protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapturedRequest:
    """One request the adapter emitted, in the most faithful form available.

    HTTP adapters populate ``json`` (the decoded body) and ``headers``;
    in-process adapters populate ``request`` (the :class:`ModelRequest` they
    received). At least one of ``json``/``request`` is set.
    """

    method: str = ""
    path: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    json: dict[str, Any] | None = None
    request: ModelRequest | None = None


class UnsupportedStep(RuntimeError):
    """An adapter was asked to queue a wire step its transport cannot express."""


@runtime_checkable
class AdapterUnderTest(Protocol):
    """A live adapter instance the harness drives."""

    name: str
    model: str
    dialect: str
    tags: frozenset[str]

    @property
    def provider(self) -> Provider: ...

    def queue(self, step: WireStep) -> None: ...

    def queue_count_tokens(self, value: int | None) -> None:
        """Queue a dialect-shaped ``count_tokens`` response for the next call.

        ``None`` queues a response with no usable count so the caller falls
        back to the heuristic. Adapters without the ``count_tokens`` tag may
        raise :class:`UnsupportedStep`.
        """
        ...

    def captured_requests(self) -> list[CapturedRequest]: ...

    @property
    def retry_delays(self) -> list[float]: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class AdapterSpec:
    """A named, reusable factory for adapters under test."""

    name: str
    tags: frozenset[str]
    make: Callable[[], AdapterUnderTest]
    dialect: str
    description: str = ""

    def supports(self, required: Sequence[str] | frozenset[str]) -> bool:
        return frozenset(required) <= self.tags


__all__ = [
    "ALL_DIALECTS",
    "ALL_TAGS",
    "ANTHROPIC_DIALECT",
    "CANCEL",
    "CLOSE",
    "COUNT_TOKENS",
    "DEGRADATION",
    "DOCUMENTS",
    "GEMINI_DIALECT",
    "HTTP",
    "IR_DIALECT",
    "OLLAMA_DIALECT",
    "OPENAI_CHAT_DIALECT",
    "OPENAI_COMPATIBLE_DIALECT",
    "OPENAI_RESPONSES_DIALECT",
    "OPENCODE_DIALECT",
    "SECRETS",
    "STREAM",
    "THINKING",
    "TOOLS",
    "VISION",
    "WIRE_REQUEST",
    "AdapterSpec",
    "AdapterUnderTest",
    "CapturedRequest",
    "EventsStep",
    "FaultStep",
    "ParkStep",
    "StatusStep",
    "UnsupportedStep",
    "WireStep",
]
