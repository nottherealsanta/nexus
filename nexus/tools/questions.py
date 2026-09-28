"""Session-scoped question requests for tools and agents.

The broker is deliberately UI- and permission-agnostic. It owns pending
questions until an answer, explicit cancellation, timeout, or waiter
cancellation. An optional event sink can persist lifecycle events; request
publication precedes visibility to resolvers, and answer publication precedes
completion of the waiting request.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..errors import OperationCancelled

DEFAULT_QUESTION_TIMEOUT_S = 120.0
MAX_QUESTION_TIMEOUT_S = 24 * 60 * 60.0
MAX_QUESTION_PROMPT_CHARS = 16_384
MAX_QUESTION_ANSWER_CHARS = 4_096
MAX_QUESTION_PATTERN_CHARS = 256
MAX_CHOICE_ID_CHARS = 64
_CHOICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")

QuestionEventSink = Callable[
    [str, dict[str, Any]], object | Awaitable[object]
]
QuestionIdFactory = Callable[[], str | uuid.UUID]


class QuestionBrokerError(ValueError):
    """A question request or broker configuration is invalid."""


class QuestionAnswerError(QuestionBrokerError):
    """An answer does not satisfy the question's declared response shape."""


class QuestionTimeout(TimeoutError):
    """A question remained unanswered through its configured deadline."""


@dataclass(frozen=True, slots=True)
class QuestionChoice:
    """One stable, UI-presentable choice."""

    id: str
    label: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or _CHOICE_ID.fullmatch(self.id) is None:
            raise QuestionBrokerError(
                "choice id must be 1-64 stable ASCII letters, digits, or . _ : -"
            )
        if not isinstance(self.label, str) or not self.label.strip():
            raise QuestionBrokerError("choice label must be non-empty")
        if len(self.label) > MAX_QUESTION_PROMPT_CHARS or "\x00" in self.label:
            raise QuestionBrokerError("choice label is invalid or too long")

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label}


