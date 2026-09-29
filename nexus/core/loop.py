"""The agentic loop (plan section 4), written against protocols only.

``nexus.core.loop`` is the seam the plan calls out explicitly: it drives a turn
by talking to a :class:`SessionView`, a :class:`ContextAssembler`, a
:class:`ProviderResolver`, and an :class:`EventSink`, and it imports **no**
concrete session, context, runtime, manager, or provider adapter. Everything
below the loop (L0 ``errors``/``events``, L1 ``model``, core ``turn``/``cancel``)
is fair game; everything above it is reached through a protocol.

Tool handling
-------------

The loop assembles one structured :class:`~nexus.model.request.ModelRequest` per
iteration, streams the provider's normalized events, collects assistant content
blocks in wire order, and persists the assistant message **before** any tool
handling.

With no ``tools``/``gate`` dependencies (the Phase 1 path) every tool call is
answered with one persisted user message of error ``ToolResult`` blocks; the
turn continues so the model can self-correct, bounded by
:attr:`~nexus.core.turn.TurnLimits.max_iterations`. Malformed tool arguments
become a durable error result and count against ``malformed_budget``. Nothing is
ever executed.

With protocol ``tools``/``gate`` dependencies the loop drives the full Phase 2
flow using **only** the structural contracts defined below — it still imports no
concrete manager. It prepares and permission-plans the whole batch, resolves
every ``ASK`` durably (emitting ``permission.requested``/``permission.resolved``),
dispatches only approved calls, and appends exactly one user ``ToolResult``
message after all executions finish. Provider capability ``tools=False`` drops
the schemas and turns any emitted call into a capability error result.

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
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

import msgspec

from ..errors import MalformedToolCall, OperationCancelled, ProviderError
from ..events import Event
from ..model.capabilities import Capabilities
from ..model.message import (
    DUPLICATE_TOOL_CALL_KEY,
    ContentBlock,
    Document,
    Image,
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
from ..util import redact_secrets, redact_url_userinfo
from .cancel import CancelToken
from .turn import TurnLimits, TurnOutcome, TurnState, TurnUsage

__all__ = [
    "DEFAULT_MALFORMED_BUDGET",
    "BatchPlanView",
    "ContextAssembler",
    "EnvironmentFactory",
    "EvaluationView",
    "EventSink",
    "HookOutcomeView",
    "HookRunner",
    "IterationEnvironment",
    "ManifestLeaseView",
    "ManifestRefView",
    "PermissionGate",
    "PreparedBatchView",
    "PreparedCallView",
    "ProviderResolver",
    "ResolvedModel",
    "SessionView",
    "ToolCallView",
    "ToolDispatcher",
    "ToolPreviewView",
    "TurnLeaseView",
    "run_turn",
]

#: How many malformed tool calls a single turn tolerates before the harness
#: gives up. A model that cannot emit valid JSON repeatedly is a harness-level
#: failure, not a tool-level one.
DEFAULT_MALFORMED_BUDGET = 4
#: Bounds on the child request snapshot recorded in ``context.assembled``.
MAX_SNAPSHOT_SYSTEM_CHARS = 200_000
MAX_SNAPSHOT_TOOLS = 256
MAX_SNAPSHOT_SCHEMA_BYTES = 32_768


def _request_snapshot(request: ModelRequest) -> dict[str, Any]:
    """The redacted, bounded system text and tool schemas of one request."""
    tools: list[dict[str, Any]] = []
    for tool in request.tools[:MAX_SNAPSHOT_TOOLS]:
        schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
        if len(msgspec.json.encode(schema)) > MAX_SNAPSHOT_SCHEMA_BYTES:
            schema = {"type": "object", "description": "(schema too large to record)"}
        tools.append({
            "name": tool.name,
            "description": redact_secrets(tool.description or "")[:4_000],
            "input_schema": schema,
        })
    return {
        "system": redact_secrets((request.system or "")[:MAX_SNAPSHOT_SYSTEM_CHARS]),
        "tools": tools,
    }

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
# Manifest / per-iteration environment contracts
# ---------------------------------------------------------------------------
#
# The loop is deliberately unaware of ``nexus.ext``: it names only the
# structural shape of an atomically pinned manifest generation and of the
# per-iteration environment a runtime builds from it. A runtime that owns an
# extension world passes ``manifest_ref``/``environment_for`` and the loop pins
# exactly one generation per iteration, then releases it after every tool and
# event for that iteration. A runtime without one passes neither, and the loop
# behaves exactly as it did before manifests existed.


class ManifestLeaseView(Protocol):
    """A pinned claim on one immutable manifest generation.

    Structurally matches :class:`nexus.ext.manifest.ManifestLease`. ``manifest``
    is opaque to the loop: only the runtime's environment factory reads it.
    """

    @property
    def manifest(self) -> object: ...

    @property
    def generation(self) -> int: ...

    @property
    def released(self) -> bool: ...

    def release(self) -> None: ...


class ManifestRefView(Protocol):
    """The atomic, reference-counted handle to the current generation."""

    @property
    def generation(self) -> int: ...

    def get(self) -> object: ...

    def pin(self) -> ManifestLeaseView: ...


class HookOutcomeView(Protocol):
    """The aggregate result of one lifecycle event's hooks (plan section 5.7).

    Structurally matches :class:`nexus.hooks.model.HookOutcome`. ``blocked`` and
    ``modified``/``modified_input`` are the only fields the loop acts on; a
    ``modify`` input the loop applies must be revalidated against the tool schema
    and re-gated before it is dispatched. ``to_dict`` is used to persist
    ``hook.fired``/``hook.blocked``.
    """

    @property
    def blocked(self) -> bool: ...

    @property
    def reason(self) -> str: ...

    @property
    def modified(self) -> bool: ...

    @property
    def modified_input(self) -> Mapping[str, Any] | None: ...

    def to_dict(self) -> dict[str, Any]: ...


class HookRunner(Protocol):
    """Runs one pinned generation's lifecycle hooks.

    The loop names only the lifecycle events and the tool hooks; the concrete
    runner (a runtime adapter over the hook manager) owns discovery, matching,
    timeouts, and the ``block``/``modify`` semantics. Every method returns a
    :class:`HookOutcomeView` so the loop can persist the decision and act on a
    block/modify without importing ``nexus.hooks``.
    """

    async def lifecycle(
        self,
        event: str,
        *,
        session_id: str,
        turn_id: str,
        data: Mapping[str, Any] | None = None,
        cancel: CancelToken | None = None,
    ) -> HookOutcomeView: ...

    async def pre_tool_use(
        self,
        *,
        tool: str,
        key: str | None = None,
        bundle: str | None = None,
        tool_input: Mapping[str, Any],
        session_id: str,
        turn_id: str,
        cancel: CancelToken | None = None,
    ) -> HookOutcomeView: ...

    async def post_tool_use(
        self,
        *,
        tool: str,
        key: str | None = None,
        bundle: str | None = None,
        tool_input: Mapping[str, Any],
        result: Mapping[str, Any] | None = None,
        session_id: str,
        turn_id: str,
        cancel: CancelToken | None = None,
    ) -> HookOutcomeView: ...


class IterationEnvironment(Protocol):
    """Everything one loop iteration draws from a single pinned generation.

    A runtime builds this from the pinned manifest: the context assembler
    (config/system files/skills index/tool schemas), the tool dispatcher (the
    generation's registered tools), and the permission gate (turn-frozen
    security baseline). The loop uses one environment for the whole iteration —
    assembly, model request, permission planning, dispatch, and result
    persistence — so a concurrent reload cannot split an iteration across
    generations.
    """

    def assemble(
        self, session: SessionView, /
    ) -> ModelRequest | Awaitable[ModelRequest]: ...

    @property
    def dispatcher(self) -> ToolDispatcher | None: ...

    @property
    def gate(self) -> PermissionGate | None: ...

    @property
    def hooks(self) -> HookRunner | None: ...


class EnvironmentFactory(Protocol):
    """Build the iteration environment from one pinned manifest generation.

    Called once per loop iteration, synchronously after the generation is
    pinned, so the manifest is stable for the whole iteration. May return an
    awaitable; the loop awaits it before using the environment.
    """

    def __call__(
        self,
        session: SessionView,
        lease: ManifestLeaseView,
        iteration: int,
        /,
    ) -> IterationEnvironment | Awaitable[IterationEnvironment]: ...


# ---------------------------------------------------------------------------
# Tool contracts (protocol level; the loop never imports a concrete manager)
# ---------------------------------------------------------------------------
#
# The tools packet (``nexus.tools``) owns the concrete shapes; the loop only
# depends on the structural surface below. That keeps ``core/loop.py`` free of
# any ``nexus.tools`` import while still letting it drive preparation, permission
# planning, durable approval, and dispatch. The concrete adapters live in
# ``nexus.runtime`` (composition root) and are handed to ``run_turn`` by
# ``Session``.


class ToolCallView(Protocol):
    """One parsed tool invocation (``ToolUse``-shaped)."""

    id: str
    name: str
    input: dict[str, Any]


class PreparedCallView(Protocol):
    """One prepared batch entry: an error, or an executable awaiting a decision."""

    call: ToolCallView
    spec: object | None
    key: str | None
    error: object | None
    code: str | None
    decision: object | None


class PreparedBatchView(Protocol):
    """An ordered, prepared batch (see ``nexus.tools.manager.PreparedBatch``)."""

    entries: Sequence[PreparedCallView]

    def calls(self) -> Sequence[ToolCallView]: ...

    def spec_map(self) -> Mapping[str, object]: ...

    def apply_plan(self, plan: BatchPlanView, /) -> PreparedBatchView: ...

    def with_decisions(
        self,
        decisions: Mapping[str, object] | Sequence[tuple[str, object]],
        /,
    ) -> PreparedBatchView: ...


class EvaluationView(Protocol):
    """The permission engine's verdict for one call."""

    call: ToolCallView
    spec: object | None
    key: str | None
    outcome: object
    decision: object | None
    code: str
    reason: str


