"""``MockProvider``: stateless, request-keyed replay (MOCK_PLAN §5).

Unlike :class:`~nexus.model.providers.scripted.ScriptedProvider` it keeps no call
cursor. The actor comes from the ``⟦mock …⟧`` directive in the conversation's
first user message and the step from the number of assistant messages already
present, so parallel subagents sharing one provider, reconnects, forks and
replays all continue from where the messages leave off.

It never touches the network, the clock (beyond bounded ``asyncio.sleep``
pacing) or the filesystem. A request with no directive raises
:class:`MockRouteViolation`.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator

from ...errors import MalformedToolCall, ProviderError
from ...model.capabilities import Capabilities
from ...model.message import Text
from ...model.request import ModelRequest
from ...model.stream import (
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
from .directive import Directive, format_directive, parse_directive
from .dsl import Ctx, Dyn, Fail, Hang, Scenario, Turn

__all__ = ["MOCK_PROVIDER", "MockProvider", "MockRouteViolation"]

MOCK_PROVIDER = "mock"

_CHUNK = 8
_CHUNK_DELAY = 0.012
_MAX_DYN_DEPTH = 8

_CAPABILITIES = Capabilities(
    tools=True,
    parallel_tool_calls=True,
    streaming=True,
    thinking=True,
    max_context_tokens=200_000,
    max_output_tokens=8_192,
)


class MockRouteViolation(ProviderError):
    """A request reached the mock provider without a scenario directive."""


class MockProvider:
    """Deterministic provider driven by the scenario catalogue."""

    name = MOCK_PROVIDER

    def __init__(self, catalog: dict[str, Scenario] | None = None) -> None:
        if catalog is None:
            from .scenarios import CATALOG

            catalog = CATALOG
        self._catalog = catalog
        self._closed = False
        #: Requests answered, for tests (bounded).
        self.calls = 0

    # -- Provider protocol -------------------------------------------------

    def capabilities(self, model: str) -> Capabilities:
        return _CAPABILITIES

    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(req)

    async def count_tokens(self, req: ModelRequest) -> int | None:
        return None

    async def aclose(self) -> None:
        self._closed = True

    # -- internals ---------------------------------------------------------

    @staticmethod
    def directive_of(req: ModelRequest) -> Directive | None:
        for msg in req.messages:
            if msg.role != "user":
                continue
            text = "".join(b.text for b in msg.content if isinstance(b, Text))
            found = parse_directive(text)
            if found is not None:
                return found
            break  # only the first user message names the actor
        return None

    async def _stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        if self._closed:
            raise ProviderError("mock: provider is closed")
        directive = self.directive_of(req)
        if directive is None:
            raise MockRouteViolation(
                "mock provider received a request with no ⟦mock⟧ directive; "
                "mock models only serve /mock scenarios"
            )
        scenario = self._catalog.get(directive.scenario)
        if scenario is None:
            raise MockRouteViolation(f"unknown mock scenario {directive.scenario!r}")
        self.calls += 1
        index = sum(1 for m in req.messages if m.role == "assistant")
        steps = scenario.actors.get(directive.actor)
        ctx = Ctx(scenario.name, directive.actor, index, directive.speed, directive.seed, tuple(req.messages))
        if steps is None:
            step: object = Turn(text=f"[mock] no scripted actor {directive.actor!r} in {scenario.name}.")
        elif index < len(steps):
            step = steps[index]
        else:
            step = Turn(text=f"[mock] {scenario.name}/{directive.actor} script finished.")
        rng = random.Random(f"{directive.seed}:{directive.actor}:{index}")
        model = req.model or f"{MOCK_PROVIDER}/{scenario.name}"
        for depth in range(_MAX_DYN_DEPTH):
            if isinstance(step, Dyn):
                step = step.fn(ctx)
                continue
            if isinstance(step, Fail):
                if step.recover_at_user_turn is None or ctx.user_turns < step.recover_at_user_turn:
                    if step.malformed:
                        raise MalformedToolCall("mock_malformed", step.message, raw="{not json")
                    raise ProviderError(step.message)
                if step.then is None:
                    return
                step = step.then
                continue
            break
        pace = directive.speed
        yield MessageStart(model=model, provider=MOCK_PROVIDER)
        if isinstance(step, Hang):
            async for event in self._text(step.text, pace, rng):
                yield event
            await asyncio.Event().wait()  # until cancelled
            return
        assert isinstance(step, Turn), step
        if step.think:
            async for event in self._think(step.think, pace, rng):
                yield event
        if step.text:
            async for event in self._text(step.text, pace, rng):
                yield event
        for position, item in enumerate(step.calls):
            call_id = f"mock_{directive.actor}_{index}_{position}"
            payload = dict(item.input)
            if item.child is not None:
                child = Directive(scenario.name, item.child, directive.speed, directive.seed)
                payload["prompt"] = f"{payload.get('prompt', '')}\n{format_directive(child)}"
                payload.setdefault("model", f"{MOCK_PROVIDER}/{scenario.name}")
            yield ToolCallStart(id=call_id, name=item.name)
            async for event in self._args(call_id, payload, pace):
                yield event
            yield ToolCallEnd(id=call_id, input=payload)
        used_in, used_out = step.usage or (
            max(50, sum(len(str(m.content)) for m in req.messages) // 4),
            max(10, (len(step.text) + len(step.think)) // 4 + 20 * len(step.calls)),
        )
        yield Usage(input=used_in, output=used_out)
        stop = step.stop_reason or ("tool_use" if step.calls else "end_turn")
        yield MessageStop(stop_reason=stop)  # type: ignore[arg-type]

    async def _pause(self, pace: float, rng: random.Random) -> None:
        if pace > 0:
            await asyncio.sleep(_CHUNK_DELAY * pace * (0.5 + rng.random()))
        else:
            await asyncio.sleep(0)

    async def _text(self, text: str, pace: float, rng: random.Random) -> AsyncIterator[StreamEvent]:
        for start in range(0, len(text), _CHUNK):
            yield TextDelta(text=text[start:start + _CHUNK])
            await self._pause(pace, rng)

    async def _think(self, text: str, pace: float, rng: random.Random) -> AsyncIterator[StreamEvent]:
        for start in range(0, len(text), _CHUNK * 2):
            yield ThinkingDelta(text=text[start:start + _CHUNK * 2])
            await self._pause(pace, rng)
        yield ThinkingEnd(signature="mock-signature")

    async def _args(self, call_id: str, payload: dict, pace: float) -> AsyncIterator[StreamEvent]:
        import json

        raw = json.dumps(payload)
        for start in range(0, len(raw), 48):
            yield ToolCallDelta(id=call_id, partial_json=raw[start:start + 48])
            await asyncio.sleep(0)
