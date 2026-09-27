"""The public session handle (plan sections 5.1 and 14.2).

``Session`` is the substrate the loop and UI adapters build on. It provides:

* validated identity and append-only history/event APIs over the JSONL store;
* exclusive active-turn ownership via the session lock, so two turns cannot run
  against one session in-process or across processes;
* cancellation-token plumbing (a fresh token per turn);
* dangling ``ToolUse`` crash recovery: if the process died after an assistant
  tool call was persisted but before its results were, recovery appends one
  model-visible error ``ToolResult`` per unresolved call **without executing
  anything**;
* a **persistent, session-scoped event bus** wired from :mod:`nexus.core.bus`.
  The bus outlives any single turn, so a turn survives with zero subscribers and
  any number of views may watch the same session;
* :meth:`start_turn`, which runs a turn **detached** from any consumer and
  returns its ``turn_id`` immediately;
* :meth:`subscribe`, which catches a late view up from the append-only log and
  then follows the live bus with no gaps and no duplicates;
* :meth:`enqueue`, the durable input queue consumed at the next turn boundary;
* presence counting: ``attended`` is derived from the subscriber count, and a
  drop to zero applies the session's unattended policy to any pending approval;
* :meth:`send`, retained as the attached, backpressured compatibility wrapper
  whose early close still cancels the turn.

Still absent until later packets: fork/list/delete/replay live on
:class:`~nexus.session.manager.SessionManager`; permissions are reached through
:meth:`resolve_permission`.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import msgspec

from ..core.bus import DROP_OLDEST, Bus
from ..core.cancel import CancelToken
from ..core.loop import run_turn
from ..core.turn import TurnLimits, TurnState
from ..errors import OperationCancelled, SessionBusy, SessionError
from ..events import Event
from ..model.message import (
    ContentBlock,
    Message,
    MessageMeta,
    Text,
    ToolResult,
    ToolUse,
)
from ..model.reasoning_effort import ReasoningEffortSelection
from ..model.selection import ModelSelection
from ..util import new_id
from . import snapshot as snapshot_mod
from .agent_selection import AgentSelection
from .ids import validate_session_id
from .lock import SessionLock
from .snapshot import Snapshot, SnapshotSummary
from .store import EventRecord, MessageRecord, ReadResult, SessionStore, SummaryRecord

_INTERRUPTED_TEMPLATE = (
    "Tool call {tool_use_id!r} ({name}) was interrupted before it ran because the "
    "process stopped mid-turn. It was NOT executed. Re-issue the call if you still "
    "need its result."
)

#: Marks the producer task's completion on the fan-out buffer.
_SENTINEL = object()

#: Events that delimit a turn or report a failure. These bypass the bounded
#: fan-out so cancellation cleanup can never deadlock waiting for a gone consumer.
_CRITICAL_EVENTS = frozenset(
    {"turn.started", "turn.completed", "turn.failed", "turn.cancelled", "error"}
)

#: Terminal turn events. A follower may stop after one with ``until``.
TERMINAL_EVENTS = frozenset({"turn.completed", "turn.failed", "turn.cancelled"})

#: Default fan-out capacity. Non-critical events block the producer until the
#: consumer makes room, so this bounds memory without ever dropping an event.
DEFAULT_EVENT_BUFFER = 512

#: Default decision applied to a pending approval when the last viewer leaves.
#: Kept as a plain string so this layer never imports ``nexus.tools``.
DEFAULT_UNATTENDED_DECISION = "deny_once"

#: Reason attached to an unattended ``fail_turn`` fallback.
_UNATTENDED_FAIL_REASON = "unattended policy fails the turn"

#: Input-queue event types. Replayed in append order on open so a crash between
#: a submission and its consumption/drop never loses the pending FIFO.
_QUEUED_EVENT = "input.queued"
_INPUT_EVENT_TYPES = frozenset(
    {_QUEUED_EVENT, "input.consumed", "input.dropped"}
)

#: Tag for raw bytes in a queued-input payload. Makes the representation
#: self-describing and JSON-safe independent of the encoder.
_QUEUED_BYTES_TAG = "__nexus_bytes__"

#: The durable per-session model-selection event. Like ``input.*`` and
#: ``presence.*`` it is emitted outside a turn (no turn id) and carries a
#: monotonic ``seq``, so a late subscriber and a reopened handle both rebuild the
#: same selection from the log.
_MODEL_SELECTED_EVENT = "model.selected"
_AGENT_SELECTED_EVENT = "agent.selected"
_REASONING_EFFORT_SELECTED_EVENT = "reasoning_effort.selected"


def _decode_queued_content(raw: object) -> list[ContentBlock] | None:
    """Decode a persisted ``input.queued`` payload; ``None`` when malformed.

    Rehydration is best-effort: a corrupt or unrepresentable payload is skipped
    rather than failing the open, exactly as a UI tolerates an unknown event.
    """
    if raw is None:
        return None
    try:
        restored = _restore_queued_bytes(raw)
        content = msgspec.convert(restored, type=list[ContentBlock])
    except (msgspec.ValidationError, msgspec.DecodeError, TypeError, ValueError):
        return None
    if not content:
        return None
    return content


def _encode_queued_content(content: list[ContentBlock]) -> list[object]:
    """Encode queued content into a JSON-safe, self-describing payload.

    ``msgspec`` already base64-encodes ``bytes`` when converting structs to
    builtins, and the block ``type`` tag is preserved. Any bytes that survive
    as raw bytes (for example from a caller-supplied ``dict`` block) are tagged
    explicitly, so the payload is safe under **any** JSON encoder and can be
    reopened without relying on msgspec's implicit binary handling.
    """
    return [_json_safe(block) for block in msgspec.to_builtins(content)]


def _json_safe(value: object) -> object:
    """Recursively make a builtins tree JSON-native, tagging raw bytes."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {_QUEUED_BYTES_TAG: base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _restore_queued_bytes(value: object) -> object:
    """Inverse of :func:`_json_safe`: untag base64 byte markers."""
    if isinstance(value, Mapping):
        if set(value) == {_QUEUED_BYTES_TAG}:
            encoded = value[_QUEUED_BYTES_TAG]
            if isinstance(encoded, str):
                try:
                    return base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    return value
        return {str(key): _restore_queued_bytes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_queued_bytes(item) for item in value]
    return value


def _unattended_mode(policy: object) -> str:
    """Normalize an unattended policy to ``"deny"``/``"allow"``/``"fail_turn"``.

    Accepts the config vocabulary (``"deny"``/``"allow"``/``"fail_turn"``), the
    legacy ``Decision`` string values (``"deny_once"``/``"allow_always"``), and
    ``Decision`` enums. Anything unrecognized fails closed to ``"deny"``.
    """
    value = getattr(policy, "value", policy)
    if not isinstance(value, str):
        return "deny"
    lowered = value.lower()
    if lowered in ("fail", "fail_turn"):
        return "fail_turn"
    if lowered.startswith("allow"):
        return "allow"
    return "deny"


def _coerce_user_input(user_input: str | list[ContentBlock]) -> list[ContentBlock]:
    """Validate and normalize a ``send`` argument before any lock is taken."""
    if isinstance(user_input, str):
        return [Text(text=user_input)]
    blocks = list(user_input)
    if not blocks:
        raise ValueError("user_input must contain at least one content block")
    return blocks


@dataclass
class _QueuedInput:
    """One durable-but-in-memory queued submission."""

    queued_id: str
    content: list[ContentBlock]


class _Fanout:
    """Bounded single-producer/single-consumer buffer with true backpressure.

    Non-critical emissions ``await`` space, so every persisted UI event is
    delivered exactly once and nothing is dropped. Terminal/error/sentinel
    insertions are non-blocking (and may exceed the soft bound) so cleanup can
    always complete even when the consumer has gone away.

    Used only by :meth:`Session.send`, the attached compatibility path. The
    detached :meth:`Session.subscribe` path is lossless by construction because
    it reads the authoritative append-only log.
    """

    __slots__ = ("_capacity", "_data", "_items", "_space")

    def __init__(self, capacity: int = DEFAULT_EVENT_BUFFER) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("event_buffer must be a positive integer")
        self._capacity = capacity
        self._items: deque[Any] = deque()
        self._data = asyncio.Event()
        self._space = asyncio.Event()
        self._space.set()

    def _offer(self, item: Any) -> None:
        self._items.append(item)
        self._data.set()

    async def put(self, item: Any, *, critical: bool = False) -> None:
        if critical:
            # Never await: must work during cancellation with no consumer.
            self._offer(item)
            return
        while len(self._items) >= self._capacity:
            self._space.clear()
            # Re-check after clearing so a concurrent wakeup is not lost.
            if len(self._items) < self._capacity:
                break
            await self._space.wait()
        self._offer(item)

    async def get(self) -> Any:
        while not self._items:
            self._data.clear()
            if self._items:
                break
            await self._data.wait()
        item = self._items.popleft()
        if len(self._items) < self._capacity:
            self._space.set()
        return item

    def close(self) -> None:
        """Non-blocking sentinel; safe to call from a task done-callback."""
        self._offer(_SENTINEL)