class BatchPlanView(Protocol):
    """A pure permission plan over a whole batch (see ``BatchPlan``)."""

    evaluations: Sequence[EvaluationView]

    def asks(self) -> Sequence[EvaluationView]: ...

    def failures(self) -> Sequence[EvaluationView]: ...


class PermissionGate(Protocol):
    """Permission planning plus UI-independent, durable approval.

    The concrete adapter binds the frozen engine, session grants, attended flag,
    and an :class:`~nexus.tools.permissions.ApprovalBroker`. The loop emits
    ``permission.requested``/``permission.resolved`` around :meth:`open` /
    :meth:`await_decision`, so a UI can resolve the request by id through the
    session while the turn is parked.
    """

    def plan(self, prepared: PreparedBatchView, /) -> BatchPlanView: ...

    def request_for(self, evaluation: EvaluationView, /) -> object: ...

    def open(self, request: object, /) -> None: ...

    async def await_decision(
        self, request: object, /, *, cancel: CancelToken
    ) -> object: ...

    def resolution(self, request_id: str, /) -> Mapping[str, Any] | None: ...

    def authorize(
        self,
        prepared: PreparedBatchView,
        plan: BatchPlanView,
        decisions: Sequence[tuple[str, object]],
        /,
    ) -> PreparedBatchView: ...

    def resolve(self, request_id: str, decision: object, /) -> bool: ...

    def cancel_pending(self) -> None: ...


class ToolPreviewView(Protocol):
    """A non-executing preview of one call's bundle and canonical key."""

    @property
    def name(self) -> str: ...

    @property
    def bundle(self) -> str | None: ...

    @property
    def key(self) -> str | None: ...


class ToolDispatcher(Protocol):
    """Validation/preparation plus gated dispatch, preserving result order."""

    def prepare(self, tool_uses: Sequence[ToolUse], /) -> PreparedBatchView: ...

    def preview(self, tool_uses: Sequence[ToolUse], /) -> Sequence[ToolPreviewView]:
        """Preview each call's canonical ``(bundle, key)`` without executing.

        Optional in practice (the loop duck-types it): the concrete adapter
        canonicalizes fs/path-mode keys through its ``PathGuard`` so a
        ``PreToolUse`` hook matches the same key the permission engine will.
        """
        ...

    async def dispatch(
        self,
        prepared: PreparedBatchView,
        /,
        *,
        emit: Callable[[str, dict[str, Any] | None], object],
        cancel: CancelToken,
        parallel_allowed: bool = True,
    ) -> Sequence[ToolResult]: ...


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

    def has_output(self) -> bool:
        """Whether any user-visible content has been produced so far.

        Used by the degradation path: a capability rejection may only be retried
        when *nothing* visible has streamed yet (plan section 15.5). Text and
        thinking buffers count, as does any started or flushed tool call.
        """
        return bool(self._text or self._thinking or self._items or self._names)


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


def _call_fallbacks(
    resolver: ProviderResolver, request: ModelRequest
) -> list[ResolvedModel]:
    """Return the resolver's configured fallback candidates, or ``[]``.

    Structural and optional: a resolver without ``fallbacks`` (every Phase 1
    resolver) yields no candidates, so the loop keeps its single-provider
    behaviour exactly. A broken fallbacks implementation never fails a turn.
    """
    method = getattr(resolver, "fallbacks", None)
    if not callable(method):
        return []
    try:
        candidates = method(request)
    except Exception:  # noqa: BLE001 - a bad fallback list must not break a turn
        return []
    return [candidate for candidate in candidates if candidate is not None]


def _candidate_models(
    resolver: ProviderResolver,
    request: ModelRequest,
    primary: ResolvedModel,
) -> list[ResolvedModel]:
    """Primary first, then each distinct configured fallback."""
    candidates = [primary]
    seen = {(primary.provider.name, primary.model)}
    for candidate in _call_fallbacks(resolver, request):
        key = (candidate.provider.name, candidate.model)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def _session_provider(session: SessionView) -> str | None:
    """The provider of the most recent assistant turn, for switch detection."""
    for message in reversed(session.messages):
        if message.role != "assistant":
            continue
        meta = getattr(message, "meta", None)
        provider = getattr(meta, "provider", None)
        if provider:
            return provider
    return None


def _call_environment_factory(
    factory: EnvironmentFactory,
    session: SessionView,
    lease: ManifestLeaseView,
    iteration: int,
    *,
    cancel: CancelToken | None = None,
) -> IterationEnvironment | Awaitable[IterationEnvironment]:
    """Invoke an environment factory, tolerating a ``for_iteration`` method.

    ``cancel`` is passed only when the factory accepts it, so existing factories
    keep working while a manifest environment can wire it into the PreCompact
    gate (a command hook then cancels with the turn).
    """
    method = getattr(factory, "for_iteration", None)
    if method is None:
        method = factory
    if _accepts_keyword(method, "cancel"):
        return method(session, lease, iteration, cancel=cancel)
    return method(session, lease, iteration)


def _accepts_keyword(func: object, name: str) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins/opaque callables
        return False
    if name in params:
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


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


#: Message substrings that identify which capability a plain ``ProviderError``
#: is rejecting. Adapters should raise :class:`CapabilityRejected` with an
#: explicit ``feature``; this is the best-effort fallback for a provider that
#: only carries the rejection in its error text.
_CAPABILITY_FEATURE_HINTS: tuple[tuple[str, str], ...] = (
    ("does not support tool", "tools"),
    ("does not support function", "tools"),
    ("unsupported tool", "tools"),
    ("tool calling", "tools"),
    ("function calling", "tools"),
    ("tool_use", "tools"),
    ("tool use", "tools"),
    ("does not support parallel", "parallel_tool_calls"),
    ("parallel tool", "parallel_tool_calls"),
    ("does not support thinking", "thinking"),
    ("extended thinking", "thinking"),
    ("does not support prompt cach", "prompt_caching"),
    ("prompt cach", "prompt_caching"),
    ("does not support vision", "vision"),
    ("does not support image", "vision"),
    ("does not support document", "documents"),
    ("does not support pdf", "documents"),
    ("structured output", "json_schema_strict"),
    ("json schema", "json_schema_strict"),
)


