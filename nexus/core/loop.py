"""The agentic loop (plan section 4), written against protocols only.

``nexus.core.loop`` is the seam the plan calls out explicitly: it drives a turn
by talking to a :class:`SessionView`, a :class:`ContextAssembler`, a
:class:`ProviderResolver`, and an :class:`EventSink`, and it imports **no**
concrete session, context, runtime, manager, or provider adapter. Everything
below the loop (L0 ``errors``/``events``, L1 ``model``, core ``turn``/``cancel``)
is fair game; everything above it is reached through a protocol.

Phase 1 scope
-------------

This is the no-tools phase. The loop assembles one structured
:class:`~nexus.model.request.ModelRequest` per iteration, streams the provider's
normalized events, collects assistant content blocks in wire order, and persists
the assistant message **before** any tool handling. There is no dispatcher and
there are deliberately no ``tools``/``permissions`` modules referenced here.

When the model asks for a tool, valid calls are answered with one persisted user
message of error ``ToolResult`` blocks explaining that tools are unavailable
until Phase 2. The turn then continues to the next iteration so the model can
self-correct in text; the number of iterations is bounded by
:attr:`~nexus.core.turn.TurnLimits.max_iterations`. Malformed tool arguments are
converted into a durable, model-visible error result and count against
``malformed_budget``. Nothing is ever executed.

Persistence contract
--------------------

The loop stamps every :class:`~nexus.events.Event` with the session and turn id
but leaves ``seq`` at ``0``. The :class:`EventSink` is expected to **persist
first** (letting the session store assign the monotonic ``seq``) and fan out
second, so the returned record's ``seq`` is the authoritative write ordering.
Messages are persisted through :meth:`SessionView.append_message`, which returns
a record carrying the store-assigned ``seq``.

Concurrency contract
--------------------

The loop owns exactly one turn lease. If no lease is supplied it calls
``session.begin_turn(...)``; either way it releases the lease on **every** exit
path, including cancellation and unexpected failure. Cancellation is
cooperative: the lease's :class:`~nexus.core.cancel.CancelToken` is raced
against every stream ``__anext__`` so a provider blocked at a wait point can be
interrupted.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

import msgspec

from ..errors import MalformedToolCall, OperationCancelled, ProviderError
from ..events import Event
from ..model.message import (
    ContentBlock,
    Message,
    MessageMeta,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)
from ..model.provider import Provider, ResolvedModel
from ..model.request import ModelRequest
from ..model.stream import (
    MessageStop,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ThinkingEnd,
    ToolCallAccumulator,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
)
from ..model.stream import Usage as StreamUsage
from .cancel import CancelToken
from .turn import TurnLimits, TurnOutcome, TurnState, TurnUsage

__all__ = [
    "DEFAULT_MALFORMED_BUDGET",
    "ContextAssembler",
    "EventSink",
    "ProviderResolver",
    "ResolvedModel",
    "SessionView",
    "TurnLeaseView",
    "run_turn",
]

#: How many malformed tool calls a single turn tolerates before the harness
#: gives up. A model that cannot emit valid JSON repeatedly is a harness-level
#: failure, not a tool-level one.
DEFAULT_MALFORMED_BUDGET = 4

#: Provider stop reasons that may terminate a turn without tool handling.
_COMPLETION_REASONS = frozenset({"end_turn", "max_tokens", "stop_sequence", "refusal"})

#: The Phase 1 answer for a valid tool call: honest, actionable, and testable.
_UNAVAILABLE_TEXT = (
    "Tool execution is not available until Phase 2. The call to {name!r} was "
    "not run. Respond to the user in plain text; do not issue further tool calls."
)


# ---------------------------------------------------------------------------
# Structural protocols (the only way the loop reaches the world above it)
# ---------------------------------------------------------------------------


class TurnLeaseView(Protocol):
    """The subset of a session turn lease the loop needs.

    Matches :class:`nexus.session.session.TurnLease` structurally. ``state`` is
    read/write because the loop advances it and reflects it back on the lease.
    """

    turn_id: str
    state: TurnState
    cancel_token: CancelToken
    limits: TurnLimits | None

    def release(self) -> None: ...


class SessionView(Protocol):
    """Structured history plus the append/lease seams (plan section 5.1).

    Implemented structurally by :class:`nexus.session.session.Session`; the loop
    never imports that class. ``messages`` is the structured history *including*
    the just-persisted user message, which is what the assembler reads.
    """

    @property
    def id(self) -> str: ...

    @property
    def messages(self) -> list[Message]: ...

    def append_message(
        self, message: Message, *, seq: int | None = None
    ) -> object: ...

    def begin_turn(
        self,
        *,
        turn_id: str | None = None,
        limits: TurnLimits | None = None,
    ) -> TurnLeaseView: ...


class ContextAssembler(Protocol):
    """Turns structured history into a provider-ready request.

    The contract for the context/runtime packet: an object with
    ``assemble(session)`` returning (or awaiting into) a
    :class:`~nexus.model.request.ModelRequest`. ``session.messages`` already
    contains the current user turn, so "current input" needs no separate
    argument.
    """

    def assemble(
        self, session: SessionView, /
    ) -> ModelRequest | Awaitable[ModelRequest]: ...


class ProviderResolver(Protocol):
    """Resolves a request to a concrete provider/model/capability triple.

    The contract for the runtime/router packet: ``resolve(request)`` returns a
    :class:`ResolvedModel`. The plan's ``provider_for(request)`` unpacking is
    preserved because :class:`ResolvedModel` is a tuple.
    """

    def resolve(self, request: ModelRequest, /) -> ResolvedModel: ...


class EventSink(Protocol):
    """Persist-then-fan-out sink for the public event stream.

    The runtime adapter is expected to append the event to the session store
    (which assigns ``seq``) and then publish it to the UI bus. ``emit`` may be
    synchronous or asynchronous: an async sink lets the runtime apply
    backpressure without dropping events, and the loop awaits it. An object
    exposing ``append_event(event)`` — such as the real session handle — is also
    accepted, so a runtime can pass the session directly.
    """

    def emit(self, event: Event) -> object | Awaitable[object]: ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _Emitter:
    """Stamps session/turn ids onto events and forwards them to the sink.

    ``emit`` is asynchronous so an async sink can apply backpressure; sync sinks
    (including test fakes) are awaited transparently via :func:`_maybe_await`.
    """

    __slots__ = ("_session", "_sink", "_turn")

    def __init__(self, sink: EventSink, session_id: str, turn_id: str) -> None:
        self._sink = sink
        self._session = session_id
        self._turn = turn_id

    async def emit(self, event_type: str, data: dict | None = None) -> object:
        event = Event(
            type=event_type,
            data=dict(data or {}),
            session=self._session,
            turn=self._turn,
        )
        emit = getattr(self._sink, "emit", None)
        if emit is not None:
            return await _maybe_await(emit(event))
        append = getattr(self._sink, "append_event", None)
        if append is not None:
            return await _maybe_await(append(event))
        return await _maybe_await(self._sink(event))  # type: ignore[operator]


class _BlockCollector:
    """Collects normalized stream events into ordered assistant content blocks.

    Text and thinking runs are coalesced; tool calls are accumulated through the
    shared :class:`~nexus.model.stream.ToolCallAccumulator` and finalized in the
    order their blocks began, which is what keeps interleaved tool calls in wire
    order. An empty authoritative ``input`` is treated as *no* authoritative
    input, so malformed streamed JSON is surfaced rather than silently masked.
    """

    __slots__ = ("_accumulator", "_items", "_names", "_signature", "_text", "_thinking")

    def __init__(self) -> None:
        self._accumulator = ToolCallAccumulator()
        self._names: dict[str, str] = {}
        self._items: list[tuple] = []
        self._text: list[str] = []
        self._thinking: list[str] = []
        self._signature: str | None = None

    def _flush_text(self) -> None:
        if self._text:
            self._items.append(("text", "".join(self._text)))
            self._text = []

    def _flush_thinking(self) -> None:
        if self._thinking or self._signature is not None:
            self._items.append(("thinking", "".join(self._thinking), self._signature))
            self._thinking = []
            self._signature = None

    def text_delta(self, text: str) -> None:
        self._flush_thinking()
        self._text.append(text)

    def thinking_delta(self, text: str) -> None:
        self._flush_text()
        self._thinking.append(text)

    def thinking_end(self, signature: str) -> None:
        self._signature = signature

    def tool_start(self, call_id: str, name: str) -> None:
        self._flush_text()
        self._flush_thinking()
        self._names[call_id] = name
        self._accumulator.start(call_id, name)

    def tool_delta(self, call_id: str, partial_json: str) -> None:
        self._accumulator.delta(call_id, partial_json)

    def tool_end(self, call_id: str, input: dict) -> None:
        authoritative = input if input else None
        final = self._accumulator.finish(call_id, authoritative)
        name = self._names.pop(call_id, "")
        self._items.append(("tool", call_id, name, final))

    def tool_error(self, call_id: str) -> None:
        """Finalize a call whose arguments were malformed into an empty input."""
        self._flush_text()
        self._flush_thinking()
        name = self._names.pop(call_id, "")
        # Passing a dict guarantees no raise; we only want to clear buffers.
        self._accumulator.finish(call_id, {})
        self._items.append(("tool", call_id, name, {}))

    def blocks(self) -> list[ContentBlock]:
        self._flush_text()
        self._flush_thinking()
        blocks: list[ContentBlock] = []
        for item in self._items:
            if item[0] == "text":
                blocks.append(Text(text=item[1]))
            elif item[0] == "thinking":
                blocks.append(Thinking(text=item[1], signature=item[2]))
            else:
                blocks.append(ToolUse(id=item[1], name=item[2], input=item[3]))
        return blocks


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _call_assembler(
    assembler: ContextAssembler, session: SessionView
) -> ModelRequest | Awaitable[ModelRequest]:
    method = getattr(assembler, "assemble", None)
    if method is not None:
        return method(session)
    return assembler(session)  # type: ignore[operator]


def _call_resolver(
    resolver: ProviderResolver, request: ModelRequest
) -> ResolvedModel:
    method = getattr(resolver, "resolve", None)
    if method is not None:
        return method(request)
    return resolver(request)  # type: ignore[operator]


async def _forward_cancel(source: CancelToken, target: CancelToken) -> None:
    """Mirror an external cancellation token onto the lease token."""
    await source.wait()
    target.cancel(source.reason)


async def _cancel_and_drain(*tasks: asyncio.Task) -> None:
    """Cancel tasks and await them under suppression so their ``finally`` runs."""
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _aiter_cancellable(
    source: AsyncIterator[StreamEvent], token: CancelToken
) -> AsyncIterator[StreamEvent]:
    """Iterate ``source`` while racing every ``__anext__`` against cancellation.

    A provider parked at a wait point is cancelled promptly; pending
    ``__anext__``/token-wait tasks are always cancelled *and awaited* under
    suppression so generator ``finally`` blocks run immediately. The source
    iterator is always closed on exit (normal, cancellation, or a raised parse
    error), which closes an adapter's HTTP response without waiting for GC.
    Cancellation surfaces as :class:`~nexus.errors.OperationCancelled`.
    """
    iterator = source.__aiter__()
    nxt: asyncio.Task | None = None
    waiter: asyncio.Task | None = None
    try:
        while True:
            token.raise_if_cancelled()
            nxt = asyncio.ensure_future(iterator.__anext__())
            waiter = asyncio.ensure_future(token.wait())
            try:
                done, _ = await asyncio.wait(
                    {nxt, waiter}, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                await _cancel_and_drain(nxt, waiter)
                raise
            if waiter in done:
                await _cancel_and_drain(nxt)
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter
                raise OperationCancelled(token.reason or "cancelled")
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
            try:
                event = nxt.result()
            except StopAsyncIteration:
                return
            yield event
    finally:
        await _cancel_and_drain(*[task for task in (nxt, waiter) if task is not None])
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await aclose()


async def _collect_stream(
    provider: Provider,
    request: ModelRequest,
    collector: _BlockCollector,
    emitter: _Emitter,
    token: CancelToken,
) -> tuple[str | None, StreamUsage | None, MalformedToolCall | None]:
    """Consume one provider stream, emitting live events and collecting blocks."""
    stop_reason: str | None = None
    usage: StreamUsage | None = None
    malformed: MalformedToolCall | None = None
    try:
        stream = _aiter_cancellable(provider.stream(request), token)
        async with contextlib.aclosing(stream) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    collector.text_delta(event.text)
                    await emitter.emit("text.delta", {"text": event.text})
                elif isinstance(event, ThinkingDelta):
                    collector.thinking_delta(event.text)
                    await emitter.emit("thinking.delta", {"text": event.text})
                elif isinstance(event, ThinkingEnd):
                    collector.thinking_end(event.signature)
                elif isinstance(event, ToolCallStart):
                    collector.tool_start(event.id, event.name)
                elif isinstance(event, ToolCallDelta):
                    collector.tool_delta(event.id, event.partial_json)
                elif isinstance(event, ToolCallEnd):
                    collector.tool_end(event.id, event.input)
                elif isinstance(event, StreamUsage):
                    usage = event
                elif isinstance(event, MessageStop):
                    stop_reason = event.stop_reason
                # MessageStart and Raw carry no block content for Phase 1.
    except MalformedToolCall as exc:
        collector.tool_error(exc.tool_call_id)
        malformed = exc
    return stop_reason, usage, malformed


# ---------------------------------------------------------------------------
# Data shaping
# ---------------------------------------------------------------------------


def _coerce_input(user_input: list[ContentBlock] | str) -> list[ContentBlock]:
    if isinstance(user_input, str):
        return [Text(text=user_input)]
    blocks = list(user_input)
    if not blocks:
        raise ValueError("user_input must contain at least one content block")
    return blocks


def _limits_data(limits: TurnLimits) -> dict:
    return {
        "max_iterations": limits.max_iterations,
        "max_seconds": limits.max_seconds,
    }


def _stream_usage_data(usage: StreamUsage) -> dict:
    return {
        "input": usage.input,
        "output": usage.output,
        "cache_read": usage.cache_read,
        "cache_write": usage.cache_write,
        "reasoning": usage.reasoning,
    }


def _turn_usage_data(usage: TurnUsage) -> dict:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
    }


def _synthetic_results(
    tool_uses: list[ToolUse], malformed: MalformedToolCall | None
) -> list[ToolResult]:
    """One error result per call; nothing is ever dispatched."""
    results: list[ToolResult] = []
    for block in tool_uses:
        if malformed is not None and block.id == malformed.tool_call_id:
            detail = (
                f"Tool call {block.name!r} ({block.id}) had malformed arguments: "
                f"{malformed}. It was not executed."
            )
        else:
            detail = _UNAVAILABLE_TEXT.format(name=block.name)
        results.append(
            ToolResult(
                tool_use_id=block.id,
                content=[Text(text=detail)],
                is_error=True,
            )
        )
    return results


async def _emit_terminal(emitter: _Emitter, state: TurnState) -> None:
    if state.phase == "completed":
        await emitter.emit(
            "turn.completed",
            {
                "stop_reason": state.stop_reason,
                "iterations": state.iteration,
                "usage": _turn_usage_data(state.usage),
            },
        )
    elif state.phase == "failed":
        await emitter.emit(
            "turn.failed",
            {"error": state.error, "iterations": state.iteration},
        )
    elif state.phase == "cancelled":
        await emitter.emit(
            "turn.cancelled",
            {"reason": state.error, "iterations": state.iteration},
        )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def run_turn(
    *,
    session: SessionView,
    user_input: list[ContentBlock] | str,
    assemble: ContextAssembler,
    provider_for: ProviderResolver,
    emit: EventSink,
    lease: TurnLeaseView | None = None,
    limits: TurnLimits | None = None,
    cancel: CancelToken | None = None,
    malformed_budget: int = DEFAULT_MALFORMED_BUDGET,
    persist_user_message: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> TurnOutcome:
    """Run one Phase 1 turn to a terminal state and return its outcome.

    ``lease`` may be supplied by a caller that already holds the session lock;
    otherwise the loop acquires one. The lease is always released before this
    coroutine returns, and exactly one ``turn.completed``/``turn.failed``/
    ``turn.cancelled`` event is emitted on every path.

    ``persist_user_message=False`` tells the loop the caller has already
    persisted the user turn (for example a recovered ``ToolResult`` message
    coalesced with the new input). The loop still validates ``user_input`` and
    assembles from ``session.messages``, but must not append a second user
    message, which would break role alternation.
    """
    if malformed_budget < 0:
        raise ValueError("malformed_budget must be non-negative")
    # Validate caller arguments before taking ownership of any lease/lock.
    content = _coerce_input(user_input)

    if lease is None:
        lease = session.begin_turn(limits=limits)
    if lease.limits is not None:
        limits = lease.limits
    if limits is None:
        limits = TurnLimits()

    session_id = session.id
    turn_id = lease.turn_id
    token = lease.cancel_token
    emitter = _Emitter(emit, session_id, turn_id)

    state = lease.state
    started = clock()

    watcher: asyncio.Task | None = None
    if cancel is not None and cancel is not token:
        watcher = asyncio.create_task(_forward_cancel(cancel, token))

    malformed_total = 0
    terminal: TurnState | None = None
    try:
        await emitter.emit("turn.started", {"limits": _limits_data(limits)})
        if persist_user_message:
            session.append_message(
                Message(
                    role="user",
                    content=content,
                    meta=MessageMeta(turn_id=turn_id),
                )
            )

        while terminal is None:
            reason = limits.exceeded(
                state.usage, clock() - started, iterations=state.iteration
            )
            if reason is not None:
                stop = "max_iterations" if reason == "max_iterations" else "budget"
                terminal = state.complete(stop)
                break
            token.raise_if_cancelled()

            request = await _maybe_await(_call_assembler(assemble, session))
            await emitter.emit(
                "context.assembled",
                {
                    "iteration": state.iteration,
                    "messages": len(request.messages),
                    "provider": request.provider,
                    "model": request.model,
                },
            )

            resolved = _call_resolver(provider_for, request)
            model = request.model or resolved.model
            streaming_request = msgspec.structs.replace(
                request, model=model, provider=resolved.provider.name
            )
            await emitter.emit(
                "model.started",
                {
                    "iteration": state.iteration,
                    "provider": resolved.provider.name,
                    "model": model,
                    "tools": resolved.capabilities.tools,
                    "streaming": resolved.capabilities.streaming,
                },
            )

            collector = _BlockCollector()
            stop_reason, usage, malformed = await _collect_stream(
                resolved.provider, streaming_request, collector, emitter, token
            )

            blocks = collector.blocks()
            text = "".join(block.text for block in blocks if isinstance(block, Text))
            thinking = "".join(
                block.text for block in blocks if isinstance(block, Thinking)
            )
            signature = next(
                (
                    block.signature
                    for block in reversed(blocks)
                    if isinstance(block, Thinking) and block.signature
                ),
                None,
            )
            if text:
                await emitter.emit("text", {"text": text})
            if thinking or signature is not None:
                await emitter.emit("thinking", {"text": thinking, "signature": signature})

            has_tools = any(isinstance(block, ToolUse) for block in blocks)
            effective_stop = stop_reason or ("tool_use" if has_tools else "end_turn")
            if usage is not None:
                await emitter.emit("model.usage", _stream_usage_data(usage))
            await emitter.emit("model.stopped", {"stop_reason": effective_stop})

            call_usage = (
                TurnUsage.from_stream_usage(usage)
                if usage is not None
                else TurnUsage()
            )
            state = state.record_usage(call_usage)
            lease.state = state

            # The assistant message is durable before any tool handling, so a
            # crash can never leave a tool result without its originating call.
            session.append_message(
                Message(
                    role="assistant",
                    content=blocks,
                    meta=MessageMeta(
                        provider=resolved.provider.name,
                        model=model,
                        usage=_stream_usage_data(usage) if usage is not None else None,
                        turn_id=turn_id,
                    ),
                )
            )

            if not has_tools:
                if effective_stop == "error":
                    terminal = state.fail("model stopped with stop_reason 'error'")
                else:
                    complete_reason = (
                        effective_stop
                        if effective_stop in _COMPLETION_REASONS
                        else "end_turn"
                    )
                    terminal = state.model_responded(
                        has_tool_use=False, stop_reason=complete_reason
                    )
                break

            # Phase 1: no dispatcher. Answer every call with a durable,
            # model-visible error result and let the model self-correct.
            if malformed is not None:
                malformed_total += 1
            session.append_message(
                Message(
                    role="user",
                    content=_synthetic_results(
                        [b for b in blocks if isinstance(b, ToolUse)], malformed
                    ),
                    meta=MessageMeta(turn_id=turn_id),
                )
            )
            state = state.model_responded(has_tool_use=True, stop_reason="tool_use")
            lease.state = state

            if malformed_total > malformed_budget:
                terminal = state.fail(
                    "malformed tool call budget exceeded "
                    f"({malformed_total} > {malformed_budget})"
                )
                break

            state = state.begin_iteration()
            lease.state = state

    except OperationCancelled as exc:
        terminal = state.cancel(str(exc) or "cancelled")
    except ProviderError as exc:
        terminal = state.fail(f"{type(exc).__name__}: {exc}")
    except asyncio.CancelledError:
        terminal = state.cancel("task cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - harness failures end the turn, never crash
        terminal = state.fail(f"{type(exc).__name__}: {exc}")
    finally:
        # Lease release is the outermost cleanup: it must run even if draining
        # the watcher or emitting the terminal event raises unexpectedly.
        try:
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher
            if terminal is None:
                terminal = state.fail("turn ended without a terminal state")
            if terminal is None:  # pragma: no cover - state.fail always succeeds
                terminal = state
            await _emit_terminal(emitter, terminal)
        finally:
            lease.release()

    return TurnOutcome.from_state(terminal)