class _SessionEventSink:
    """Persist, then fan out to the persistent bus and an optional attached queue.

    The loop emits in strict order and both fan-out paths are FIFO, so consumers
    observe exactly the persisted ordering. Persisting before enqueuing means an
    event a consumer has seen is already durable. The bus carries live events to
    every subscriber; the attached queue (``send`` only) applies backpressure.
    """

    __slots__ = ("_fanout", "_session")

    def __init__(self, session: Session, fanout: _Fanout | None = None) -> None:
        self._session = session
        self._fanout = fanout

    async def emit(self, event: Event) -> EventRecord:
        record = self._session.append_event(event)
        self._session._observe_event(event)
        if event.type == "turn.completed":
            # Cadence is evaluated only on completed turns. Snapshot failure is
            # never allowed to fail the turn: the log is authoritative and a
            # missing/old snapshot only costs a full replay on resume.
            self._session._snapshot_after_completed_turn()
        self._session._publish(record.event)
        if self._fanout is not None:
            await self._fanout.put(
                record.event, critical=event.type in _CRITICAL_EVENTS
            )
        return record


@dataclass
class TurnLease:
    """Exclusive ownership of a session's active turn.

    Holds the session lock for its lifetime. Use it as a context manager or call
    :meth:`release` exactly once; release is idempotent.
    """

    turn_id: str
    state: TurnState
    cancel_token: CancelToken
    limits: TurnLimits | None = None
    _release: Callable[[], None] | None = None

    def release(self) -> None:
        release, self._release = self._release, None
        if release is not None:
            release()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> bool:
        self.release()
        return False