def _rejected_feature(exc: ProviderError) -> str | None:
    """The capability a provider error rejects, or ``None`` if unrecognized."""
    feature = getattr(exc, "feature", None) or getattr(exc, "capability", None)
    if isinstance(feature, str) and feature:
        return feature
    message = str(exc).lower()
    for needle, name in _CAPABILITY_FEATURE_HINTS:
        if needle in message:
            return name
    return None


#: Fallback degradation policy per content-bearing feature when the adapter has
#: not declared one. Dropping thinking keeps an unsupported chain-of-thought out
#: of the prompt; converting an unsupported image/document to a short text note
#: preserves the *existence* of the attachment rather than silently losing it.
_DEFAULT_DEGRADATION: dict[str, str] = {
    "thinking": "drop",
    "vision": "to_text",
    "documents": "to_text",
}

#: Metadata keys that only exist because a capability was requested. A
#: degradation retry removes them so a provider that rejected the capability is
#: not sent the metadata that advertised it.
_FEATURE_METADATA_KEYS: dict[str, tuple[str, ...]] = {
    "prompt_caching": ("cache",),
    "json_schema_strict": (
        "json_schema_strict",
        "structured_output",
        "response_format",
        "strict",
    ),
    "parallel_tool_calls": ("parallel_tool_calls",),
}


def _degradation_policy(capabilities: Capabilities, feature: str) -> str:
    """The declared ``drop``/``to_text``/``error`` policy for ``feature``.

    An unknown or malformed declaration falls back to the per-feature default,
    so a hostile or corrupt catalogue can never turn the retry path into a
    silent no-op or an unexpected raise.
    """
    degradation = getattr(capabilities, "degradation", None) or {}
    if isinstance(degradation, Mapping):
        policy = degradation.get(feature)
        if policy in ("drop", "to_text", "error"):
            return policy
    return _DEFAULT_DEGRADATION.get(feature, "drop")


def _degrade_block(block: ContentBlock, feature: str, policy: str):
    """Return the replacement block, ``None`` to drop, or raise for ``error``."""
    if policy == "error":
        raise ProviderError(
            f"provider rejected the {feature!r} capability and the configured "
            f"degradation policy for {feature!r} is 'error'"
        )
    if policy == "drop":
        return None
    if isinstance(block, Thinking):
        return Text(text=block.text)
    if isinstance(block, Image):
        return Text(
            text=f"[image omitted: provider does not support the {feature} capability]"
        )
    if isinstance(block, Document):
        title = block.title or block.media_type
        return Text(
            text=(
                f"[document omitted ({title}): provider does not support the "
                f"{feature} capability]"
            )
        )
    return Text(text=f"[{feature} omitted: provider does not support it]")


def _degrade_inner(
    content: list[Text | Image], feature: str, policy: str
) -> list[Text | Image] | None:
    """Apply a vision degradation to a ``ToolResult`` inner content list."""
    changed = False
    out: list[Text | Image] = []
    for block in content:
        if isinstance(block, Image):
            changed = True
            replacement = _degrade_block(block, feature, policy)
            if replacement is None:
                continue
            out.append(replacement)  # type: ignore[arg-type]
        else:
            out.append(block)
    return out if changed else None


def _degrade_messages(
    messages: list[Message], feature: str, policy: str
) -> list[Message] | None:
    """Return messages with ``feature`` blocks dropped/to-text, or ``None``.

    ``None`` means nothing changed, which lets the caller avoid replacing the
    request when a feature happens to be unused (so an identical request object
    is reused and no cache-stability signal is perturbed).
    """
    if feature == "thinking":
        block_type: type = Thinking
    elif feature == "vision":
        block_type = Image
    elif feature == "documents":
        block_type = Document
    else:
        return None
    changed = False
    out_messages: list[Message] = []
    for message in messages:
        content_changed = False
        new_content: list[ContentBlock] = []
        for block in message.content:
            if isinstance(block, block_type):
                content_changed = True
                replacement = _degrade_block(block, feature, policy)
                if replacement is None:
                    continue
                new_content.append(replacement)
            elif isinstance(block, ToolResult) and feature == "vision":
                inner = _degrade_inner(block.content, feature, policy)
                if inner is None:
                    new_content.append(block)
                else:
                    content_changed = True
                    new_content.append(msgspec.structs.replace(block, content=inner))
            else:
                new_content.append(block)
        if content_changed:
            changed = True
            out_messages.append(msgspec.structs.replace(message, content=new_content))
        else:
            out_messages.append(message)
    return out_messages if changed else None


def _request_without_feature(
    request: ModelRequest,
    feature: str,
    capabilities: Capabilities,
) -> ModelRequest:
    """Adapt a request for one degradation retry with ``feature`` disabled.

    This is the other half of "the retry actually changes the request": emitting
    ``context.degraded`` while resending the same body would loop the provider's
    rejection into a hard failure. Every supported feature has a concrete
    transform, so a retry is always materially different:

    * ``tools`` -- drop the tool schemas;
    * ``thinking`` -- clear ``thinking_budget`` and drop/to-text thinking blocks
      per the declared policy;
    * ``prompt_caching`` -- remove the ``cache`` metadata the adapter reads to
      place breakpoints;
    * ``vision``/``documents`` -- drop or convert the blocks per policy,
      including images nested in ``ToolResult.content``;
    * ``json_schema_strict``/``parallel_tool_calls`` -- remove the metadata keys
      that requested them, when the request represents them at all.

    An unknown feature leaves the request unchanged; the caller still refuses a
    second retry, so an unrecognized rejection fails the turn rather than
    looping.
    """
    if feature == "tools":
        if request.tools:
            return msgspec.structs.replace(request, tools=[])
        return request

    if feature in ("thinking", "vision", "documents"):
        policy = _degradation_policy(capabilities, feature)
        updates: dict[str, object] = {}
        messages = _degrade_messages(request.messages, feature, policy)
        if messages is not None:
            updates["messages"] = messages
        if feature == "thinking" and request.params.thinking_budget is not None:
            updates["params"] = msgspec.structs.replace(
                request.params, thinking_budget=None
            )
        if not updates:
            return request
        return msgspec.structs.replace(request, **updates)

    keys = _FEATURE_METADATA_KEYS.get(feature)
    if keys and isinstance(request.metadata, Mapping):
        remaining = {
            key: value for key, value in request.metadata.items() if key not in keys
        }
        if len(remaining) != len(request.metadata):
            return msgspec.structs.replace(request, metadata=remaining)
    return request


async def _stream_with_degradation(
    *,
    provider: Provider,
    request: ModelRequest,
    capabilities: Capabilities,
    emitter: _Emitter,
    token: CancelToken,
    iteration: int,
    model: str,
    collector: _BlockCollector | None = None,
) -> tuple[
    _BlockCollector,
    str | None,
    StreamUsage | None,
    MalformedToolCall | None,
    Capabilities,
]:
    """Stream one model call, retrying once on a capability rejection.

    Plan section 15.5: when the provider rejects a capability the registry
    claimed, the turn degrades rather than failing -- ``context.degraded`` and
    ``registry.mismatch`` are emitted, the offending feature is disabled, and
    the call is retried exactly once. A rejection is only retried when nothing
    visible has streamed yet; a refusal (a stop reason) or a mid-stream failure
    is never retried.

    ``collector`` may be supplied by the caller so it can inspect whether any
    output streamed after a failure (the provider-fallback guard). It is reused
    across the capability retry, which only happens while it is still empty.
    """
    attempt_caps = capabilities
    attempt_request = request
    if collector is None:
        collector = _BlockCollector()
    retried = False
    while True:
        try:
            stop_reason, usage, malformed = await _collect_stream(
                provider, attempt_request, collector, emitter, token
            )
            return collector, stop_reason, usage, malformed, attempt_caps
        except MalformedToolCall:
            # _collect_stream turns malformed arguments into a durable error
            # result; it is never a capability retry.
            raise
        except ProviderError as exc:
            feature = None if retried else _rejected_feature(exc)
            if feature is None or collector.has_output():
                raise
            retried = True
            attempt_caps = attempt_caps.disabled(feature)
            attempt_request = _request_without_feature(
                attempt_request, feature, attempt_caps
            )
            detail = redact_secrets(str(exc))
            await emitter.emit(
                "context.degraded",
                {
                    "iteration": iteration,
                    "provider": provider.name,
                    "model": model,
                    "feature": feature,
                    "reason": "capability_rejected",
                    "retry": 1,
                    "detail": detail,
                },
            )
            await emitter.emit(
                "registry.mismatch",
                {
                    "iteration": iteration,
                    "provider": provider.name,
                    "model": model,
                    "feature": feature,
                    "source": "provider-rejection",
                    "detail": detail,
                },
            )


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


