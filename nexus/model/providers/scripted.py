"""Deterministic, offline provider for tests and examples (plan section 8).

``ScriptedProvider`` replays a caller-supplied script per call. It has no
network, no clock, and no randomness: the same scripts always produce the same
normalized event stream. Each call consumes exactly one script, records the
:class:`~nexus.model.request.ModelRequest` it received, and either yields
stream events, raises a scripted exception, or parks at a wait point.

Script steps
------------

* a :class:`~nexus.model.stream.StreamEvent` is yielded as-is;
* an exception instance (e.g. :class:`~nexus.errors.ProviderError`, or a
  :class:`~nexus.errors.MalformedToolCall` for malformed-args coverage) is
  raised;
* a :class:`Wait` parks the stream until cancelled or until its event is set —
  useful for exercising cancellation at a streaming wait point;
* a callable is invoked with the request and may return a single event, an
  iterable of events, or an awaitable of either.

Running out of scripts raises :class:`ScriptExhausted`, never a side effect.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from ...errors import ProviderError
from ..capabilities import Capabilities
from ..request import ModelRequest
from ..stream import (
    MessageStart,
    MessageStop,
    StreamEvent,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

__all__ = [
    "DEFAULT_MODEL",
    "ScriptExhausted",
    "ScriptedProvider",
    "Wait",
    "text_response",
    "tool_response",
]

DEFAULT_MODEL = "scripted-model"

#: A conservative-but-capable default: streaming, tools and thinking on, so
#: scripts can exercise every normalized event shape.
DEFAULT_CAPABILITIES = Capabilities(
    tools=True,
    parallel_tool_calls=True,
    streaming=True,
    thinking=True,
    max_context_tokens=200_000,
    max_output_tokens=8_192,
)


class ScriptExhausted(ProviderError):
    """The provider was asked for more calls than it has scripts for."""

    def __init__(self, index: int, available: int):
        super().__init__(
            f"scripted provider exhausted: call {index} has no script "
            f"({available} script(s) available)"
        )
        self.index = index
        self.available = available


@dataclass
class Wait:
    """A script step that blocks until cancelled or ``event`` is set."""

    event: asyncio.Event | None = None

    async def wait(self) -> None:
        if self.event is None:
            await asyncio.Event().wait()
        else:
            await self.event.wait()


#: One step of a script.
Step = StreamEvent | BaseException | Wait | Callable[..., Any]


def _as_events(value: Any) -> Iterable[StreamEvent]:
    if value is None:
        return ()
    if hasattr(value, "__struct_fields__"):  # a single msgspec StreamEvent
        return (value,)
    return value


class ScriptedProvider:
    """A deterministic :class:`~nexus.model.provider.Provider` for tests."""

    def __init__(
        self,
        *scripts: Iterable[Step],
        name: str = "scripted",
        model: str = DEFAULT_MODEL,
        capabilities: Capabilities | None = None,
    ) -> None:
        self.name = name
        self._model = model
        self._scripts: list[list[Step]] = [list(script) for script in scripts]
        self._capabilities = capabilities if capabilities is not None else DEFAULT_CAPABILITIES
        self._index = 0
        self._closed = False
        #: Requests received, in call order.
        self.requests: list[ModelRequest] = []

    # -- introspection -----------------------------------------------------

    @property
    def calls(self) -> int:
        """How many scripts have been consumed."""
        return self._index

    @property
    def remaining(self) -> int:
        return len(self._scripts) - self._index

    @property
    def closed(self) -> bool:
        return self._closed

    # -- Provider protocol -------------------------------------------------

    def capabilities(self, model: str) -> Capabilities:
        return self._capabilities

    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(req)

    async def _stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        if self._closed:
            raise ProviderError("scripted: provider is closed")
        if self._index >= len(self._scripts):
            raise ScriptExhausted(self._index, len(self._scripts))
        script = self._scripts[self._index]
        self._index += 1
        self.requests.append(req)

        for step in script:
            if isinstance(step, Wait):
                await step.wait()
                continue
            if isinstance(step, BaseException):
                raise step
            if callable(step):
                produced = step(req)
                if inspect.isawaitable(produced):
                    produced = await produced
                for event in _as_events(produced):
                    yield event
                continue
            yield step

    async def count_tokens(self, req: ModelRequest) -> int | None:
        return None

    async def aclose(self) -> None:
        self._closed = True


def text_response(
    text: str,
    *,
    model: str = DEFAULT_MODEL,
    provider: str = "scripted",
    usage: Usage | None = None,
    stop_reason: str = "end_turn",
) -> list[StreamEvent]:
    """Build a one-text-block script."""
    events: list[StreamEvent] = [MessageStart(model=model, provider=provider)]
    if text:
        events.append(TextDelta(text=text))
    if usage is not None:
        events.append(usage)
    events.append(MessageStop(stop_reason=stop_reason))  # type: ignore[arg-type]
    return events


def tool_response(
    *calls: tuple[str, str, dict],
    model: str = DEFAULT_MODEL,
    provider: str = "scripted",
    usage: Usage | None = None,
    stop_reason: str = "tool_use",
) -> list[StreamEvent]:
    """Build a tool-call script from ``(id, name, input)`` triples."""
    events: list[StreamEvent] = [MessageStart(model=model, provider=provider)]
    for call_id, name, input in calls:
        events.append(ToolCallStart(id=call_id, name=name))
        events.append(ToolCallEnd(id=call_id, input=dict(input)))
    if usage is not None:
        events.append(usage)
    events.append(MessageStop(stop_reason=stop_reason))  # type: ignore[arg-type]
    return events
