"""Normalized conversation and stream types shared by every provider (PLAN.md step 1).

A conversation is a list of `Message`s made of ordered parts. Each part a
provider produced keeps the exact wire item it came from (`raw`) and the
provider that made it (`origin`), so a replay to the same provider is lossless
(reasoning `encrypted_content`, item ids) while other providers use the
normalized fields.

A provider's `stream()` yields `Event`s. Wire events (`WireRequest`,
`WireEvent`) are always emitted so a client can show exactly what went out and
what came back, token by token.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# -- Conversation -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Text:
    text: str
    origin: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Reasoning:
    """Visible reasoning summary; `raw` carries the opaque state to send back."""

    summary: str
    origin: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text exactly as the model produced it
    origin: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Media:
    """An image a tool returned, kept as a reference to a stored artifact (PLAN K8).

    Stored and sent to clients without bytes. `Agent.request()` fills `data_url` just before a provider sees
    the message, so a dialect reads `data_url` and never touches the artifact store. `data_url` None means the
    image is not being sent (see docs/llm-providers.md, "Tool-result media").
    """

    artifact: str
    mime: str
    name: str = ""
    data_url: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    output: str
    is_error: bool = False
    media: tuple[Media, ...] = ()  # images that come after `output`, in order


@dataclass(frozen=True, slots=True)
class Image:
    """User image, stored with its bytes for durable replay and preview."""

    data_url: str
    name: str = "image.png"


Part = Text | Reasoning | ToolCall | ToolResult | Image


def tool_images(part: ToolResult) -> list[Media]:
    """The images of a tool result a provider should send: only those whose bytes were filled in."""
    return [m for m in part.media if m.data_url]


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["user", "assistant", "tool"]
    parts: tuple[Part, ...]


def has_images(messages: Any) -> bool:
    """Whether a request carries any image, a user's or a tool's (Copilot wants a header then)."""
    return any(isinstance(p, Image) or (isinstance(p, ToolResult) and tool_images(p))
               for m in messages for p in m.parts)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Request:
    model: str
    instructions: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    reasoning_effort: str | None = None
    conversation_id: str | None = None  # stable per conversation; used for prompt caching


# -- Stream events ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WireRequest:
    """The HTTP request as sent. Credential headers are redacted."""

    method: str
    url: str
    headers: dict[str, str]
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WireResponse:
    status: int
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class WireEvent:
    """One server-sent event exactly as received (data parsed as JSON)."""

    name: str
    data: Any


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ToolArgsDelta:
    id: str
    delta: str


@dataclass(frozen=True, slots=True)
class PartDone:
    """A finished part, ready to append to the assistant message."""

    part: Part


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Done:
    stop: Literal["end_turn", "tool_calls", "max_tokens", "other"]
    response_id: str | None = None
    detail: str | None = None


Event = (
    WireRequest
    | WireResponse
    | WireEvent
    | TextDelta
    | ReasoningDelta
    | ToolCallStarted
    | ToolArgsDelta
    | PartDone
    | Usage
    | Done
)


class ProviderError(Exception):
    """A provider failure with a short, credential-free message.

    `phase` says where the failure happened (`transport`, `response`, `stream`);
    retries are allowed only before response stream events. `retry_after` is the
    provider's requested wait in seconds, when it sent a valid one.
    """

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None,
                 phase: str = "stream", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.phase = phase
        self.retry_after = retry_after