def _usage_event_data(usage: StreamUsage, provider: object) -> dict:
    """``model.usage`` payload plus ``prompt``: every input token the request held.

    Adapters disagree on whether ``input`` already counts cached tokens. One
    that reports them separately sets ``usage_input_excludes_cache``, so the
    prompt size (what the request occupied in the context window) is the same
    measurement for every provider.
    """
    data = _stream_usage_data(usage)
    prompt = usage.input
    if getattr(provider, "usage_input_excludes_cache", False) is True:
        prompt += usage.cache_read + usage.cache_write
    if prompt > 0:
        data["prompt"] = prompt
    return data


def _turn_usage_data(usage: TurnUsage) -> dict:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
    }


#: Provider-safe synthetic id length cap (Anthropic accepts short opaque ids).
_MAX_SYNTHETIC_ID = 64


def _synthetic_call_id(original: str, reserved: set[str]) -> str:
    """A fresh, deterministic, provider-safe id for a duplicated call.

    ``reserved`` contains every original id in the message (so a synthetic id
    can never collide with a later legitimate id) plus synthetics already
    handed out. The result is stable for a given message.
    """
    base = re.sub(r"[^A-Za-z0-9_-]", "_", original).strip("_") or "call"
    base = base[: _MAX_SYNTHETIC_ID - 8]
    index = 1
    candidate = f"{base}_dup{index}"
    while candidate in reserved:
        index += 1
        candidate = f"{base}_dup{index}"
    return candidate


def _normalize_tool_use_ids(
    blocks: list[ContentBlock],
) -> tuple[list[ContentBlock], dict[str, str]]:
    """Make every persisted ``ToolUse`` id unique; return re-identified duplicates.

    The first occurrence of an id keeps its original id. Each later occurrence
    gets a fresh synthetic id and its input is marked with
    :data:`~nexus.model.message.DUPLICATE_TOOL_CALL_KEY` set to the original id,
    so the tools layer can turn it into a model-visible duplicate-id error
    without depending on manager-level detection. The returned mapping is
    ``synthetic_id -> original_id``.
    """
    reserved = {block.id for block in blocks if isinstance(block, ToolUse)}
    seen: set[str] = set()
    out: list[ContentBlock] = []
    duplicates: dict[str, str] = {}
    for block in blocks:
        if not isinstance(block, ToolUse):
            out.append(block)
            continue
        if block.id in seen:
            original = block.id
            new_id = _synthetic_call_id(original, reserved | seen)
            new_input = dict(block.input)
            new_input[DUPLICATE_TOOL_CALL_KEY] = original
            block = ToolUse(id=new_id, name=block.name, input=new_input)
            duplicates[new_id] = original
        seen.add(block.id)
        out.append(block)
    return out, duplicates


def _synthetic_results(
    tool_uses: list[ToolUse],
    malformed: MalformedToolCall | None,
    duplicates: Mapping[str, str] | None = None,
) -> list[ToolResult]:
    """One error result per call; nothing is ever dispatched."""
    duplicates = duplicates or {}
    results: list[ToolResult] = []
    for block in tool_uses:
        if block.id in duplicates:
            detail = (
                f"Tool call {block.name!r} ({block.id}) duplicated id "
                f"{duplicates[block.id]!r}; the duplicated call was not executed."
            )
        elif malformed is not None and block.id == malformed.tool_call_id:
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


def _outcome_value(outcome: object) -> str:
    """Normalize a StrEnum/str outcome to its plain string value."""
    value = getattr(outcome, "value", outcome)
    return value if isinstance(value, str) else str(value)


def _error_result(tool_use_id: str, name: str, detail: str) -> ToolResult:
    return ToolResult(
        tool_use_id=tool_use_id,
        content=[Text(text=f"{name}: {detail}")],
        is_error=True,
    )


_MAX_TRANSCRIPT_ARGUMENT_CHARS = 8192
_MAX_TRANSCRIPT_ARGUMENT_DEPTH = 8
_MAX_TRANSCRIPT_ARGUMENT_ITEMS = 256
_MAX_TRANSCRIPT_RESULT_CHARS = 100_000
_TRANSCRIPT_RESULT_REDACTION_MARGIN_CHARS = 1024
_TRANSCRIPT_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{4096,}")
_MAX_TRANSCRIPT_RESULT_BLOCKS = 256
_MAX_TRANSCRIPT_DIFF_CHARS = 24_000
_MAX_TRANSCRIPT_DIFF_LINES = 240


def _safe_tool_input(value: object, *, tool: str | None = None) -> dict[str, Any]:
    """Bound and redact tool arguments before including them in durable events."""
    def clean(item: object, depth: int = 0) -> object:
        if depth >= _MAX_TRANSCRIPT_ARGUMENT_DEPTH:
            return "[nested value omitted]"
        if isinstance(item, str):
            text = redact_secrets(item)
            return text if len(text) <= _MAX_TRANSCRIPT_ARGUMENT_CHARS else text[:_MAX_TRANSCRIPT_ARGUMENT_CHARS] + "…"
        if isinstance(item, Mapping):
            return {
                redact_secrets(str(key))[:200]: (
                    "***"
                    if re.search(r"(?i)(api[-_]?key|authorization|access[-_]?token|refresh[-_]?token|client[-_]?secret|password|secret|token)", str(key))
                    else clean(value, depth + 1)
                )
                for key, value in list(item.items())[:_MAX_TRANSCRIPT_ARGUMENT_ITEMS]
            }
        if isinstance(item, (list, tuple)):
            return [clean(value, depth + 1) for value in item[:_MAX_TRANSCRIPT_ARGUMENT_ITEMS]]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return f"<{type(item).__name__}>"

    safe = clean(value)
    # A Write input is the complete file body.  Keeping even a small body in a
    # transcript is unnecessary for rendering and turns the transcript into a
    # second durable copy of the file.  Edit text is similarly represented by
    # its bounded post-execution diff rather than raw replacement arguments.
    if isinstance(safe, dict) and tool == "Write" and "content" in safe:
        original = value.get("content") if isinstance(value, Mapping) else None
        size = len(original) if isinstance(original, str) else 0
        safe["content"] = f"[write content omitted: {size} chars]"
    if isinstance(safe, dict) and tool == "Edit":
        for key in ("old_string", "new_string"):
            if key in safe:
                safe[key] = "[edit text omitted; see diff preview when available]"
    encoded = msgspec.json.encode(safe)
    if len(encoded) > _MAX_TRANSCRIPT_ARGUMENT_CHARS:
        return {"_truncated": encoded[: _MAX_TRANSCRIPT_ARGUMENT_CHARS - 4].decode("utf-8", "ignore") + "…"}
    return safe if isinstance(safe, dict) else {}


def _safe_tool_diff(value: object) -> dict[str, Any] | None:
    """Defensively bound the Edit transcript artifact from a persisted result."""
    if not isinstance(value, Mapping):
        return None
    path = value.get("path")
    hunk = value.get("hunk")
    added = value.get("added_lines")
    removed = value.get("removed_lines")
    if not isinstance(path, str) or not isinstance(hunk, str):
        return None
    safe_lines: list[str] = []
    used = 0
    truncated = bool(value.get("truncated"))
    for line in redact_secrets(hunk).splitlines():
        separator = 1 if safe_lines else 0
        if (
            len(safe_lines) >= _MAX_TRANSCRIPT_DIFF_LINES
            or used + separator + len(line) > _MAX_TRANSCRIPT_DIFF_CHARS
        ):
            truncated = True
            break
        safe_lines.append(line)
        used += separator + len(line)
    safe_hunk = "\n".join(safe_lines)
    return {
        "path": redact_secrets(path)[:2000],
        "hunk": safe_hunk,
        "added_lines": added if isinstance(added, int) and added >= 0 else 0,
        "removed_lines": removed if isinstance(removed, int) and removed >= 0 else 0,
        "truncated": truncated or safe_hunk != hunk,
    }