class Session:
    """A handle over one append-only session log.

    A handle is bound to the single asyncio event loop that runs its turns: its
    event bus, idle event, input queue, and turn task are loop-bound. Drive one
    handle from one loop only; see
    :class:`~nexus.session.manager.SessionManager` for the full affinity rule.
    """

    def __init__(
        self,
        session_id: str,
        *,
        store: SessionStore,
        lock: SessionLock | None = None,
        assemble: Callable[[object], Any] | None = None,
        provider_for: Callable[..., Any] | None = None,
        limits: TurnLimits | Callable[[], TurnLimits] | None = None,
        tools: Callable[..., Any] | None = None,
        attended: bool = False,
        event_buffer: int = DEFAULT_EVENT_BUFFER,
        snapshot_every: int | Callable[[], int] | None = None,
        unattended_decision: object | None = None,
        auto_start_queued: bool = True,
        ensure_ready: Callable[[], Any] | None = None,
        turn_cleanup: Callable[[str, str], None] | None = None,
        hooks: Callable[[], Any] | None = None,
    ):
        self._id = validate_session_id(session_id)
        self._store = store
        self._lock = lock if lock is not None else SessionLock.for_session(store.directory, self._id)
        self._read: ReadResult | None = None
        self._active: TurnLease | None = None
        self._cancel = CancelToken()
        # Loop dependencies, injected by the runtime (or a test) so this layer
        # never imports a concrete context manager, router, provider, or tools
        # manager. ``tools`` is an opaque per-turn factory returning a bundle
        # exposing ``schemas``/``dispatcher``/``gate``.
        self._assemble = assemble
        self._provider_for = provider_for
        self._limits = limits
        self._tools_for_turn = tools
        self._attended = bool(attended)
        #: The active turn's frozen tool bundle, so ``resolve_permission`` can
        #: reach the approval broker without this layer importing ``nexus.tools``.
        self._active_tools: Any | None = None
        if type(event_buffer) is not int or event_buffer < 1:
            raise ValueError("event_buffer must be a positive integer")
        self._event_buffer = event_buffer
        #: Persistent, session-scoped fan-out. It is created once and outlives
        #: every turn, so a detached turn with zero subscribers still completes
        #: and a late subscriber can replay from the log and then follow live.
        self._bus = Bus(maxsize=event_buffer, policy=DROP_OLDEST)
        #: Number of live :meth:`subscribe` iterators. Attendance is derived from
        #: this count (single user, many views), never sticky.
        self._viewers = 0
        #: Durable input submissions awaiting the next turn boundary.
        self._queue: deque[_QueuedInput] = deque()
        #: Set when no detached turn is running. ``wait_idle`` awaits this
        #: instead of polling, so it never busy-waits.
        self._idle = asyncio.Event()
        self._idle.set()
        #: The current detached turn's task, retained so it is not garbage
        #: collected and so callers can await it.
        self._turn_task: asyncio.Task | None = None
        #: Pending approval request ids observed on the event stream, so a drop
        #: to zero viewers can apply the unattended policy without a UI call.
        self._pending_permissions: set[str] = set()
        #: Decision used when the last viewer leaves with an approval pending.
        self._unattended_decision = (
            DEFAULT_UNATTENDED_DECISION
            if unattended_decision is None
            else unattended_decision
        )
        #: Snapshot cadence seam. ``int`` counts completed turns; a callable is
        #: re-evaluated per completed turn so the integration packet can feed it
        #: ``config.v2.session.snapshot_every`` without this layer importing
        #: configuration. ``None`` disables automatic snapshots.
        self._snapshot_every = snapshot_every
        #: Whether a completed turn automatically starts the next queued
        #: submission. When disabled, queued inputs wait for an explicit
        #: :meth:`start_turn` and :meth:`wait_idle` treats a pending queue as idle.
        self._auto_start_queued = bool(auto_start_queued)
        #: Optional async readiness seam awaited before a turn is prepared (for
        #: example a runtime bootstrapping its extension manifest). Kept opaque so
        #: this layer imports no manager.
        self._ensure_ready = ensure_ready
        #: Optional per-turn cleanup seam called with the finished turn id when a
        #: detached turn retires (success, failure, or cancellation), so a runtime
        #: can drop session/turn-local state such as skill activations.
        self._turn_cleanup = turn_cleanup
        #: Optional provider of the current manifest's hook runner. Called before
        #: a user prompt is persisted and once at close, so ``SessionStart``/
        #: ``UserPromptSubmit``/``SessionEnd`` run against the pinned set and are
        #: persisted through this session's own sink.
        self._hooks_for_session = hooks
        #: Once-per-handle lifecycle flags. ``_session_started`` guards a blocked
        #: first prompt from re-firing; ``_session_ended`` makes ``aclose``
        #: idempotent.
        self._session_started = False
        self._session_ended = False
        #: Set once a session's artifacts have been (or are about to be) moved to
        #: trash. A retired handle is permanently unwritable: no append, no
        #: enqueue, and no turn start. This is what stops a late viewer cleanup,
        #: an unattended fallback, or a scheduled turn from recreating the
        #: deleted log after ``SessionManager.delete`` moved it away.
        self._retired = False
        #: The single shared close task. Every ``aclose``/manager close awaits
        #: this same task, so ``SessionEnd`` runs exactly once and the bus is
        #: closed before any caller returns.
        self._close_task: asyncio.Task | None = None
        #: The last close failure, retained for observation (never raised).
        self.close_error: Exception | None = None
        #: The in-flight queued-turn consumer (a queued submission whose
        #: ``UserPromptSubmit`` hook must run asynchronously before it is durable).
        self._queued_task: asyncio.Task | None = None
        #: The unattended policy frozen at turn start, so a config reload mid-turn
        #: cannot weaken the turn's approval fallback.
        self._turn_unattended: object | None = None
        #: Whether the turn now being prepared is the first in this session, so
        #: ``run_turn`` can fire ``SessionStart`` exactly once.
        self._turn_is_new = False
        #: Most recent background snapshot failure, for diagnostics only.
        self.snapshot_error: Exception | None = None
        #: Records appended by the last :meth:`recover_dangling_tool_uses` call.
        self.recovered: tuple[MessageRecord, ...] = ()
        #: The last durable ``model.selected`` for this session, or ``None`` when
        #: the session still uses the configured default. Rehydrated from the log
        #: on open and refreshed as the event is observed, so a mid-turn selection
        #: is visible to the *next* turn without reading the whole log again.
        self._model_selection: ModelSelection | None = None
        self._agent_selection: AgentSelection | None = None
        self._reasoning_effort_selection: ReasoningEffortSelection | None = None
        self._turn_agent_definition: Any | None = None
        # A crash can leave a submission durable as ``input.queued`` but absent
        # from memory; rebuilding the FIFO here means a reopened handle resumes
        # exactly where the previous process stopped.
        self._rehydrate_queue()
        self._rehydrate_model_selection()
        self._rehydrate_agent_selection()
        self._rehydrate_reasoning_effort_selection()

    # -- wiring ------------------------------------------------------------

    @property
    def can_send(self) -> bool:
        """Whether :meth:`send` has an assembler and provider resolver wired."""
        return self._assemble is not None and self._provider_for is not None

    def bind(
        self,
        *,
        assemble: Callable[[object], Any] | None = None,
        provider_for: Callable[..., Any] | None = None,
        limits: TurnLimits | Callable[[], TurnLimits] | None = None,
        tools: Callable[..., Any] | None = None,
        attended: bool | None = None,
        snapshot_every: int | Callable[[], int] | None = None,
        unattended_decision: object | None = None,
        auto_start_queued: bool | None = None,
        ensure_ready: Callable[[], Any] | None = None,
        turn_cleanup: Callable[[str, str], None] | None = None,
        hooks: Callable[[], Any] | None = None,
    ) -> Session:
        """Attach or replace the loop dependencies used by :meth:`send`."""
        if assemble is not None:
            self._assemble = assemble
        if provider_for is not None:
            self._provider_for = provider_for
        if limits is not None:
            self._limits = limits
        if tools is not None:
            self._tools_for_turn = tools
        if attended is not None:
            self._attended = bool(attended)
        if snapshot_every is not None:
            self._snapshot_every = snapshot_every
        if unattended_decision is not None:
            self._unattended_decision = unattended_decision
        if auto_start_queued is not None:
            self._auto_start_queued = bool(auto_start_queued)
        if ensure_ready is not None:
            self._ensure_ready = ensure_ready
        if turn_cleanup is not None:
            self._turn_cleanup = turn_cleanup
        if hooks is not None:
            self._hooks_for_session = hooks
        return self

    # -- presence ----------------------------------------------------------

    @property
    def viewers(self) -> int:
        """Live subscriber count (single user, many views)."""
        return self._viewers

    @property
    def queue_depth(self) -> int:
        """Number of queued submissions awaiting the next turn boundary."""
        return len(self._queue)

    @property
    def queued_ids(self) -> tuple[str, ...]:
        """Ids of queued submissions in FIFO order (oldest first)."""
        return tuple(item.queued_id for item in self._queue)

    @property
    def pending_permissions(self) -> tuple[str, ...]:
        """Approval request ids observed but not yet resolved."""
        return tuple(self._pending_permissions)

    @property
    def attended(self) -> bool:
        """Whether an interactive approver is attached for this session.

        Derived from presence: any live subscriber counts as attended. The legacy
        explicit flag (constructor/``bind``/``mark_attended``) is retained so the
        single-view CLI keeps prompting; it is OR-ed in, never required.
        """
        return self._viewers > 0 or self._attended

    def mark_attended(self, attended: bool = True) -> Session:
        self._attended = bool(attended)
        return self

    def _live_unattended_policy(self) -> object:
        """The live unattended policy from its (possibly) callable seam.

        The integration packet wires a callable that reads
        ``permissions.on_unattended`` from the effective config, so a config edit
        between turns is honored. A callable that raises falls back to the default
        rather than leaving a pending approval stuck.
        """
        policy = self._unattended_decision
        if callable(policy):
            try:
                return policy()
            except Exception:  # noqa: BLE001 - a bad policy must not wedge a turn
                return DEFAULT_UNATTENDED_DECISION
        return policy

    def _unattended_policy(self) -> object:
        """The policy in force for the active turn, frozen at turn start.

        A running turn uses the value captured by :meth:`_prepare_turn`, so a
        config reload (or any other change to the callable seam) cannot weaken the
        approval fallback mid-turn. With no active turn the live value is used.
        """
        if self._active is not None and self._turn_unattended is not None:
            return self._turn_unattended
        return self._live_unattended_policy()

    def _apply_unattended_fallback(self) -> None:
        """Apply the session's unattended policy to every pending approval.

        The plan's rule: a view that attaches and then disconnects mid-turn must
        not leave the session blocked on an approval nobody will answer. The
        policy vocabulary matches ``permissions.on_unattended``: ``deny`` resolves
        each request with a one-shot deny, ``allow`` with a one-shot allow, and
        ``fail_turn`` fails the running turn. First resolver always wins: an id
        already resolved by a UI is gone from ``_pending_permissions``, and the
        gate rejects a second resolution.
        """
        if self._retired or not self._pending_permissions:
            return
        gate = getattr(self._active_tools, "gate", None)
        if gate is None:
            return
        resolve = getattr(gate, "resolve", None)
        fail = getattr(gate, "fail", None)
        if not callable(resolve) and not callable(fail):
            # No way to answer the prompt: cancel it so the turn cannot hang.
            cancel_pending = getattr(gate, "cancel_pending", None)
            if callable(cancel_pending):
                cancel_pending()
                self._pending_permissions.clear()
            return
        mode = _unattended_mode(self._unattended_policy())
        decision = "allow_once" if mode == "allow" else "deny_once"
        for request_id in list(self._pending_permissions):
            try:
                if mode == "fail_turn" and callable(fail):
                    resolved = bool(fail(request_id, _UNATTENDED_FAIL_REASON))
                elif callable(resolve):
                    resolved = bool(resolve(request_id, decision))
                else:
                    resolved = False
            except Exception:  # noqa: BLE001 - fallback must never crash a view
                resolved = False
            if resolved:
                self._pending_permissions.discard(request_id)

    def _observe_event(self, event: Event) -> None:
        if event.type == "permission.requested":
            request_id = event.data.get("id")
            if request_id:
                self._pending_permissions.add(request_id)
            if not self.attended:
                # The last viewer may have left *before* this approval was
                # requested (a mid-turn disconnect race). Fallback only runs on
                # the disconnect edge, so apply the session's unattended policy
                # here too rather than leaving the turn blocked on a prompt that
                # nobody can answer. The sticky attended flag (headless ``send``)
                # is OR-ed into ``attended``, so an attached headless consumer
                # still resolves manually.
                self._apply_unattended_fallback()
        elif event.type == "permission.resolved":
            request_id = event.data.get("id")
            if request_id:
                self._pending_permissions.discard(request_id)
        elif event.type == _MODEL_SELECTED_EVENT:
            parsed = ModelSelection.from_dict(event.data)
            if parsed is not None:
                self._model_selection = parsed
        elif event.type == _AGENT_SELECTED_EVENT:
            if isinstance(event.data, Mapping) and event.data.get("name") is None:
                self._agent_selection = None
                return
            parsed = AgentSelection.from_dict(event.data)
            if parsed is not None:
                self._agent_selection = parsed
        elif event.type == _REASONING_EFFORT_SELECTED_EVENT:
            parsed = ReasoningEffortSelection.from_dict(event.data)
            if parsed is not None:
                self._reasoning_effort_selection = parsed
        elif event.type in TERMINAL_EVENTS:
            # A terminal turn can never resolve another approval; leaving stale
            # ids behind would let a later viewer-drop "resolve" a dead request.
            self._pending_permissions.clear()

    def resolve_permission(self, request_id: str, decision: object) -> bool:
        """Resolve one active pending approval; unknown/stale ids return False.

        Delegates to the active turn's approval broker. There is no terminal
        coupling: any UI adapter can call this with a
        :class:`~nexus.tools.permissions.Decision` (or its string value).
        """
        bundle = self._active_tools
        gate = getattr(bundle, "gate", None)
        resolve = getattr(gate, "resolve", None)
        if resolve is None:
            return False
        return bool(resolve(request_id, decision))

    # -- model selection ---------------------------------------------------

    @property
    def model_selection(self) -> ModelSelection | None:
        """The session's durable model override, or ``None`` for the default.

        Read from the in-memory mirror (rehydrated from the log on open and
        refreshed on every ``model.selected``), so the turn-start freeze never
        rescans the log.
        """
        return self._model_selection

    def select_model(self, selection: ModelSelection) -> EventRecord | None:
        """Persist one durable model selection and publish it.

        Emitted outside a turn, exactly like ``input.*``/``presence.*``: it
        carries no turn id, gets a monotonic ``seq``, and is rehydrated on
        reopen. A selection is **not** applied to an active turn; the running
        turn's environment was frozen at turn start, so only the next turn picks
        the override up.
        """
        self._ensure_writable()
        return self._emit(_MODEL_SELECTED_EVENT, selection.to_dict())

    def _rehydrate_model_selection(self) -> None:
        """Rebuild the model override from the append-only log.

        A pure read that emits nothing, so reopening a handle never duplicates a
        ``model.selected`` event. The newest event wins; a malformed one is
        skipped rather than failing the open.
        """
        latest: ModelSelection | None = None
        for event in self.read().events():
            if event.type != _MODEL_SELECTED_EVENT:
                continue
            parsed = ModelSelection.from_dict(event.data)
            if parsed is not None:
                latest = parsed
        self._model_selection = latest

    @property
    def agent_selection(self) -> AgentSelection | None:
        """The durable root-agent override, or ``None`` for config/default."""
        return self._agent_selection

    def select_agent(self, selection: AgentSelection) -> EventRecord | None:
        """Persist a root-agent choice for the next turn only."""
        self._ensure_writable()
        return self._emit(_AGENT_SELECTED_EVENT, selection.to_dict())

    def reset_agent(self) -> EventRecord | None:
        """Clear the session override and return to config/default resolution."""
        self._ensure_writable()
        return self._emit(_AGENT_SELECTED_EVENT, {"name": None})

    def _rehydrate_agent_selection(self) -> None:
        latest: AgentSelection | None = None
        for event in self.read().events():
            if event.type == _AGENT_SELECTED_EVENT:
                if isinstance(event.data, Mapping) and event.data.get("name") is None:
                    latest = None
                else:
                    parsed = AgentSelection.from_dict(event.data)
                    if parsed is not None:
                        latest = parsed
        self._agent_selection = latest

    @property
    def reasoning_effort_selection(self) -> ReasoningEffortSelection | None:
        """The durable root reasoning-effort override, or ``None`` for default."""
        return self._reasoning_effort_selection

    def select_reasoning_effort(
        self, selection: ReasoningEffortSelection
    ) -> EventRecord | None:
        """Persist a root-session effort override for subsequent turns only."""
        if not isinstance(selection, ReasoningEffortSelection):
            raise TypeError("selection must be a ReasoningEffortSelection")
        self._ensure_writable()
        return self._emit(_REASONING_EFFORT_SELECTED_EVENT, selection.to_dict())

    def clear_reasoning_effort(self) -> EventRecord | None:
        """Persist a reset to configured/default reasoning effort."""
        self._ensure_writable()
        return self._emit(
            _REASONING_EFFORT_SELECTED_EVENT,
            ReasoningEffortSelection(effort=None).to_dict(),
        )

    def _rehydrate_reasoning_effort_selection(self) -> None:
        """Rebuild the latest valid reasoning-effort selection from the log."""
        latest: ReasoningEffortSelection | None = None
        for event in self.read().events():
            if event.type != _REASONING_EFFORT_SELECTED_EVENT:
                continue
            parsed = ReasoningEffortSelection.from_dict(event.data)
            if parsed is not None:
                latest = parsed
        self._reasoning_effort_selection = latest

    # -- identity ----------------------------------------------------------

    @property
    def id(self) -> str:
        return self._id

    @property
    def path(self) -> Path:
        return self._store.log_path(self._id)

    @property
    def directory(self) -> Path:
        return self._store.directory

    @property
    def snapshot_path(self) -> Path:
        return snapshot_mod.snapshot_path(self.directory, self._id)

    @property
    def active(self) -> bool:
        return self._active is not None

    @property
    def active_turn_id(self) -> str | None:
        return self._active.turn_id if self._active is not None else None

    @property
    def turn_is_new(self) -> bool:
        """Whether the currently active/next turn is the session's first."""
        return self._turn_is_new

    @property
    def retired(self) -> bool:
        """Whether this handle has been retired for a delete (unwritable)."""
        return self._retired

    def retire(self) -> bool:
        """Permanently retire this handle; returns whether it changed state.

        ``SessionManager.delete`` retires the live handle **before** it moves
        any artifact, so a concurrent or late writer — a disconnecting viewer's
        presence cleanup, an unattended fallback, a scheduled turn start — is
        blocked from recreating the deleted log. Retirement is idempotent. The
        manager rolls it back with :meth:`unretire` if the delete fails before
        the move, so a refused/failed delete never strands a usable session.
        """
        if self._retired:
            return False
        self._retired = True
        return True

    def unretire(self) -> None:
        """Roll back a retirement after a delete that failed before the move."""
        self._retired = False

    def _ensure_writable(self) -> None:
        """Refuse any durable write once this handle is retired."""
        if self._retired:
            raise SessionError(
                f"Session {self._id!r} has been deleted; refusing to write"
            )

    # -- history -----------------------------------------------------------

    def read(self, *, force: bool = False) -> ReadResult:
        if force or self._read is None:
            self._read = self._store.read(self._id)
        return self._read

    @property
    def records(self) -> list:
        """Full, authoritative log records; snapshots never omit history."""
        return list(self.read().records)

    @property
    def events(self) -> list[Event]:
        """Full, authoritative log events; snapshots never omit history."""
        return self.read().events()

    @property
    def current(self) -> snapshot_mod.CurrentState:
        """Snapshot-aware current state.

        A valid snapshot supplies the message prefix and usage; the log tail is
        appended. ``events``/``records`` remain the full log. An absent, corrupt,
        stale, or future snapshot degrades cleanly to a full-log projection.
        """
        read = self.read()
        loaded = snapshot_mod.load(self.directory, self._id, read)
        return snapshot_mod.current_state(read, loaded)

    @property
    def messages(self) -> list[Message]:
        return list(self.current.messages)

    def next_seq(self) -> int:
        return self._store.next_seq(self._id)

    def append_message(self, message: Message, *, seq: int | None = None) -> MessageRecord:
        self._ensure_writable()
        record = self._store.append_message(self._id, message, seq=seq)
        self._read = None
        return record

    def append_event(self, event: Event, *, seq: int | None = None) -> EventRecord:
        self._ensure_writable()
        if event.session is None:
            event = msgspec.structs.replace(event, session=self._id)
        record = self._store.append_event(self._id, event, seq=seq)
        self._read = None
        return record

    def append_summary(
        self,
        *,
        text: str = "",
        summary_id: str = "",
        strategy: str = "",
        input_digest: str = "",
        source_from_seq: int = 0,
        source_to_seq: int = 0,
        source_messages: int = 0,
        tokens_before: int | None = None,
        tokens_after: int | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> SummaryRecord:
        """Append one durable summary/compaction artifact (never a transcript turn)."""
        self._ensure_writable()
        record = self._store.append_summary(
            self._id,
            text=text,
            summary_id=summary_id,
            strategy=strategy,
            input_digest=input_digest,
            source_from_seq=source_from_seq,
            source_to_seq=source_to_seq,
            source_messages=source_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            provider=provider,
            model=model,
        )
        self._read = None
        return record

    @property
    def summaries(self) -> list[SummaryRecord]:
        """All durable summary artifacts, in append order."""
        return [
            record
            for record in self.read().records
            if isinstance(record, SummaryRecord)
        ]

    def latest_summary(self) -> SummaryRecord | None:
        """The newest durable summary artifact, or ``None``."""
        latest: SummaryRecord | None = None
        for record in self.read().records:
            if isinstance(record, SummaryRecord):
                latest = record
        return latest

    def summary_for(self, input_digest: str, strategy: str) -> SummaryRecord | None:
        """A durable artifact for the same semantic summary inputs, if any.

        Used to make summarization idempotent: the same ``(inputs, strategy)``
        reuses the existing artifact instead of re-running and re-appending.
        """
        if not input_digest:
            return None
        for record in self.read().records:
            if (
                isinstance(record, SummaryRecord)
                and record.input_digest == input_digest
                and record.strategy == strategy
            ):
                return record
        return None

    def message_seqs(self) -> tuple[int, ...]:
        """The record sequence of every persisted message, in order.

        Used to stamp a summary artifact's source range without the context
        layer importing the session store's record types.
        """
        return tuple(
            record.seq
            for record in self.read().records
            if isinstance(record, MessageRecord)
        )

    # -- snapshots ---------------------------------------------------------

    def write_snapshot(
        self,
        *,
        through_seq: int | None = None,
        summary: SnapshotSummary | None = None,
    ) -> Snapshot:
        """Derive and atomically publish a snapshot; the log is never touched.

        ``through_seq`` defaults to the last persisted sequence. The snapshot's
        messages/usage are derived from the authoritative log prefix, so it is
        always a valid projection at the moment it is written. ``summary`` is
        opaque metadata for a later context packet: it is cached here but its
        authority must live in the log.
        """
        # A snapshot is a session artifact too: never recreate one for a deleted
        # session (``_artifacts_exist`` treats a stray ``.snap.json`` as one).
        self._ensure_writable()
        read = self.read(force=True)
        seq = read.next_seq if through_seq is None else through_seq
        if type(seq) is not int or seq < 0:
            raise ValueError("through_seq must be a non-negative integer")
        if seq > read.next_seq:
            raise SessionError(
                f"snapshot boundary {seq} is beyond the log end {read.next_seq}"
            )
        snapshot = snapshot_mod.build_from_records(
            self._id, read.records, seq, summary=summary
        )
        snapshot_mod.write(
            self.directory, self._id, snapshot, fsync=self._store.fsync
        )
        return snapshot

    def maybe_snapshot(self) -> Snapshot | None:
        """Write a snapshot when the configured completed-turn cadence is due.

        The cadence is the ``snapshot_every`` seam the integration packet feeds
        from ``config.v2.session.snapshot_every``. Counting is durable: it is
        derived from ``turn.completed`` events persisted after the last valid
        snapshot boundary, so a reopened handle resumes the same cadence.
        """
        every = self._snapshot_every
        if callable(every):
            every = every()
        if every is None:
            return None
        if type(every) is not int or every < 1:
            raise ValueError("snapshot_every must be a positive integer or callable")
        read = self.read(force=True)
        prior = snapshot_mod.load(self.directory, self._id, read)
        boundary = prior.seq if prior is not None else 0
        completed = sum(
            1
            for event in read.events()
            if event.type == "turn.completed" and event.seq > boundary
        )
        if completed < every:
            return None
        return self.write_snapshot()

    def _snapshot_after_completed_turn(self) -> None:
        """Cadence hook used by the event sink; never lets snapshot I/O escape."""
        try:
            self.maybe_snapshot()
        except Exception as exc:  # noqa: BLE001 - derived state must not fail a turn
            self.snapshot_error = exc

    # -- event bus ---------------------------------------------------------

    def _publish(self, event: Event) -> None:
        """Fan one persisted event out to every live subscriber."""
        self._bus.publish(event)

    def _emit(
        self, event_type: str, data: dict[str, Any] | None = None
    ) -> EventRecord | None:
        """Persist and publish a session-scoped event (presence/input).

        These transitions are not part of a turn, so they carry no turn id; they
        still get a monotonic ``seq`` and are durable, so a late subscriber can
        reconstruct them from the log.

        Once the handle is retired for a delete this is a **no-op**: a session
        transition such as presence cleanup or a dropped queue must never
        recreate a log that has already been moved to the trash.
        """
        if self._retired:
            return None
        event = Event(type=event_type, data=dict(data or {}), session=self._id)
        record = self.append_event(event)
        self._observe_event(event)
        self._publish(record.event)
        return record

    def _events_after(self, from_seq: int) -> list[Event]:
        """All persisted events with ``seq > from_seq`` (append-only, ordered)."""
        return [
            record.event
            for record in self.read(force=True).records
            if isinstance(record, EventRecord) and record.event.seq > from_seq
        ]

    async def subscribe(
        self,
        from_seq: int = 0,
        *,
        follow: bool = True,
        until: Callable[[Event], bool] | None = None,
    ) -> AsyncIterator[Event]:
        """Catch up from ``from_seq`` then follow live events, gap-free.

        ``from_seq`` is exclusive: events with a greater ``seq`` are yielded.
        The bus subscription is registered **before** the log is read, so an
        event persisted during catch-up is not missed; a running ``last_seq``
        watermark then drops any event already replayed, so nothing is yielded
        twice. If the bounded bus buffer dropped an event under load, the gap is
        healed from the authoritative log before the live event is yielded.

        Closing this iterator unsubscribes the view and updates presence; it
        **never** cancels the turn, which is detached and owned by the session.
        ``until`` stops iteration after the first event it accepts (used by
        :meth:`send` to stop at a terminal event); ``follow=False`` replays the
        log only.
        """
        if type(from_seq) is not int or from_seq < 0:
            raise ValueError("from_seq must be a non-negative integer")
        sub = self._bus.subscribe()
        self._viewers += 1
        last = from_seq
        try:
            # Presence is emitted inside the ``try`` so a failed publish still
            # runs the unsubscribe/decrement cleanup below rather than leaking a
            # subscriber count (and the bus registration) forever.
            self._emit("presence.joined", {"viewers": self._viewers})
            self._emit(
                "presence.changed",
                {"viewers": self._viewers, "attended": self.attended},
            )
            for event in self._events_after(from_seq):
                last = event.seq
                yield event
                if until is not None and until(event):
                    return
            if not follow:
                return
            while True:
                event = await sub.get()
                if event.seq <= last:
                    continue
                if event.seq > last + 1:
                    for missing in self._events_after(last):
                        if missing.seq >= event.seq:
                            break
                        last = missing.seq
                        yield missing
                        if until is not None and until(missing):
                            return
                if event.seq <= last:
                    continue
                last = event.seq
                yield event
                if until is not None and until(event):
                    return
        finally:
            self._bus.unsubscribe(sub)
            self._viewers = max(0, self._viewers - 1)
            # Cleanup must complete even if persisting/publishing a presence
            # event fails, and must not mask an in-flight iteration error.
            with contextlib.suppress(Exception):
                self._emit("presence.left", {"viewers": self._viewers})
            with contextlib.suppress(Exception):
                self._emit(
                    "presence.changed",
                    {"viewers": self._viewers, "attended": self.attended},
                )
            if self._viewers == 0:
                with contextlib.suppress(Exception):
                    self._apply_unattended_fallback()

    # -- input queue -------------------------------------------------------

    def enqueue(self, user_input: str | list[ContentBlock]) -> str:
        """Persist a submission to run at the next turn boundary; return its id.

        The input is appended as an ``input.queued`` event (durable, drawable)
        and held in a FIFO in-memory queue. It is consumed either by an explicit
        :meth:`start_turn` with no content or automatically when the running turn
        completes, emitting ``input.consumed``; if it can never run it emits
        ``input.dropped``.
        """
        self._ensure_writable()
        content = _coerce_user_input(user_input)
        queued_id = new_id()
        self._queue.append(_QueuedInput(queued_id, content))
        self._emit(
            "input.queued",
            {
                "queued_id": queued_id,
                "content": _encode_queued_content(content),
                "queue_depth": len(self._queue),
            },
        )
        return queued_id

    def _drop_queue(self, reason: str | None = None) -> None:
        while self._queue:
            item = self._queue.popleft()
            self._emit(
                "input.dropped",
                {"queued_id": item.queued_id, "reason": reason or "dropped"},
            )

    def _rehydrate_queue(self) -> None:
        """Rebuild the pending input FIFO from the append-only input events.

        A process that died between ``input.queued`` and its
        ``input.consumed``/``input.dropped`` leaves the submission durable but
        absent from memory. Replaying the events in order restores the exact
        pending order. This is a pure read: it emits nothing, so reopening never
        duplicates an ``input.queued`` (or any other) event, and a malformed
        payload is skipped so a corrupt line can never break an open. The
        rehydrated queue then behaves exactly like one built in-process: the
        next ``start_turn`` (or completed-turn boundary) consumes it FIFO.
        """
        pending: dict[str, _QueuedInput] = {}
        for event in self.read().events():
            if event.type not in _INPUT_EVENT_TYPES:
                continue
            data = event.data
            if not isinstance(data, Mapping):
                continue
            queued_id = data.get("queued_id")
            if not isinstance(queued_id, str) or not queued_id:
                continue
            if event.type == _QUEUED_EVENT:
                content = _decode_queued_content(data.get("content"))
                if content is None:
                    continue
                pending[queued_id] = _QueuedInput(queued_id, content)
            else:
                pending.pop(queued_id, None)
        self._queue = deque(pending.values())

    # -- turn ownership and cancellation -----------------------------------

    @property
    def cancel_token(self) -> CancelToken:
        return self._cancel

    def cancel(self, reason: str | None = None, *, drop_queue: bool = True) -> None:
        """Cancel the active turn and (by default) drop queued submissions."""
        self._cancel.cancel(reason)
        # A cancelled turn can never resolve an approval; forget the ids now so a
        # later viewer-drop cannot act on a request the loop is already tearing
        # down. The terminal ``turn.cancelled`` event clears them again.
        self._pending_permissions.clear()
        if drop_queue:
            self._drop_queue(reason or "cancelled")

    def begin_turn(
        self,
        *,
        turn_id: str | None = None,
        limits: TurnLimits | None = None,
    ) -> TurnLease:
        """Claim exclusive turn ownership, or raise :class:`SessionBusy`.

        Acquires the exclusive session lock non-blocking (cross-process safety)
        and installs a fresh cancellation token. The caller owns the returned
        lease and must release it.
        """
        if self._active is not None:
            raise SessionBusy(f"Session {self._id!r} already has an active turn")
        self._lock.acquire(shared=False, blocking=False)
        try:
            # Re-check under the lock: a concurrent delete retires the handle and
            # moves the log while holding the same exclusive lock, so acquiring it
            # after the delete must fail closed rather than recreate the log.
            if self._retired:
                raise SessionError(
                    f"Session {self._id!r} has been deleted; refusing to start a turn"
                )
            self._idle.clear()
            token = CancelToken()
            state = TurnState.new(
                turn_id=turn_id or new_id(), session_id=self._id
            ).start()
        except BaseException:
            # Never leak the flock if lease construction fails after acquiring.
            self._lock.release()
            self._idle.set()
            raise
        lease = TurnLease(
            turn_id=state.turn_id,
            state=state,
            cancel_token=token,
            limits=limits,
            _release=lambda: self._end_turn(state.turn_id),
        )
        self._active = lease
        self._cancel = token
        return lease

    def _end_turn(self, turn_id: str) -> None:
        active = self._active
        if active is not None and active.turn_id != turn_id:
            # A newer turn owns the session; a stale lease must not release it.
            return
        ended = active is not None
        self._active = None
        self._turn_unattended = None
        # Defensive: release even when ``_active`` was already cleared, so a
        # lease that was detached without releasing cannot leak the flock.
        self._lock.release()
        if ended:
            # Terminal/cancel cleanup seam: a finished turn can never leave a
            # session/turn-local activation behind to leak into a later turn.
            cleanup = self._turn_cleanup
            if cleanup is not None:
                with contextlib.suppress(Exception):
                    cleanup(self._id, turn_id)
        if self._turn_task is None:
            # No detached task (for example a preparation that failed before
            # launch): the session is idle.
            self._idle.set()

    # -- crash recovery ----------------------------------------------------

    def recover_dangling_tool_uses(self, *, lock: bool = True) -> list[MessageRecord]:
        """Append an error result for every ``ToolUse`` with no matching result.

        Recovery is deterministic and idempotent: it only reads persisted
        records, never executes a call, and once results are appended a second
        call finds nothing unresolved.

        With ``lock=True`` (the default) the exclusive lock is taken
        non-blocking; if another process is actively running the session the
        method returns an empty list rather than racing it.
        """
        if self._retired:
            # A deleted session is never recovered: its log belongs to the trash.
            return []
        if lock:
            if self._lock.held:
                # This handle already owns the active turn; never write while it runs.
                return []
            try:
                self._lock.acquire(shared=False, blocking=False)
            except SessionBusy:
                return []
            try:
                return self._recover_locked()
            finally:
                self._lock.release()
        return self._recover_locked()

    def _recover_locked(self) -> list[MessageRecord]:
        issued: dict[str, ToolUse] = {}
        resolved: set[str] = set()
        for message in self.messages:
            for block in message.content:
                if isinstance(block, ToolUse):
                    issued.setdefault(block.id, block)
                elif isinstance(block, ToolResult):
                    resolved.add(block.tool_use_id)
        pending = [call for call_id, call in issued.items() if call_id not in resolved]
        if not pending:
            self.recovered = ()
            return []
        content = [
            ToolResult(
                tool_use_id=call.id,
                content=[Text(text=_INTERRUPTED_TEMPLATE.format(tool_use_id=call.id, name=call.name))],
                is_error=True,
            )
            for call in pending
        ]
        record = self.append_message(Message(role="user", content=content))
        self.recovered = (record,)
        return [record]

    # -- turn execution ----------------------------------------------------

    def _assemble_for_turn(self) -> Any:
        """Freeze a per-turn assembler snapshot, if the assembler supports it.

        ``ContextManager.for_turn`` returns a manager bound to one effective
        config and rendered system text, so every iteration of this turn sees the
        same model/sampling/provider/system. Assemblers without ``for_turn`` (for
        example test doubles) are used as-is.
        """
        for_turn = getattr(self._assemble, "for_turn", None)
        if callable(for_turn):
            # A session-aware assembler (the runtime's coordinator) is handed the
            # session so a per-session model override is read and frozen once, at
            # turn start. A mid-turn selection therefore affects only the next
            # turn. Plain test doubles are called with no arguments as before.
            if getattr(self._assemble, "session_aware", False):
                return for_turn(session=self)
            return for_turn()
        return self._assemble

    def _limits_for_turn(self, assembler: Any) -> TurnLimits | None:
        """Derive limits from the same snapshot used for assembly.

        The context snapshot exposes ``turn_limits()`` from its frozen config, so
        a single reload feeds both the limits and every assemble call. An
        assembler without that hook falls back to the injected limits source.
        """
        turn_limits = getattr(assembler, "turn_limits", None)
        if callable(turn_limits):
            return turn_limits()
        if callable(self._limits):
            return self._limits()
        return self._limits

    def _tool_turn_for(self, assembler: Any, lease: TurnLease, attended: bool | None) -> Any:
        """Build the frozen per-turn tool bundle, or ``None`` when unwired.

        Called during the snapshot phase (before any log mutation) so an unknown
        profile or tool-config error releases the lease without a partial turn.
        """
        if self._tools_for_turn is None:
            return None
        config: Any | None = None
        effective = getattr(assembler, "effective_config", None)
        if callable(effective):
            config = effective()
        bundle = self._tools_for_turn(
            config=config,
            session=self,
            turn_id=lease.turn_id,
            attended=self.attended if attended is None else bool(attended),
        )
        if bundle is not None:
            freeze = getattr(assembler, "freeze_tools", None)
            if callable(freeze):
                freeze(getattr(bundle, "schemas", ()))
        return bundle

    def _resolve_turn_input(
        self, user_input: str | list[ContentBlock] | None
    ) -> tuple[list[ContentBlock], _QueuedInput | None]:
        """Return the turn's content and, if it comes from the queue, its item.

        The queue head is **peeked**, not popped: the caller removes it only
        after the lease and config snapshot succeed, so a failed start (for
        example :class:`SessionBusy`) leaves the durable FIFO intact instead of
        silently losing the submission from memory.
        """
        if user_input is None:
            if not self._queue:
                raise ValueError("no queued input to start a turn")
            item = self._queue[0]
            return item.content, item
        return _coerce_user_input(user_input), None

    def _remove_queued(self, item: _QueuedInput) -> bool:
        """Remove ``item`` from the FIFO by identity; ``False`` if absent."""
        for index, candidate in enumerate(self._queue):
            if candidate is item:
                del self._queue[index]
                return True
        return False

    # -- lifecycle hooks ---------------------------------------------------

    def _hook_runner(self) -> Any | None:
        """The current manifest's hook runner, or ``None`` when hooks are off."""
        provider = self._hooks_for_session
        if provider is None:
            return None
        try:
            return provider()
        except Exception:  # noqa: BLE001 - a broken provider never blocks a turn
            return None

    def _persist_hook_outcome(self, outcome: Any, event: str) -> None:
        """Persist ``hook.fired``/``hook.blocked`` through this session's sink."""
        to_dict = getattr(outcome, "to_dict", None)
        payload = to_dict() if callable(to_dict) else {}
        if not isinstance(payload, Mapping):
            return
        for decision in payload.get("decisions", ()) or ():
            if not isinstance(decision, Mapping):
                continue
            self._emit(
                "hook.fired",
                {
                    "event": event,
                    "hook": decision.get("hook"),
                    "action": decision.get("action"),
                },
            )
        if str(payload.get("decision")) == "block":
            self._emit(
                "hook.blocked",
                {
                    "event": event,
                    "reason": payload.get("reason") or "blocked by hook",
                },
            )

    async def _run_session_hook(
        self,
        runner: Any,
        event: str,
        *,
        data: Mapping[str, Any] | None = None,
        tool_input: Mapping[str, Any] | None = None,
    ) -> Any | None:
        """Run one lifecycle hook, persisting its decisions; never wedges a turn."""
        if runner is None:
            return None
        try:
            if tool_input is not None and event == "UserPromptSubmit":
                submit = getattr(runner, "user_prompt_submit", None)
                if callable(submit):
                    outcome = await submit(
                        content=tool_input.get("content", []),
                        session_id=self._id,
                        turn_id="",
                    )
                else:
                    outcome = await runner.lifecycle(
                        event,
                        session_id=self._id,
                        turn_id="",
                        data=dict(data or {}),
                    )
            else:
                outcome = await runner.lifecycle(
                    event, session_id=self._id, turn_id="", data=dict(data or {})
                )
        except (OperationCancelled, asyncio.CancelledError):
            raise
        except Exception:  # noqa: BLE001 - a broken hook never wedges a prompt
            return None
        self._persist_hook_outcome(outcome, event)
        return outcome

    @staticmethod
    def _block_to_dict(block: ContentBlock) -> dict[str, Any]:
        try:
            return dict(msgspec.structs.asdict(block))
        except Exception:  # noqa: BLE001 - an unknown block type is opaque
            return {"type": type(block).__name__}

    @staticmethod
    def _coerce_prompt_modified(modified: Any) -> list[ContentBlock] | None:
        """Parse a ``UserPromptSubmit`` modify into content blocks, or ``None``.

        A hook may return a ``text`` string or a ``content`` list of blocks; any
        other shape is ignored rather than crashing the prompt.
        """
        if not isinstance(modified, Mapping):
            return None
        text = modified.get("text")
        if isinstance(text, str):
            return [Text(text=text)] if text.strip() else None
        raw = modified.get("content")
        if raw is None:
            return None
        if isinstance(raw, str):
            return [Text(text=raw)] if raw.strip() else None
        if isinstance(raw, (list, tuple)):
            blocks: list[ContentBlock] = []
            for item in raw:
                if isinstance(item, ContentBlock):
                    blocks.append(item)
                elif isinstance(item, Mapping):
                    try:
                        blocks.append(msgspec.convert(item, type=ContentBlock))
                    except (
                        msgspec.ValidationError,
                        msgspec.DecodeError,
                        TypeError,
                        ValueError,
                    ):
                        return None
                else:
                    return None
            return blocks or None
        return None

    async def _gate_user_prompt(
        self, content: list[ContentBlock]
    ) -> tuple[list[ContentBlock], str | None]:
        """Run ``SessionStart``/``UserPromptSubmit`` before the prompt is durable.

        Returns ``(content, block_reason)``. A non-``None`` ``block_reason`` means
        the prompt must not be appended: nothing was written, and the caller
        fails the action with that reason. A modify is parsed safely into content
        blocks and persisted/assembled instead of the original.
        """
        runner = self._hook_runner()
        if runner is None:
            return content, None
        if not self._session_started and not self.read().records:
            await self._run_session_hook(runner, "SessionStart")
            self._session_started = True
        outcome = await self._run_session_hook(
            runner,
            "UserPromptSubmit",
            data={"content": [self._block_to_dict(b) for b in content]},
            tool_input={"content": [self._block_to_dict(b) for b in content]},
        )
        if outcome is None:
            return content, None
        if getattr(outcome, "blocked", False):
            reason = str(getattr(outcome, "reason", "") or "").strip()
            return content, reason or "blocked by UserPromptSubmit hook"
        if getattr(outcome, "modified", False):
            modified = self._coerce_prompt_modified(
                getattr(outcome, "modified_input", None)
            )
            if modified is not None:
                return modified, None
        return content, None

    def _fail_blocked_prompt(self, reason: str) -> str:
        """Emit a persisted failed turn for a hook-blocked prompt; no user message.

        The session never acquires a lease, so there is no active turn to leak.
        A synthetic ``turn.started``/``turn.failed`` pair keeps the stream
        well-formed for a UI that pairs terminal events.
        """
        turn_id = new_id()
        limits_data: dict[str, Any] = {}
        try:
            limits = self._limits() if callable(self._limits) else self._limits
            if limits is not None:
                limits_data = {
                    "max_iterations": limits.max_iterations,
                    "max_seconds": limits.max_seconds,
                }
        except Exception:  # noqa: BLE001 - limits are advisory here
            limits_data = {}
        self._emit("turn.started", {"limits": limits_data})
        self._emit(
            "turn.failed",
            {"error": f"UserPromptSubmit blocked: {reason}", "iterations": 0},
        )
        return turn_id

    def _prepare_turn(
        self,
        content: list[ContentBlock],
        attended: bool,
        turn_id: str | None,
        limits: TurnLimits | None,
        *,
        queued_item: _QueuedInput | None = None,
    ) -> tuple[TurnLease, Any, Any]:
        """Claim the lease, snapshot config, and persist the user message.

        Synchronous by design: the lease is claimed and the log is mutated with
        no intervening ``await``, so a concurrent turn can never observe a
        half-prepared turn. Any snapshot failure releases the lease without
        touching the log. A queued submission is removed from the FIFO only
        after the lease/snapshot succeed and its ``input.consumed`` event is
        durable, so a failed preparation cannot lose it.
        """
        lease = self.begin_turn(turn_id=turn_id, limits=limits)
        # Freeze the unattended fallback policy for the whole turn: a reload that
        # arrives mid-turn must not be able to weaken (or strengthen) it.
        self._turn_unattended = self._live_unattended_policy()
        try:
            self._turn_is_new = not self.read().records
            self._turn_agent_definition = None
            self._turn_agent_selection_source = None
            assembler = self._assemble_for_turn()
            # An explicit per-turn ``limits`` argument wins over the config
            # snapshot; only derive from the snapshot when the caller passed none.
            if limits is None:
                lease.limits = self._limits_for_turn(assembler)
            tool_turn = self._tool_turn_for(assembler, lease, attended)
            # Keep the definition resolved at turn start even when an explicit
            # model selection suppresses the assembler's agent defaults. It is
            # still the effective root agent for prompts/tools and turn history.
            agent = self._turn_agent_definition or getattr(
                assembler, "agent_definition", None
            )
            if agent is not None:
                manager = getattr(tool_turn, "manager", None)
                self._turn_agent_selection_source = getattr(
                    assembler, "agent_selection_source", "default"
                )
                self._turn_agent_metadata = {
                    "name": agent.name,
                    "source": getattr(
                        getattr(assembler, "agent_selection_source", None),
                        "value",
                        getattr(assembler, "agent_selection_source", "default"),
                    ),
                    "fingerprint": agent.fingerprint(),
                    "read_only": bool(agent.read_only),
                    "profile": agent.profile or getattr(manager, "profile", None),
                    "tools": list(getattr(manager, "names", ())),
                }
            else:
                self._turn_agent_metadata = {}
        except BaseException:
            self._turn_agent_definition = None
            self._turn_unattended = None
            lease.release()
            raise
        self._active_tools = tool_turn
        try:
            if queued_item is not None:
                # Emit before removing: if the durable event fails, the item
                # stays queued and memory still matches the log.
                self._emit(
                    "input.consumed",
                    {"queued_id": queued_item.queued_id, "turn": lease.turn_id},
                )
                self._remove_queued(queued_item)
            self.recover_dangling_tool_uses(lock=False)
            self.append_message(
                Message(
                    role="user",
                    content=content,
                    meta=MessageMeta(turn_id=lease.turn_id),
                )
            )
        except BaseException:
            # From here the log may be partially written; still release ownership
            # so the session is not wedged by a failed preparation.
            self._active_tools = None
            self._turn_agent_definition = None
            self._turn_unattended = None
            lease.release()
            raise
        return lease, assembler, tool_turn

    def _launch_turn(
        self,
        lease: TurnLease,
        content: list[ContentBlock],
        assembler: Any,
        tool_turn: Any,
        *,
        fanout: _Fanout | None = None,
        input_id: str | None = None,
        input_content: list[ContentBlock] | None = None,
    ) -> asyncio.Task:
        """Start the detached producer task for a prepared turn."""
        sink = _SessionEventSink(self, fanout)
        task = asyncio.create_task(
            run_turn(
                session=self,
                user_input=content,
                assemble=assembler,
                provider_for=self._provider_for,
                emit=sink,
                tools=getattr(tool_turn, "dispatcher", None),
                gate=getattr(tool_turn, "gate", None),
                lease=lease,
                persist_user_message=False,
                input_id=input_id,
                input_content=input_content,
                manifest_ref=getattr(tool_turn, "manifest_ref", None),
                environment_for=getattr(tool_turn, "environment_for", None),
                hooks=getattr(tool_turn, "hooks", None),
            )
        )
        self._turn_task = task
        if fanout is not None:
            task.add_done_callback(lambda _task: fanout.close())
        task.add_done_callback(self._on_turn_done)
        return task

    def _on_turn_done(self, task: asyncio.Task) -> None:
        """Retire a finished turn and consume the next queued submission."""
        if self._turn_task is not task:
            # A newer turn already took ownership; never clobber its state.
            return
        self._turn_task = None
        self._active_tools = None
        self._turn_agent_definition = None
        # Defensive lease release. ``run_turn`` releases in its own ``finally``
        # on every normal path, but a task cancelled before it first runs (for
        # example during loop shutdown) never reaches that block. Releasing here
        # guarantees the flock cannot be leaked by such a cancellation.
        lease = self._active
        if lease is not None:
            lease.release()
        self._launch_queued_if_idle()
        if self._turn_task is None:
            self._idle.set()

    def _launch_queued_if_idle(self) -> None:
        """Start the next queued turn at a turn boundary, if one is waiting.

        Gated by ``auto_start_queued``: when disabled a completed turn never
        silently promotes the queue, so the session settles with the submission
        pending until an explicit :meth:`start_turn`. With hooks configured the
        ``UserPromptSubmit`` gate is asynchronous, so consumption is scheduled;
        without hooks it stays synchronous (the pre-hook behaviour).
        """
        if not self._auto_start_queued:
            return
        if self._active is not None or not self._queue:
            return
        if self._queued_task is not None and not self._queued_task.done():
            return
        if self._hook_runner() is None:
            item = self._queue[0]
            self._start_queued_turn(item.content, item)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._queued_task = loop.create_task(self._consume_queued_turn())

    def _start_queued_turn(
        self, content: list[ContentBlock], item: _QueuedInput
    ) -> None:
        """Prepare and launch one queued turn from already-gated content."""
        try:
            lease, assembler, tool_turn = self._prepare_turn(
                content, self.attended, None, None, queued_item=item
            )
        except BaseException:  # noqa: BLE001 - a bad queued turn must not crash the drain
            # Drop only if it is still pending. If preparation got past the
            # consumed transition before failing, a second ``input.dropped``
            # would contradict the durable ``input.consumed``.
            if self._remove_queued(item):
                self._emit(
                    "input.dropped",
                    {"queued_id": item.queued_id, "reason": "turn start failed"},
                )
            return
        try:
            self._launch_turn(lease, content, assembler, tool_turn, fanout=None)
        except BaseException:  # noqa: BLE001 - release the lease, then keep draining
            self._active_tools = None
            lease.release()
            if self._remove_queued(item):
                self._emit(
                    "input.dropped",
                    {"queued_id": item.queued_id, "reason": "turn launch failed"},
                )

    async def _consume_queued_turn(self) -> None:
        """Run the queued prompt's hooks, then prepare and launch its turn."""
        if self._active is not None or not self._queue:
            return
        item = self._queue[0]
        content, block_reason = await self._gate_user_prompt(item.content)
        if block_reason is not None:
            if self._remove_queued(item):
                self._emit(
                    "input.dropped",
                    {
                        "queued_id": item.queued_id,
                        "reason": f"UserPromptSubmit blocked: {block_reason}",
                    },
                )
            self._fail_blocked_prompt(block_reason)
            return
        self._start_queued_turn(content, item)

    async def start_turn(
        self,
        user_input: str | list[ContentBlock] | None = None,
        *,
        attended: bool | None = None,
        turn_id: str | None = None,
        limits: TurnLimits | None = None,
    ) -> str:
        """Start one turn detached from any subscriber; return its ``turn_id``.

        The turn runs to completion as a session-owned task, so closing or never
        opening a subscriber does not affect it. With ``user_input=None`` the
        head of the input queue is consumed instead (emitting ``input.consumed``).

        Configuration is snapshotted once, synchronously, before the task starts;
        a snapshot failure releases the lease without touching the log.
        """
        if self._assemble is None or self._provider_for is None:
            raise SessionError(
                "Session.start_turn requires an assembler and provider resolver; "
                "open the session through Runtime.session() or call bind() first"
            )
        self._ensure_writable()
        if self._ensure_ready is not None:
            await self._ensure_ready()
        # Re-check after the await: a delete can retire the handle while the
        # readiness seam yields, and a retired handle must never write.
        self._ensure_writable()
        content, queued_item = self._resolve_turn_input(user_input)
        content, block_reason = await self._gate_user_prompt(content)
        if block_reason is not None:
            if queued_item is not None and self._remove_queued(queued_item):
                self._emit(
                    "input.dropped",
                    {
                        "queued_id": queued_item.queued_id,
                        "reason": f"UserPromptSubmit blocked: {block_reason}",
                    },
                )
            return self._fail_blocked_prompt(block_reason)
        effective = self.attended if attended is None else bool(attended)
        lease, assembler, tool_turn = self._prepare_turn(
            content,
            effective,
            turn_id,
            limits,
            queued_item=queued_item,
        )
        try:
            self._launch_turn(
                lease,
                content,
                assembler,
                tool_turn,
                fanout=None,
                input_id=new_id() if queued_item is None else None,
                input_content=_encode_queued_content(content) if queued_item is None else None,
            )
        except BaseException:
            self._active_tools = None
            lease.release()
            raise
        return lease.turn_id

    def fail_turn(
        self,
        turn_id: str,
        error: str,
        *,
        reason: str = "",
    ) -> None:
        """Persist a synthetic terminal ``turn.failed`` for a turn that never ran.

        The host supervisor schedules a turn by calling :meth:`start_turn`; if
        that raises (a bad config snapshot, an unwired session, a readiness
        failure, ...) no producer ever runs, so a follower would stream forever
        waiting for a terminal event. This makes the failure durable and
        session-scoped — carrying the pre-assigned ``turn_id`` — so ``run``,
        ``chat``, and JSONL terminate, and a late subscriber reconstructs the
        same failed turn from the log.

        Idempotence is the caller's concern: it is called exactly once by the
        supervisor for a submission whose start raised.
        """
        event = Event(
            type="turn.failed",
            data={
                "error": error,
                "reason": reason or error,
                "iterations": 0,
                "turn_id": turn_id,
            },
            session=self._id,
            turn=turn_id,
        )
        record = self.append_event(event)
        self._observe_event(event)
        self._publish(record.event)

    async def wait_turn(self, turn_id: str | None = None) -> None:
        """Await the current detached turn (optionally a specific ``turn_id``).

        Awaiting a turn never cancels it: the shield means a cancelled waiter
        leaves the session-owned task running.
        """
        task = self._turn_task
        if task is None:
            return
        current = self.active_turn_id
        if turn_id is not None and current is not None and current != turn_id:
            return
        await self._await_detached(task)

    async def _await_detached(self, task: asyncio.Task) -> None:
        """Await a detached turn without letting waiter cancellation cancel it.

        ``asyncio.shield`` keeps the session-owned turn running when the waiter
        is cancelled, but the ``CancelledError`` is **re-raised** so
        ``asyncio.wait_for`` reports its timeout instead of silently returning.
        A turn that was itself cancelled is treated as finished, preserving the
        legacy "awaiting a cancelled turn returns quietly" behaviour.
        """
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                return
            raise

    async def wait_idle(self) -> None:
        """Wait until no detached turn is running.

        Cancellable and ``asyncio.wait_for``-compatible: a cancelled waiter (or a
        timeout) propagates, but never cancels the detached, session-owned turn.

        This never busy-waits: it awaits the session's idle event, which is set
        when the running task is retired. A queued submission with no running
        turn is **idle** — queued inputs are consumed at the next turn boundary
        (when ``auto_start_queued`` is enabled) or by an explicit
        :meth:`start_turn` — so this returns instead of spinning on the queue.
        Callers that want a pending submission to run should call
        :meth:`start_turn`.
        """
        while self._turn_task is not None:
            self._idle.clear()
            # Re-check after clearing: a turn may have finished between the loop
            # condition and the clear, in which case the event is already set.
            if self._turn_task is None:
                break
            await self._idle.wait()

    async def send(
        self, user_input: str | list[ContentBlock], *, attended: bool | None = None
    ) -> AsyncIterator[Event]:
        """Run one turn and stream its persisted events in order (attached).

        This is the compatibility wrapper for the single-view CLI: unlike
        :meth:`subscribe`, the consumer owns the turn's lifetime, so closing the
        stream early cancels the turn and releases the lease. It is implemented
        over the same prepared-turn/turn-runner core as :meth:`start_turn`, with
        a private backpressured fan-out so a slow consumer never loses an event.
        """
        if self._assemble is None or self._provider_for is None:
            raise SessionError(
                "Session.send requires an assembler and provider resolver; "
                "open the session through Runtime.session() or call bind() first"
            )
        self._ensure_writable()
        if self._ensure_ready is not None:
            await self._ensure_ready()
        self._ensure_writable()
        content = _coerce_user_input(user_input)
        # ``UserPromptSubmit`` runs before anything is durable; capture the log
        # watermark so the blocked path can yield exactly the events it emitted.
        watermark = self.next_seq() - 1
        content, block_reason = await self._gate_user_prompt(content)
        if block_reason is not None:
            self._fail_blocked_prompt(block_reason)
            for event in self._events_after(watermark):
                yield event
            return
        # The legacy sticky flag (not presence) drives attendance here, so a
        # headless JSON consumer stays unattended even while it is attached.
        effective = self._attended if attended is None else bool(attended)
        fanout = _Fanout(self._event_buffer)
        lease, assembler, tool_turn = self._prepare_turn(content, effective, None, None)
        # Every event persisted before the producer started (the gate's hook
        # events, and a queued ``input.consumed``) is replayed first, because the
        # fan-out only captures what the loop emits after launch.
        pre_launch_events = self._events_after(watermark)
        try:
            task = self._launch_turn(
                lease,
                content,
                assembler,
                tool_turn,
                fanout=fanout,
                input_id=new_id(),
                input_content=_encode_queued_content(content),
            )
        except BaseException:
            self._active_tools = None
            lease.release()
            raise
        try:
            for event in pre_launch_events:
                yield event
            while True:
                item = await fanout.get()
                if item is _SENTINEL:
                    break
                yield item
        finally:
            try:
                if not task.done():
                    self.cancel("consumer closed")
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            finally:
                # The loop releases the lease in its own ``finally``; this is an
                # outermost safety net (e.g. a producer cancelled before it
                # entered the loop, or a task that raised). Only clear the active
                # tool bundle if no newer (e.g. queued) turn has taken over.
                if self._active is None or self._active.turn_id == lease.turn_id:
                    self._active_tools = None
                lease.release()

    # -- close -------------------------------------------------------------

    async def aclose(self, *, hook_timeout: float = 5.0) -> None:
        """Close this handle: fire ``SessionEnd`` once, then close the bus.

        The hook runs against the *current* manifest's pinned set and its events
        are persisted before the bus closes. A hook timeout or failure is
        swallowed so a wedged close can never hang shutdown. Every concurrent or
        repeated caller awaits the **same** tracked close task, so ``SessionEnd``
        fires exactly once and the bus is closed before any caller returns.
        """
        task = self._close_task
        if task is None:
            task = asyncio.ensure_future(self._close_session(hook_timeout))
            self._close_task = task
        # Shield so a cancelled *waiter* never cancels the shared close; the
        # CancelledError still propagates to that waiter.
        await asyncio.shield(task)

    async def _close_session(self, hook_timeout: float) -> None:
        if self._session_ended:
            return
        self._session_ended = True
        try:
            # Stop an in-flight queued consumer and any active turn first, so no
            # writer races the final events.
            queued, self._queued_task = self._queued_task, None
            if queued is not None and not queued.done():
                queued.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await queued
            task = self._turn_task
            if task is not None and not task.done():
                self.cancel("session closed")
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._await_detached(task)
            runner = self._hook_runner()
            if runner is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self._run_session_hook(runner, "SessionEnd"),
                        timeout=hook_timeout,
                    )
        except Exception as exc:  # noqa: BLE001 - observe, never wedge a close
            self.close_error = exc
        finally:
            with contextlib.suppress(Exception):
                await self._bus.aclose()


__all__ = [
    "DEFAULT_EVENT_BUFFER",
    "DEFAULT_UNATTENDED_DECISION",
    "TERMINAL_EVENTS",
    "Session",
    "TurnLease",
]