@dataclass(frozen=True, slots=True)
class QuestionRequest:
    """Immutable, serializable data describing one outstanding question.

    A non-empty ``choices`` tuple requires an answer to be one of its stable
    choice IDs. With no choices, the answer is free text constrained by
    ``max_length`` and (optionally) a full-match regular expression.
    """

    question_id: str
    root_session_id: str
    source_session_id: str
    turn_id: str
    agent_id: str
    call_id: str
    prompt: str
    choices: tuple[QuestionChoice, ...]
    created_at: datetime
    deadline: datetime
    max_length: int = MAX_QUESTION_ANSWER_CHARS
    pattern: str | None = None

    def __post_init__(self) -> None:
        try:
            parsed_id = uuid.UUID(self.question_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise QuestionBrokerError("question_id must be a UUID") from exc
        if str(parsed_id) != self.question_id:
            raise QuestionBrokerError("question_id must use canonical UUID spelling")
        for name in (
            "root_session_id",
            "source_session_id",
            "turn_id",
            "agent_id",
            "call_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or "\x00" in value:
                raise QuestionBrokerError(f"{name} must be a non-empty string")
        if (
            not isinstance(self.prompt, str)
            or not self.prompt.strip()
            or len(self.prompt) > MAX_QUESTION_PROMPT_CHARS
            or "\x00" in self.prompt
        ):
            raise QuestionBrokerError("prompt must be non-empty and bounded")
        if not isinstance(self.choices, tuple) or not all(
            isinstance(choice, QuestionChoice) for choice in self.choices
        ):
            raise QuestionBrokerError("choices must be a tuple of QuestionChoice")
        if len(self.choices) > 3:
            raise QuestionBrokerError("a question may have at most three choices")
        choice_ids = [choice.id for choice in self.choices]
        if len(choice_ids) != len(set(choice_ids)):
            raise QuestionBrokerError("choice IDs must be unique")
        if (
            isinstance(self.max_length, bool)
            or not isinstance(self.max_length, int)
            or not 1 <= self.max_length <= MAX_QUESTION_ANSWER_CHARS
        ):
            raise QuestionBrokerError(
                f"max_length must be between 1 and {MAX_QUESTION_ANSWER_CHARS}"
            )
        if self.pattern is not None:
            if (
                not isinstance(self.pattern, str)
                or len(self.pattern) > MAX_QUESTION_PATTERN_CHARS
            ):
                raise QuestionBrokerError("pattern must be a bounded string")
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise QuestionBrokerError(f"invalid answer pattern: {exc}") from exc
        if not isinstance(self.created_at, datetime) or not isinstance(
            self.deadline, datetime
        ):
            raise QuestionBrokerError("created_at and deadline must be datetimes")
        if (
            self.created_at.tzinfo is None
            or self.deadline.tzinfo is None
            or self.deadline <= self.created_at
        ):
            raise QuestionBrokerError("question timestamps must be aware and ordered")

    @property
    def id(self) -> str:
        """Short compatibility spelling for event/UI consumers."""
        return self.question_id

    @property
    def accepts_free_text(self) -> bool:
        return not self.choices

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "root_session_id": self.root_session_id,
            "source_session_id": self.source_session_id,
            "turn_id": self.turn_id,
            "agent_id": self.agent_id,
            "call_id": self.call_id,
            "prompt": self.prompt,
            "choices": [choice.to_dict() for choice in self.choices],
            "max_length": self.max_length if not self.choices else None,
            "pattern": self.pattern if not self.choices else None,
            "created_at": self.created_at.isoformat(),
            "deadline": self.deadline.isoformat(),
        }


@dataclass(slots=True)
class _PendingQuestion:
    request: QuestionRequest
    future: asyncio.Future[str]
    emit: QuestionEventSink | None = None


class QuestionBroker:
    """Broker immutable questions across root-owned sessions.

    ``timeout_s`` is a hard upper bound on any individual wait, including one
    whose caller supplies a longer timeout. The optional ``emit`` callback is
    invoked as ``emit(event_type, payload)`` and may be synchronous or async.
    """

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_QUESTION_TIMEOUT_S,
        emit: QuestionEventSink | None = None,
        id_factory: QuestionIdFactory = uuid.uuid4,
    ) -> None:
        self._timeout_s = self._validate_timeout(timeout_s, "timeout_s")
        if emit is not None and not callable(emit):
            raise QuestionBrokerError("emit must be callable or None")
        if not callable(id_factory):
            raise QuestionBrokerError("id_factory must be callable")
        self._emit = emit
        self._id_factory = id_factory
        self._pending_by_id: dict[str, _PendingQuestion] = {}
        self._pending_by_root: dict[str, dict[str, _PendingQuestion]] = {}
        self._lock = asyncio.Lock()

    @property
    def pending(self) -> tuple[QuestionRequest, ...]:
        """A stable snapshot of currently published, unanswered requests."""
        return tuple(item.request for item in self._pending_by_id.values())

    def pending_for(self, root_session_id: str) -> tuple[QuestionRequest, ...]:
        """Return pending requests owned by exactly ``root_session_id``."""
        return tuple(
            item.request
            for item in self._pending_by_root.get(root_session_id, {}).values()
        )

    async def request(
        self,
        *,
        root_session_id: str,
        source_session_id: str,
        turn_id: str,
        agent_id: str,
        call_id: str,
        prompt: str,
        choices: tuple[QuestionChoice, ...] = (),
        max_length: int = MAX_QUESTION_ANSWER_CHARS,
        pattern: str | None = None,
        timeout_s: float | None = None,
        cancel: object | None = None,
        emit: QuestionEventSink | None = None,
    ) -> str:
        """Publish a question and wait for a validated answer.

        ``cancel`` may be any object with ``wait()`` (including Nexus's
        ``CancelToken``). Caller-task cancellation also removes the waiter.
        ``emit`` overrides the broker sink for this question's lifecycle
        events, so each asker persists to its own turn's log.
        """
        requested_timeout = (
            self._timeout_s
            if timeout_s is None
            else min(self._validate_timeout(timeout_s, "timeout_s"), self._timeout_s)
        )
        now = datetime.now(UTC)
        loop = asyncio.get_running_loop()
        async with self._lock:
            request = self._make_request(
                root_session_id=root_session_id,
                source_session_id=source_session_id,
                turn_id=turn_id,
                agent_id=agent_id,
                call_id=call_id,
                prompt=prompt,
                choices=choices,
                max_length=max_length,
                pattern=pattern,
                created_at=now,
                deadline=now + timedelta(seconds=requested_timeout),
            )
            # Persistence must succeed before the ID can be observed or resolved.
            sink = emit if emit is not None else self._emit
            await self._emit_event("question.requested", request.to_dict(), sink)
            pending = _PendingQuestion(request, loop.create_future(), sink)
            self._pending_by_id[request.question_id] = pending
            self._pending_by_root.setdefault(root_session_id, {})[
                request.question_id
            ] = pending

        cancel_task: asyncio.Task[object] | None = None
        try:
            waiters: set[asyncio.Future[object] | asyncio.Future[str]] = {
                pending.future
            }
            if cancel is not None:
                wait_method = getattr(cancel, "wait", None)
                if not callable(wait_method):
                    raise QuestionBrokerError("cancel must expose an awaitable wait()")
                cancel_task = asyncio.create_task(wait_method())
                waiters.add(cancel_task)
            done, _ = await asyncio.wait(
                waiters,
                timeout=requested_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if pending.future in done:
                return pending.future.result()
            if cancel_task is not None and cancel_task in done:
                # A simultaneous answer wins over cooperative cancellation.
                if pending.future.done():
                    return pending.future.result()
                await self.cancel(
                    root_session_id,
                    request.question_id,
                    reason=getattr(cancel, "reason", None) or "cancelled",
                )
                if pending.future.done():
                    return pending.future.result()
                raise OperationCancelled(
                    getattr(cancel, "reason", None) or "cancelled"
                )
            async with self._lock:
                # Decide expiration under the same lock as resolve(), so a
                # just-arrived answer cannot be persisted after this waiter
                # has already committed to timing out.
                if pending.future.done():
                    return pending.future.result()
                self._remove_pending(request.question_id, pending)
                pending.future.cancel()
            raise QuestionTimeout(
                f"question {request.question_id} timed out after "
                f"{requested_timeout:g} seconds"
            )
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
            async with self._lock:
                self._remove_pending(request.question_id, pending)
                if not pending.future.done():
                    pending.future.cancel()

    async def resolve(
        self, root_session_id: str, question_id: str, answer: str
    ) -> bool:
        """Resolve a live question owned by this exact root session.

        Returns ``False`` for a stale ID or wrong root. Invalid answers raise
        :class:`QuestionAnswerError`. The first successfully persisted answer
        wins; later attempts return ``False``.
        """
        async with self._lock:
            pending = self._pending_by_id.get(question_id)
            if pending is None or pending.request.root_session_id != root_session_id:
                return False
            self._validate_answer(pending.request, answer)
            labels = {choice.id: choice.label for choice in pending.request.choices}
            await self._emit_event(
                "question.resolved",
                {
                    "question_id": question_id,
                    "root_session_id": root_session_id,
                    "answer": answer,
                    "answer_label": labels.get(answer, answer),
                },
                pending.emit,
            )
            self._remove_pending(question_id, pending)
            if not pending.future.done():
                pending.future.set_result(answer)
            return True

    async def cancel(
        self,
        root_session_id: str,
        question_id: str,
        *,
        reason: str = "cancelled",
    ) -> bool:
        """Cancel one request only when it belongs to the supplied root."""
        async with self._lock:
            pending = self._pending_by_id.get(question_id)
            if pending is None or pending.request.root_session_id != root_session_id:
                return False
            self._remove_pending(question_id, pending)
            if not pending.future.done():
                pending.future.set_exception(OperationCancelled(reason))
            return True

    async def cancel_session(
        self, root_session_id: str, *, reason: str = "session cancelled"
    ) -> int:
        """Cancel and wake every outstanding request owned by one root."""
        async with self._lock:
            pending_for_root = tuple(
                self._pending_by_root.get(root_session_id, {}).items()
            )
            for question_id, pending in pending_for_root:
                self._remove_pending(question_id, pending)
                if not pending.future.done():
                    pending.future.set_exception(OperationCancelled(reason))
            return len(pending_for_root)

    async def _emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        sink: QuestionEventSink | None = None,
    ) -> None:
        sink = sink if sink is not None else self._emit
        if sink is None:
            return
        outcome = sink(event_type, data)
        if inspect.isawaitable(outcome):
            await outcome

    def _make_request(
        self,
        *,
        root_session_id: str,
        source_session_id: str,
        turn_id: str,
        agent_id: str,
        call_id: str,
        prompt: str,
        choices: tuple[QuestionChoice, ...],
        max_length: int,
        pattern: str | None,
        created_at: datetime,
        deadline: datetime,
    ) -> QuestionRequest:
        for _ in range(32):
            candidate = self._id_factory()
            try:
                question_id = str(uuid.UUID(str(candidate)))
            except (AttributeError, TypeError, ValueError) as exc:
                raise QuestionBrokerError("id_factory must return UUIDs") from exc
            if question_id not in self._pending_by_id:
                break
        else:
            raise QuestionBrokerError("id_factory repeatedly returned colliding IDs")
        return QuestionRequest(
            question_id=question_id,
            root_session_id=root_session_id,
            source_session_id=source_session_id,
            turn_id=turn_id,
            agent_id=agent_id,
            call_id=call_id,
            prompt=prompt,
            choices=choices,
            created_at=created_at,
            deadline=deadline,
            max_length=max_length,
            pattern=pattern,
        )

    @staticmethod
    def _validate_answer(request: QuestionRequest, answer: str) -> None:
        if not isinstance(answer, str):
            raise QuestionAnswerError("answer must be a string")
        if request.choices:
            if answer not in {choice.id for choice in request.choices}:
                raise QuestionAnswerError("answer must be one of the stable choice IDs")
            return
        if len(answer) > request.max_length:
            raise QuestionAnswerError(
                f"free-text answer exceeds {request.max_length} characters"
            )
        if "\x00" in answer:
            raise QuestionAnswerError("free-text answer must not contain NUL")
        if request.pattern is not None and re.fullmatch(request.pattern, answer) is None:
            raise QuestionAnswerError("free-text answer does not match the constraint")

    @staticmethod
    def _validate_timeout(value: float, name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            or value > MAX_QUESTION_TIMEOUT_S
        ):
            raise QuestionBrokerError(
                f"{name} must be greater than zero and at most "
                f"{MAX_QUESTION_TIMEOUT_S:g} seconds"
            )
        return float(value)

    def _remove_pending(
        self, question_id: str, pending: _PendingQuestion
    ) -> None:
        if self._pending_by_id.get(question_id) is not pending:
            return
        del self._pending_by_id[question_id]
        root_pending = self._pending_by_root.get(pending.request.root_session_id)
        if root_pending is not None:
            root_pending.pop(question_id, None)
            if not root_pending:
                del self._pending_by_root[pending.request.root_session_id]


__all__ = [
    "DEFAULT_QUESTION_TIMEOUT_S",
    "MAX_QUESTION_ANSWER_CHARS",
    "MAX_QUESTION_PROMPT_CHARS",
    "MAX_QUESTION_TIMEOUT_S",
    "QuestionAnswerError",
    "QuestionBroker",
    "QuestionBrokerError",
    "QuestionChoice",
    "QuestionRequest",
    "QuestionTimeout",
]