def _tool_result_event_view(result: ToolResult) -> dict[str, Any]:
    """Bounded/redacted event representation of an already-persisted result."""
    content: list[dict[str, Any]] = []
    remaining = _MAX_TRANSCRIPT_RESULT_CHARS
    for block in result.content[:_MAX_TRANSCRIPT_RESULT_BLOCKS]:
        if isinstance(block, Text):
            redaction_limit = (
                _MAX_TRANSCRIPT_RESULT_CHARS + _TRANSCRIPT_RESULT_REDACTION_MARGIN_CHARS
            )
            bounded_input = block.text[:redaction_limit]
            if len(block.text) > redaction_limit:
                # Supply an end boundary for token patterns when the bounded
                # prefix ends in a credential; this sentinel remains beyond
                # the emitted clip.
                bounded_input += " "
            # The generic opaque-token rule has a trailing word boundary that
            # backtracks over very long unbroken strings. Collapse those runs
            # first; they are opaque credentials by the same policy.
            bounded_input = _TRANSCRIPT_LONG_TOKEN.sub("***", bounded_input)
            text = redact_secrets(bounded_input)
            clipped = text[:remaining]
            if clipped:
                content.append({"type": "text", "text": clipped})
                remaining -= len(clipped)
            if (
                len(block.text) > _MAX_TRANSCRIPT_RESULT_CHARS
                or len(clipped) < len(text)
                or remaining == 0
            ):
                content.append({"type": "text", "text": "[result truncated]"})
                break
        elif isinstance(block, Image):
            content.append({"type": "image", "text": "[image omitted]"})
    if len(result.content) > _MAX_TRANSCRIPT_RESULT_BLOCKS and remaining:
        content.append({"type": "text", "text": "[result blocks omitted]"})
    return {
        "tool_use_id": result.tool_use_id,
        "is_error": result.is_error,
        "content": content,
        "context_note": redact_secrets(result.context_note)[:2000] if result.context_note else None,
        "display": redact_secrets(result.display)[:2000] if result.display else None,
        "metrics": _safe_transcript_metrics(result.metrics),
        "diff": _safe_tool_diff(result.diff),
    }


