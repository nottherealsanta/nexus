"""The public session handle for Phase 1 (plan section 5.1).

``Session`` is the substrate the loop and UI adapters build on. It provides:

* validated identity and append-only history/event APIs over the JSONL store;
* exclusive active-turn ownership via the session lock, so two turns cannot run
  against one session in-process or across processes;
* cancellation-token plumbing (a fresh token per turn);
* dangling ``ToolUse`` crash recovery: if the process died after an assistant
  tool call was persisted but before its results were, recovery appends one
  model-visible error ``ToolResult`` per unresolved call **without executing
  anything**;
* :meth:`send`, which snapshots configuration once per turn, drives
  :func:`nexus.core.loop.run_turn` as a producer, persists every event before
  fanning it out through a bounded lossless buffer, and yields those events in
  order.

Still absent until later packets: permissions, snapshots, and
fork/list/delete/replay. The seams they will build on are
:meth:`append_message`, :meth:`append_event`, :meth:`begin_turn`,
:meth:`cancel`, :meth:`recover_dangling_tool_uses`, :meth:`send`, and
:meth:`bind`.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import msgspec

from ..core.cancel import CancelToken
from ..core.loop import run_turn
from ..core.turn import TurnLimits, TurnState
from ..errors import SessionBusy, SessionError
from ..events import Event
from ..model.message import (
    ContentBlock,
    Message,
    MessageMeta,
    Text,
    ToolResult,
    ToolUse,
)
from ..util import new_id
from . import snapshot as snapshot_mod
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

#: Default fan-out capacity. Non-critical events block the producer until the
#: consumer makes room, so this bounds memory without ever dropping an event.
DEFAULT_EVENT_BUFFER = 512


def _coerce_user_input(user_input: str | list[ContentBlock]) -> list[ContentBlock]:
    """Validate and normalize a ``send`` argument before any lock is taken."""
    if isinstance(user_input, str):
        return [Text(text=user_input)]
    blocks = list(user_input)
    if not blocks:
        raise ValueError("user_input must contain at least one content block")
    return blocks


class _Fanout:
    """Bounded single-producer/single-consumer buffer with true backpressure.

    Non-critical emissions ``await`` space, so every persisted UI event is
    delivered exactly once and nothing is dropped. Terminal/error/sentinel
    insertions are non-blocking (and may exceed the soft bound) so cleanup can
    always complete even when the consumer has gone away.
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
    """Persist-then-fan-out sink: JSONL first, bounded buffer second.

    The loop emits in strict order and the buffer is FIFO, so consumers observe
    exactly the persisted ordering. Persisting before enqueuing means an event a
    consumer has seen is already durable.
    """

    __slots__ = ("_fanout", "_session")

    def __init__(self, session: Session, fanout: _Fanout) -> None:
        self._session = session
        self._fanout = fanout

    async def emit(self, event: Event) -> EventRecord:
        record = self._session.append_event(event)
        if event.type == "turn.completed":
            # Cadence is evaluated only on completed turns. Snapshot failure is
            # never allowed to fail the turn: the log is authoritative and a
            # missing/old snapshot only costs a full replay on resume.
            self._session._snapshot_after_completed_turn()
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
    """A handle over one append-only session log."""

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
        #: Snapshot cadence seam. ``int`` counts completed turns; a callable is
        #: re-evaluated per completed turn so the integration packet can feed it
        #: ``config.v2.session.snapshot_every`` without this layer importing
        #: configuration. ``None`` disables automatic snapshots.
        self._snapshot_every = snapshot_every
        #: Most recent background snapshot failure, for diagnostics only.
        self.snapshot_error: Exception | None = None
        #: Records appended by the last :meth:`recover_dangling_tool_uses` call.
        self.recovered: tuple[MessageRecord, ...] = ()

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
        return self

    # -- approvals ---------------------------------------------------------

    @property
    def attended(self) -> bool:
        """Whether an interactive approver is attached for this session.

        Headless sessions stay ``False`` so the configured ``on_unattended``
        policy applies; an interactive adapter calls :meth:`mark_attended` (or
        passes ``attended=True`` to :meth:`send`). This decouples attendance from
        any particular terminal/UI.
        """
        return self._attended

    def mark_attended(self, attended: bool = True) -> Session:
        self._attended = bool(attended)
        return self

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
        record = self._store.append_message(self._id, message, seq=seq)
        self._read = None
        return record

    def append_event(self, event: Event, *, seq: int | None = None) -> EventRecord:
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

    # -- turn ownership and cancellation -----------------------------------

    @property
    def cancel_token(self) -> CancelToken:
        return self._cancel

    def cancel(self, reason: str | None = None) -> None:
        self._cancel.cancel(reason)

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
        token = CancelToken()
        state = TurnState.new(turn_id=turn_id or new_id(), session_id=self._id).start()
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
        if self._active is None or self._active.turn_id != turn_id:
            return
        self._active = None
        self._lock.release()

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
            attended=self._attended if attended is None else bool(attended),
        )
        if bundle is not None:
            freeze = getattr(assembler, "freeze_tools", None)
            if callable(freeze):
                freeze(getattr(bundle, "schemas", ()))
        return bundle

    async def send(
        self, user_input: str | list[ContentBlock], *, attended: bool | None = None
    ) -> AsyncIterator[Event]:
        """Run one turn and stream its persisted events in order.

        The sequence is deliberate:

        1. validate the input and claim the exclusive turn lease (so a second
           in-process or cross-process turn fails fast with
           :class:`SessionBusy`);
        2. take **one** per-turn snapshot of config/system and derive limits from
           it. If the snapshot fails, the lease is released without touching the
           log (no partial turn is persisted);
        3. recover dangling tool uses under the held lock, then append the new
           user message. Recovery and input are separate append-only records, so
           stored IR may contain consecutive user messages; the adapter coalesces
           adjacent same-role messages on the wire;
        4. run :func:`nexus.core.loop.run_turn` as a producer task that persists
           every event and fans it out to a bounded, lossless buffer;
        5. yield those events to the consumer.

        Closing the consumer early cancels the producer (even when it is blocked
        on backpressure or a provider wait); the loop emits/persists its terminal
        event, releases the lease, and the source stream is closed.
        ``session.cancel()`` cancels the lease token.

        Configuration is reloaded between turns but frozen within one, and never
        cached on the session, so concurrent sessions cannot race on shared state.
        """
        if self._assemble is None or self._provider_for is None:
            raise SessionError(
                "Session.send requires an assembler and provider resolver; "
                "open the session through Runtime.session() or call bind() first"
            )
        content = _coerce_user_input(user_input)

        # Claim the lease first, then snapshot config/system exactly once. A
        # snapshot failure releases the lease without mutating the log.
        lease = self.begin_turn()
        try:
            assembler = self._assemble_for_turn()
            lease.limits = self._limits_for_turn(assembler)
            tool_turn = self._tool_turn_for(assembler, lease, attended)
        except BaseException:
            lease.release()
            raise

        # The snapshot succeeded; from here the turn persists append-only.
        try:
            self._active_tools = tool_turn
            self.recover_dangling_tool_uses(lock=False)
            self.append_message(
                Message(
                    role="user",
                    content=content,
                    meta=MessageMeta(turn_id=lease.turn_id),
                )
            )
            fanout = _Fanout(self._event_buffer)
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
                )
            )
            task.add_done_callback(lambda _task: fanout.close())
            try:
                while True:
                    item = await fanout.get()
                    if item is _SENTINEL:
                        break
                    yield item
            finally:
                try:
                    if not task.done():
                        task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                finally:
                    # The loop releases the lease in its own ``finally``; this is
                    # an outermost safety net (e.g. a producer cancelled before
                    # it entered the loop, or a task that raised).
                    self._active_tools = None
                    lease.release()
        except BaseException:
            self._active_tools = None
            lease.release()
            raise


__all__ = ["Session", "TurnLease"]