def _safe_transcript_metrics(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    safe: dict[str, Any] = {}
    for key, item in list(value.items())[:32]:
        if item is None or isinstance(item, (bool, int)):
            safe[str(key)[:100]] = item
        elif isinstance(item, float):
            safe[str(key)[:100]] = item if item == item and abs(item) != float("inf") else None
        elif isinstance(item, str):
            safe[str(key)[:100]] = redact_secrets(item)[:200]
        elif isinstance(item, (list, tuple)):
            safe[str(key)[:100]] = [redact_secrets(str(part))[:200] for part in item[:64]]
        else:
            safe[str(key)[:100]] = f"<{type(item).__name__}>"
    return safe


async def _emit_tool_results(emitter: _Emitter, results: Sequence[ToolResult]) -> None:
    for result in results:
        await emitter.emit("tool.result", _tool_result_event_view(result))


def _hook_missing_result(block: ToolUse) -> ToolResult:
    """A defensive result for a call the dispatcher did not return a result for."""
    return ToolResult(
        tool_use_id=block.id,
        content=[Text(text=f"{block.name}: no result was produced")],
        is_error=True,
    )


#: Per-text-block and total bounds for the result view handed to ``PostToolUse``.
_MAX_POST_RESULT_CHARS = 4000
_MAX_POST_RESULT_TOTAL = 16000


def _tool_result_view(result: object, call_id: str) -> dict[str, Any]:
    """A bounded, JSON-safe view of a dispatched tool result for ``PostToolUse``.

    The hook sees the *actual* result text (not just a status), capped so a huge
    tool output cannot blow the hook's stdin budget.
    """
    texts: list[str] = []
    total = 0
    for block in getattr(result, "content", ()) or ():
        text = getattr(block, "text", None)
        if not isinstance(text, str) or not text:
            continue
        chunk = text[:_MAX_POST_RESULT_CHARS]
        if total + len(chunk) > _MAX_POST_RESULT_TOTAL:
            chunk = chunk[: max(0, _MAX_POST_RESULT_TOTAL - total)]
        if chunk:
            texts.append(chunk)
            total += len(chunk)
        if total >= _MAX_POST_RESULT_TOTAL:
            break
    return {
        "tool_use_id": getattr(result, "tool_use_id", call_id) or call_id,
        "is_error": bool(getattr(result, "is_error", False)),
        "content": texts,
        "context_note": getattr(result, "context_note", None),
    }


def _result_for_entry(entry: object, detail: str) -> ToolResult:
    """Best-effort IR error result for a prepared entry (harness failure path)."""
    call = getattr(entry, "call", None)
    call_id = getattr(call, "id", "")
    name = getattr(call, "name", "")
    error = getattr(entry, "error", None)
    to_ir = getattr(error, "to_tool_result", None)
    if callable(to_ir):
        converted: ToolResult | None = None
        with contextlib.suppress(Exception):
            converted = to_ir(call_id)
        if isinstance(converted, ToolResult):
            return converted
    return _error_result(call_id, name, detail)


async def _emit_gate_decision(
    emitter: _Emitter, evaluation, gate: PermissionGate
) -> None:
    """Audit an unattended allow/deny so the decision is reconstructable."""
    request = gate.request_for(evaluation)
    data = dict(request.to_dict()) if hasattr(request, "to_dict") else {}
    data.update(
        {
            "decision": _outcome_value(evaluation.decision)
            if evaluation.decision is not None
            else _outcome_value(evaluation.outcome),
            "code": evaluation.code,
            "reason": evaluation.reason,
            "grant": None,
        }
    )
    await emitter.emit("permission.resolved", data)


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


def _hook_payload(outcome: object) -> dict[str, Any]:
    if isinstance(outcome, Mapping):
        return dict(outcome)
    to_dict = getattr(outcome, "to_dict", None)
    if callable(to_dict):
        try:
            payload = to_dict()
        except Exception:  # noqa: BLE001 - a bad hook result must not break a turn
            return {}
        if isinstance(payload, Mapping):
            return dict(payload)
    return {}


async def _persist_hook_events(
    emitter: _Emitter, outcome: object, event_name: str
) -> None:
    """Persist ``hook.fired``/``hook.blocked`` for one lifecycle outcome.

    Hook events are part of the durable session log, so a replay reconstructs
    exactly which hooks ran and what they decided.
    """
    payload = _hook_payload(outcome)
    decisions = payload.get("decisions")
    if isinstance(decisions, Sequence):
        for decision in decisions:
            if not isinstance(decision, Mapping):
                continue
            await emitter.emit(
                "hook.fired",
                {
                    "event": event_name,
                    "hook": decision.get("hook"),
                    "action": decision.get("action"),
                },
            )
    if str(payload.get("decision")) == "block":
        await emitter.emit(
            "hook.blocked",
            {
                "event": event_name,
                "reason": payload.get("reason") or "blocked by hook",
            },
        )


async def _run_lifecycle_hook(
    hook_runner: HookRunner | None,
    emitter: _Emitter,
    event: str,
    *,
    session_id: str,
    turn_id: str,
    data: Mapping[str, Any] | None = None,
    cancel: CancelToken | None = None,
) -> object | None:
    """Run one lifecycle hook event, persisting its decisions; never fatal."""
    if hook_runner is None:
        return None
    try:
        outcome = await hook_runner.lifecycle(
            event,
            session_id=session_id,
            turn_id=turn_id,
            data=data or {},
            cancel=cancel,
        )
    except (OperationCancelled, asyncio.CancelledError):
        raise
    except Exception:  # noqa: BLE001 - a broken hook never fails the turn
        return None
    await _persist_hook_events(emitter, outcome, event)
    return outcome


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
    tools: ToolDispatcher | None = None,
    gate: PermissionGate | None = None,
    lease: TurnLeaseView | None = None,
    limits: TurnLimits | None = None,
    cancel: CancelToken | None = None,
    malformed_budget: int = DEFAULT_MALFORMED_BUDGET,
    persist_user_message: bool = True,
    manifest_ref: ManifestRefView | None = None,
    environment_for: EnvironmentFactory | None = None,
    hooks: HookRunner | None = None,
    clock: Callable[[], float] = time.monotonic,
    relay_transcript: bool = False,
    input_id: str | None = None,
    input_content: list[object] | None = None,
) -> TurnOutcome:
    """Run one turn to a terminal state and return its outcome.

    ``lease`` may be supplied by a caller that already holds the session lock;
    otherwise the loop acquires one. The lease is always released before this
    coroutine returns, and exactly one ``turn.completed``/``turn.failed``/
    ``turn.cancelled`` event is emitted on every path.

    ``persist_user_message=False`` tells the loop the caller has already
    persisted the user turn (for example a recovered ``ToolResult`` message
    coalesced with the new input). The loop still validates ``user_input`` and
    assembles from ``session.messages``, but must not append a second user
    message, which would break role alternation.

    ``tools``/``gate`` are optional protocol adapters. When both are supplied
    the loop prepares and permission-plans the whole batch, resolves every
    ``ASK`` durably, dispatches only approved calls, and persists one ordered
    ``ToolResult`` message before the next iteration. When either is absent the
    loop keeps the Phase 1 behaviour exactly: every tool call is answered with a
    synthetic, model-visible error result and nothing is executed.

    ``manifest_ref``/``environment_for`` optionally replace the static
    ``assemble``/``tools``/``gate`` seams with a **per-iteration environment**.
    When supplied, the loop synchronously pins exactly one manifest generation
    before the first await of each iteration, builds that iteration's assembler,
    dispatcher, and gate from that one snapshot, and releases the pin only after
    every tool call and event for the iteration has finished. A reload that
    commits mid-iteration is therefore invisible until the next iteration; the
    pinned generation's modules stay alive until no in-flight call can use them.
    When ``manifest_ref`` is ``None`` the static seams are used unchanged.
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

    async def _tool_emit(event_type: str, data: dict[str, Any] | None) -> None:
        """Adapt the dispatcher's ``(type, data)`` emit seam to the event sink."""
        await emitter.emit(event_type, data)

    state = lease.state
    started = clock()

    watcher: asyncio.Task | None = None
    if cancel is not None and cancel is not token:
        watcher = asyncio.create_task(_forward_cancel(cancel, token))

    malformed_total = 0
    terminal: TurnState | None = None
    #: The generation pinned for the iteration currently executing. Held across
    #: assembly, planning, dispatch, and result persistence, then released.
    manifest_lease: ManifestLeaseView | None = None
    #: The gate most recently used, so the outer cleanup can cancel any pending
    #: approval even when it came from a per-iteration environment.
    active_gate: PermissionGate | None = gate

    def _release_manifest() -> None:
        nonlocal manifest_lease
        lease_to_release, manifest_lease = manifest_lease, None
        if lease_to_release is not None:
            lease_to_release.release()

    try:
        agent_metadata = getattr(session, "_turn_agent_metadata", None)
        await emitter.emit("turn.started", {"limits": _limits_data(limits), **({"agent": dict(agent_metadata)} if isinstance(agent_metadata, Mapping) and agent_metadata else {})})
        if input_id is not None and input_content is not None:
            direct_input = {
                "input_id": input_id,
                "content": input_content,
            }
        else:
            direct_input = None
        # ``SessionStart``/``UserPromptSubmit`` run in the session layer, before
        # the user message is persisted, so a block leaves no orphan prompt and a
        # modify is durable. The loop only fires the per-iteration/turn hooks.
        if persist_user_message:
            session.append_message(
                Message(
                    role="user",
                    content=content,
                    meta=MessageMeta(turn_id=turn_id),
                )
            )
        if direct_input is not None:
            await emitter.emit("input.started", direct_input)

        while terminal is None:
            reason = limits.exceeded(
                state.usage, clock() - started, iterations=state.iteration
            )
            if reason is not None:
                stop = "max_iterations" if reason == "max_iterations" else "budget"
                terminal = state.complete(stop)
                break
            token.raise_if_cancelled()

            # Pin exactly one generation synchronously, before any await, and
            # build this iteration's environment from that snapshot. The pin is
            # released after every tool call and event for the iteration.
            environment: IterationEnvironment | None = None
            if manifest_ref is not None and environment_for is not None:
                manifest_lease = manifest_ref.pin()
                try:
                    environment = await _maybe_await(
                        _call_environment_factory(
                            environment_for,
                            session,
                            manifest_lease,
                            state.iteration,
                            cancel=token,
                        )
                    )
                except BaseException:
                    _release_manifest()
                    raise
                active_gate = getattr(environment, "gate", None)

            iteration_assemble = (
                environment if environment is not None else assemble
            )
            iteration_tools = (
                getattr(environment, "dispatcher", None)
                if environment is not None
                else tools
            )
            iteration_gate = (
                getattr(environment, "gate", None)
                if environment is not None
                else gate
            )
            iteration_hooks = (
                getattr(environment, "hooks", None)
                if environment is not None
                else hooks
            ) or hooks

            request = await _maybe_await(_call_assembler(iteration_assemble, session))
            request_metadata = (
                request.metadata if isinstance(request.metadata, dict) else {}
            )
            # The assembler ran the pinned PreCompact gate before compacting. Its
            # decisions are persisted here; a block prevents compaction (no
            # summary) and fails the turn actionably before the model is called.
            pre_compact_meta = request_metadata.get("pre_compact")
            if isinstance(pre_compact_meta, Mapping):
                outcome = pre_compact_meta.get("outcome")
                if isinstance(outcome, Mapping):
                    await _persist_hook_events(emitter, outcome, "PreCompact")
                if pre_compact_meta.get("blocked"):
                    reason = str(
                        pre_compact_meta.get("reason")
                        or "blocked by PreCompact hook"
                    )
                    terminal = state.fail(f"PreCompact blocked: {reason}")
                    _release_manifest()
                    break
            assembled_data: dict[str, Any] = {
                "iteration": state.iteration,
                "messages": len(request.messages),
                "provider": request.provider,
                "model": request.model,
                "tools": len(request.tools),
            }
            context_meta = request_metadata.get("context")
            cache_meta = request_metadata.get("cache")
            if isinstance(context_meta, dict):
                assembled_data["context"] = dict(context_meta)
            if isinstance(cache_meta, dict):
                assembled_data["cache"] = dict(cache_meta)
            # A child turn's log also records what was actually sent, so the
            # subagent page can show its real system prompt and tool schemas.
            # Context events are never relayed upward and hooks get no snapshot.
            snapshot = (
                {**assembled_data, "request": _request_snapshot(request)}
                if relay_transcript and state.iteration == 0
                else assembled_data
            )
            await emitter.emit("context.assembled", snapshot)
            await _run_lifecycle_hook(
                iteration_hooks,
                emitter,
                "ContextAssembled",
                session_id=session_id,
                turn_id=turn_id,
                data=assembled_data,
                cancel=token,
            )
            compacted = (
                context_meta.get("compacted")
                if isinstance(context_meta, dict)
                else None
            )
            if isinstance(compacted, dict) and compacted:
                await emitter.emit(
                    "context.compacted",
                    {"iteration": state.iteration, **dict(compacted)},
                )

            primary = _call_resolver(provider_for, request)
            candidates = _candidate_models(provider_for, request, primary)
            # A provider switch mid-session reinterprets another provider's
            # history; surface it before the call rather than silently migrating.
            previous_provider = _session_provider(session)
            collector = _BlockCollector()
            resolved = primary
            stop_reason: str | None = None
            usage: StreamUsage | None = None
            malformed: MalformedToolCall | None = None
            for index, candidate in enumerate(candidates):
                # The primary honours an explicit request model; a fallback uses
                # the model its own reference resolved to, so ``backup/m2`` does
                # not inherit the failed ``primary/m1`` id.
                model = (
                    request.model or candidate.model
                    if index == 0
                    else candidate.model or request.model
                )
                capabilities = candidate.capabilities
                # Capability adaptation: a provider without tool support must
                # never receive schemas, and any tool call it still emits is
                # answered with a capability error result rather than dispatched.
                effective_tools = list(request.tools) if capabilities.tools else []
                streaming_request = msgspec.structs.replace(
                    request,
                    model=model,
                    provider=candidate.provider.name,
                    tools=effective_tools,
                )
                if (
                    previous_provider is not None
                    and previous_provider != candidate.provider.name
                ):
                    await emitter.emit(
                        "context.degraded",
                        {
                            "iteration": state.iteration,
                            "provider": candidate.provider.name,
                            "model": model,
                            "feature": "provider_switch",
                            "reason": "provider_switch",
                            "from": previous_provider,
                            "to": candidate.provider.name,
                        },
                    )
                if index > 0:
                    fallback_from = candidates[index - 1]
                    await emitter.emit(
                        "model.retrying",
                        {
                            "iteration": state.iteration,
                            "attempt": index,
                            "from_provider": fallback_from.provider.name,
                            "from_model": request.model or fallback_from.model,
                            "provider": candidate.provider.name,
                            "model": model,
                            "reason": "provider_failure",
                        },
                    )
                await emitter.emit(
                    "model.started",
                    {
                        "iteration": state.iteration,
                        "provider": candidate.provider.name,
                        "model": model,
                        "tools": capabilities.tools,
                        "streaming": capabilities.streaming,
                        "attempt": index,
                        "reasoning_effort": streaming_request.params.reasoning_effort,
                    },
                )
                collector = _BlockCollector()
                try:
                    (
                        collector,
                        stop_reason,
                        usage,
                        malformed,
                        capabilities,
                    ) = await _stream_with_degradation(
                        provider=candidate.provider,
                        request=streaming_request,
                        capabilities=capabilities,
                        emitter=emitter,
                        token=token,
                        iteration=state.iteration,
                        model=model,
                        collector=collector,
                    )
                    resolved = candidate
                    break
                except ProviderError as exc:
                    # A fallback is tried only on a provider-level failure that
                    # produced no output. A refusal is a stop reason, and a
                    # partial stream has ``has_output()`` true, so neither ever
                    # falls back (plan section 8).
                    if collector.has_output() or index + 1 >= len(candidates):
                        raise
                    await emitter.emit(
                        "context.degraded",
                        {
                            "iteration": state.iteration,
                            "provider": candidate.provider.name,
                            "model": model,
                            "feature": "provider_failure",
                            "reason": "provider_failed",
                            "fallback": candidates[index + 1].provider.name,
                            "detail": redact_secrets(str(exc)),
                        },
                    )
            if malformed is not None:
                malformed_total += 1

            # Guarantee unique ToolUse ids *before* the assistant message is
            # persisted, so no later iteration/replay can ever hand a provider or
            # manager a duplicate id. Re-identified duplicates are marked so they
            # become model-visible errors rather than executables.
            blocks, duplicate_calls = _normalize_tool_use_ids(collector.blocks())
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
                await emitter.emit(
                    "model.usage", _usage_event_data(usage, resolved.provider)
                )
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
                _release_manifest()
                break

            tool_uses = [b for b in blocks if isinstance(b, ToolUse)]

            # Every root and child call gets the same bounded presentation
            # events.  These remain separate from the model's durable ToolUse
            # block, so no raw invocation input is duplicated into the event
            # log.
            for block in tool_uses:
                await emitter.emit(
                    "tool.requested", {"call_id": block.id, "tool": block.name}
                )
                await emitter.emit(
                    "tool.input",
                    {
                        "call_id": block.id,
                        "input": _safe_tool_input(block.input, tool=block.name),
                    },
                )

            if iteration_tools is None or iteration_gate is None:
                # Phase 1: no dispatcher. Answer every call with a durable,
                # model-visible error result and let the model self-correct.
                session.append_message(
                    Message(
                        role="user",
                        content=_synthetic_results(
                            tool_uses, malformed, duplicate_calls
                        ),
                        meta=MessageMeta(turn_id=turn_id),
                    )
                )
                await _emit_tool_results(
                    emitter,
                    _synthetic_results(tool_uses, malformed, duplicate_calls),
                    )
                state = state.model_responded(
                    has_tool_use=True, stop_reason="tool_use"
                )
                lease.state = state

                if malformed_total > malformed_budget:
                    terminal = state.fail(
                        "malformed tool call budget exceeded "
                        f"({malformed_total} > {malformed_budget})"
                    )
                    _release_manifest()
                    break

                state = state.begin_iteration()
                lease.state = state
                _release_manifest()
                continue

            # Phase 2: tools are available. The assistant message above is
            # already durable, so a crash can never orphan a tool call. Every
            # call is announced, then the *whole* batch is prepared and planned
            # before anything executes.
            if not capabilities.tools:
                provider_name = resolved.provider.name
                for block in tool_uses:
                    await emitter.emit(
                        "tool.failed",
                        {
                            "call_id": block.id,
                            "tool": block.name,
                            "code": "tools_unsupported",
                            "executed": False,
                        },
                    )
                session.append_message(
                    Message(
                        role="user",
                        content=[
                            _error_result(
                                block.id,
                                block.name,
                                f"provider {provider_name!r} does not support "
                                "tool calls; the call was not run",
                            )
                            for block in tool_uses
                        ],
                        meta=MessageMeta(turn_id=turn_id),
                    )
                )
                await _emit_tool_results(
                    emitter,
                    [
                        _error_result(
                            block.id,
                            block.name,
                            f"provider {provider_name!r} does not support tool calls; the call was not run",
                        )
                        for block in tool_uses
                    ],
                    )
                state = state.model_responded(
                    has_tool_use=True, stop_reason="tool_use"
                )
                lease.state = state
                state = state.begin_iteration()
                lease.state = state
                _release_manifest()
                continue

            # PreToolUse runs over the *whole* batch before anything is prepared,
            # gated, or dispatched. A block becomes an exact sanitized error
            # ToolResult; a modify rewrites the call's input, which is then
            # revalidated by ``prepare`` and re-gated by ``plan`` below.
            dispatch_uses = list(tool_uses)
            blocked_results: dict[str, ToolResult] = {}
            if iteration_hooks is not None:
                previews = None
                preview_fn = getattr(iteration_tools, "preview", None)
                if callable(preview_fn):
                    try:
                        previews = list(preview_fn(tool_uses))
                    except Exception:  # noqa: BLE001 - a preview never fails a turn
                        previews = None
                for index, block in enumerate(tool_uses):
                    preview = (
                        previews[index]
                        if previews is not None and index < len(previews)
                        else None
                    )
                    bundle: str | None = None
                    key: str | None = None
                    if preview is not None:
                        bundle = getattr(preview, "bundle", None)
                        key = getattr(preview, "key", None)
                    else:
                        bundle_for = getattr(iteration_tools, "bundle_for", None)
                        if callable(bundle_for):
                            try:
                                bundle = bundle_for(block.name)
                            except Exception:  # noqa: BLE001
                                bundle = None
                    try:
                        outcome = await iteration_hooks.pre_tool_use(
                            tool=block.name,
                            key=key,
                            bundle=bundle,
                            tool_input=dict(block.input),
                            session_id=session_id,
                            turn_id=turn_id,
                            cancel=token,
                        )
                    except (OperationCancelled, asyncio.CancelledError):
                        raise
                    except Exception:  # noqa: BLE001 - a broken hook never fails a turn
                        outcome = None
                    if outcome is None:
                        continue
                    await _persist_hook_events(emitter, outcome, "PreToolUse")
                    if getattr(outcome, "blocked", False):
                        reason = str(getattr(outcome, "reason", "") or "").strip()
                        reason = reason or "blocked by PreToolUse hook"
                        blocked_results[block.id] = ToolResult(
                            tool_use_id=block.id,
                            content=[
                                Text(
                                    text=(
                                        f"{block.name}: blocked by PreToolUse "
                                        f"hook: {reason}"
                                    )
                                )
                            ],
                            is_error=True,
                        )
                    elif getattr(outcome, "modified", False):
                        modified = getattr(outcome, "modified_input", None)
                        if isinstance(modified, Mapping):
                            dispatch_uses[index] = ToolUse(
                                id=block.id,
                                name=block.name,
                                input=dict(modified),
                            )

            # A hook-blocked call never reaches prepare/gate/dispatch. Merge
            # results back in the model's original call order so history stays
            # a valid tool_use/tool_result pairing.
            active_uses = [
                block for block in dispatch_uses if block.id not in blocked_results
            ]

            def _merge_results(
                dispatch_results,
                _uses=tool_uses,
                _blocked=blocked_results,
            ) -> list[ToolResult]:
                ordered: list[ToolResult] = []
                iterator = iter(dispatch_results)
                for block in _uses:
                    blocked = _blocked.get(block.id)
                    if blocked is not None:
                        ordered.append(blocked)
                    else:
                        ordered.append(next(iterator, _hook_missing_result(block)))
                return ordered

            prepared = iteration_tools.prepare(active_uses)
            # Duplicate-id rejections are a malformed batch and count against the
            # same budget as malformed streamed arguments (Phase 2 consistency).
            malformed_total += sum(
                1
                for entry in prepared.entries
                if getattr(entry, "code", None) == "duplicate_tool_call_id"
            )
            plan = iteration_gate.plan(prepared)

            # fail_turn (unattended) ends the turn before any executable starts,
            # but history stays valid: one ordered error result per call.
            failures = list(plan.failures())
            if failures:
                reason = failures[0].reason
                for evaluation in plan.evaluations:
                    if _outcome_value(evaluation.outcome) == "fail_turn":
                        await _emit_gate_decision(emitter, evaluation, iteration_gate)
                # One ordered result per *prepared* entry keeps the tool_use /
                # tool_result pairing valid even though the turn aborts. Match
                # evaluations positionally (never by id) so a duplicate id
                # cannot collapse two entries onto one evaluation.
                remaining = list(plan.evaluations)
                content = []
                for entry in prepared.entries:
                    evaluation = None
                    for index, candidate in enumerate(remaining):
                        if candidate.call.id == entry.call.id:
                            evaluation = remaining.pop(index)
                            break
                    detail = (
                        f"the turn was stopped before tools ran: {evaluation.reason}"
                        if evaluation is not None
                        else "the turn was stopped before tools ran"
                    )
                    content.append(_result_for_entry(entry, detail))
                session.append_message(
                    Message(
                        role="user",
                        content=_merge_results(content),
                        meta=MessageMeta(turn_id=turn_id),
                    )
                )
                await _emit_tool_results(emitter, _merge_results(content))
                terminal = state.fail(
                    f"unattended policy fails the turn: {reason}"
                )
                _release_manifest()
                break

            # Audit unattended allow/deny decisions (no ASK round-trip).
            for evaluation in plan.evaluations:
                if _outcome_value(evaluation.outcome) == "ask":
                    continue
                if evaluation.code.startswith("unattended"):
                    await _emit_gate_decision(emitter, evaluation, iteration_gate)

            prepared = prepared.apply_plan(plan)

            # Open every ASK in the batch first (so one prompt can cover the
            # whole batch), then await all of them. No executable starts until
            # every ask has resolved.
            pending_asks: list[tuple[Any, Any]] = []
            for evaluation in plan.asks():
                request = iteration_gate.request_for(evaluation)
                iteration_gate.open(request)
                request_data = (
                    dict(request.to_dict()) if hasattr(request, "to_dict") else {}
                )
                await emitter.emit("permission.requested", request_data)
                pending_asks.append((evaluation, request, request_data))

            # An ordered list (not a dict keyed by call id) so a duplicated id
            # can never collapse two decisions into one.
            decisions: list[tuple[str, object]] = []
            for evaluation, request, request_data in pending_asks:
                try:
                    decision = await iteration_gate.await_decision(
                        request, cancel=token
                    )
                except OperationCancelled:
                    iteration_gate.cancel_pending()
                    raise
                record = iteration_gate.resolution(request.id)
                await emitter.emit(
                    "permission.resolved",
                    dict(record) if record is not None else request_data,
                )
                decisions.append((evaluation.call.id, decision))
            if decisions:
                prepared = prepared.with_decisions(decisions)

            authorize = getattr(iteration_gate, "authorize", None)
            if callable(authorize):
                prepared = authorize(prepared, plan, decisions)

            results = await iteration_tools.dispatch(
                prepared,
                emit=_tool_emit,
                cancel=token,
                parallel_allowed=bool(
                    getattr(capabilities, "parallel_tool_calls", False)
                ),
            )

            # PostToolUse observes the outcome of every dispatched call. It cannot
            # change the persisted result; a modify/block here is advisory. The
            # canonical key/bundle come from the prepared entries (so a hook
            # matcher sees the same key the gate planned against, including for a
            # PreToolUse-modified call).
            if iteration_hooks is not None:
                entries = getattr(prepared, "entries", ())
                for offset, (block, result) in enumerate(
                    zip(active_uses, results)
                ):
                    entry = entries[offset] if offset < len(entries) else None
                    spec = getattr(entry, "spec", None)
                    bundle = getattr(spec, "bundle", None)
                    try:
                        post = await iteration_hooks.post_tool_use(
                            tool=block.name,
                            key=getattr(entry, "key", None),
                            bundle=bundle if isinstance(bundle, str) else None,
                            tool_input=dict(block.input),
                            result=_tool_result_view(result, block.id),
                            session_id=session_id,
                            turn_id=turn_id,
                            cancel=token,
                        )
                    except (OperationCancelled, asyncio.CancelledError):
                        raise
                    except Exception:  # noqa: BLE001
                        post = None
                    if post is not None:
                        await _persist_hook_events(emitter, post, "PostToolUse")

            # Rebuild the result list in the model's original call order: a
            # hook-blocked call contributes its exact sanitized result, and every
            # other call consumes the next dispatch result in order (so duplicate
            # call ids can never collapse two results into one).
            final_results = _merge_results(list(results))

            # Exactly one result message, in original call order, only after
            # every execution has finished.
            session.append_message(
                Message(
                    role="user",
                    content=list(final_results),
                    meta=MessageMeta(turn_id=turn_id),
                )
            )
            await _emit_tool_results(emitter, final_results)
            if malformed_total > malformed_budget:
                terminal = state.fail(
                    "malformed tool call budget exceeded "
                    f"({malformed_total} > {malformed_budget})"
                )
                _release_manifest()
                break
            state = state.model_responded(has_tool_use=True, stop_reason="tool_use")
            lease.state = state
            state = state.begin_iteration()
            lease.state = state
            _release_manifest()

    except OperationCancelled as exc:
        terminal = state.cancel(redact_url_userinfo(str(exc)) or "cancelled")
    except ProviderError as exc:
        terminal = state.fail(
            redact_url_userinfo(f"{type(exc).__name__}: {exc}")
        )
    except asyncio.CancelledError:
        terminal = state.cancel("task cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - harness failures end the turn, never crash
        terminal = state.fail(
            redact_url_userinfo(f"{type(exc).__name__}: {exc}")
        )
    finally:
        # Lease release is the outermost cleanup: it must run even if draining
        # the watcher or emitting the terminal event raises unexpectedly.
        try:
            if active_gate is not None:
                cancel_pending = getattr(active_gate, "cancel_pending", None)
                if callable(cancel_pending):
                    with contextlib.suppress(Exception):
                        cancel_pending()
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher
            if terminal is None:
                terminal = state.fail("turn ended without a terminal state")
            if terminal is None:  # pragma: no cover - state.fail always succeeds
                terminal = state
            # TurnEnd observes every terminal outcome (completed/failed/
            # cancelled). A cancelled turn must still emit its terminal event, so
            # this hook can never abort cleanup.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await _run_lifecycle_hook(
                    hooks,
                    emitter,
                    "TurnEnd",
                    session_id=session_id,
                    turn_id=turn_id,
                    data={
                        "phase": terminal.phase,
                        "stop_reason": terminal.stop_reason,
                        "iterations": terminal.iteration,
                        "error": terminal.error,
                    },
                )
            await _emit_terminal(emitter, terminal)
        finally:
            _release_manifest()
            lease.release()

    return TurnOutcome.from_state(terminal)
