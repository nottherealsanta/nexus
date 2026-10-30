"""The agent loop (PLAN.md step 2): model → tool calls → results → model, until
the model stops calling tools.

The loop owns no storage. It starts from `messages` (earlier history) and
yields `MessageAdded` each time it appends a message, so the caller can save
it. `turn()` also yields every provider event unchanged plus the loop's own
events, so a client sees the wire traffic, the deltas, and each tool run in
order.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .hooks import HookOutcome, HookRunner
from .llmhub import Provider
from .llmhub.auth import AuthError, CredentialError
from .llmhub.types import (
    Done,
    Event,
    Message,
    Part,
    PartDone,
    ProviderError,
    Request,
    Text,
    ToolCall,
    ToolResult,
    WireRequest,
    WireResponse,
)
from .retry import RetryPolicy
from .tools import DEFAULT_TOOLS, Tool, ToolContext, ToolError

if TYPE_CHECKING:
    from .observations import FileGuard

MAX_STEPS = 50


@dataclass(frozen=True, slots=True)
class StepStart:
    step: int


@dataclass(frozen=True, slots=True)
class ToolStart:
    call: ToolCall
    args: dict[str, Any] | None  # None when the arguments were not valid JSON


@dataclass(frozen=True, slots=True)
class ToolEnd:
    call: ToolCall
    result: ToolResult


@dataclass(frozen=True, slots=True)
class MessageAdded:
    message: Message
    input_id: str | None = None


@dataclass(frozen=True, slots=True)
class TurnEnd:
    stop: str
    steps: int


@dataclass(frozen=True, slots=True)
class NoUserMessage:
    """Explicit marker: this turn starts from the stored context and adds no user message.

    A manual continuation passes `NO_USER_MESSAGE` instead of text. It is not an
    empty string, so a blank prompt can never be mistaken for a continuation, and
    the loop has one path for both cases.
    """


NO_USER_MESSAGE = NoUserMessage()


@dataclass(frozen=True, slots=True)
class RetryScheduled:
    """A retryable pre-stream failure; the next attempt starts after `delay` seconds."""

    attempt: int          # the failed attempt
    next_attempt: int
    attempt_id: str       # identifies the next attempt for durable recording
    delay: float
    reason: str
    status: int | None = None
    retry_after: float | None = None
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class RetryStarted:
    """The next provider attempt is starting now."""

    attempt: int
    attempt_id: str


@dataclass(frozen=True, slots=True)
class RetryExhausted:
    """No further attempt fits the policy or budget."""

    attempts: int
    attempt_id: str
    reason: str
    status: int | None = None
    retry_after: float | None = None
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class ModelFallback:
    previous: str
    model: str
    reason: str


@dataclass(frozen=True, slots=True)
class HookFired:
    """A command hook ran. An execution detail; only what it adds to context becomes a message."""

    hook: str
    event: str
    decision: str
    message: str = ""
    error: str = ""
    seconds: float = 0.0
    tool: str | None = None
    call_id: str | None = None


LoopEvent = (Event | StepStart | ToolStart | ToolEnd | MessageAdded | TurnEnd | RetryScheduled | RetryStarted
             | RetryExhausted | ModelFallback | HookFired)
TurnInput = str | Message | NoUserMessage
Approve = Callable[[ToolCall, dict[str, Any]], Awaitable[bool]]
Inbox = Callable[[], list[str]]  # notices to add (as a user message) before the next model call


def default_instructions(workspace: Path) -> str:
    return (
        "You are a coding agent working in a local workspace.\n"
        f"Workspace: {workspace}\n"
        f"Today: {datetime.now().astimezone().date().isoformat()}\n"
        "Tools: read (files, directories), bash (commands run in the workspace), "
        "edit (exact replacement or apply_patch), write (whole files), ask (questions to the user). "
        "Read before you edit. Keep answers short and say what you changed."
    )


class Agent:
    def __init__(
        self,
        provider: Provider,
        *,
        model: str,
        workspace: Path,
        tools: tuple[Tool, ...] = DEFAULT_TOOLS,
        instructions: str | None = None,
        reasoning_effort: str | None = None,
        approve: Approve | None = None,
        max_steps: int = MAX_STEPS,
        messages: list[Message] | None = None,
        conversation_id: str | None = None,
        inbox: Inbox | None = None,
        steering: Callable[[], list[tuple[str, Message]]] | None = None,
        refresh: Callable[[Any], None] | None = None,
        files: FileGuard | None = None,
        cwd_changed: bool = False,
        retry_policy: RetryPolicy | None = None,
        retry_clock: Callable[[], float] = time.monotonic,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_random: Callable[[], float] | None = None,
        hooks: HookRunner | None = None,
        turn_id: str | None = None,
        render: Callable[[Message], Message] | None = None,
        fallback_models: list[tuple[str, Provider, str]] | None = None,
        model_ref: str = "",
    ) -> None:
        self.hooks = hooks
        self.render: Callable[[Message], Message] | None = render  # fills tool images' bytes for the provider
        self.turn_id = turn_id
        self._hook_notes: list[str] = []
        self.provider = provider
        self.model = model
        self.model_ref = model_ref
        self.fallback_models = list(fallback_models or [])
        self._stream_received = False
        self.workspace = workspace
        self.tools = {tool.spec.name: tool for tool in tools}
        self.instructions = instructions or default_instructions(workspace)
        self.reasoning_effort = reasoning_effort
        self.approve = approve
        self.max_steps = max_steps
        self.conversation_id = conversation_id or uuid.uuid4().hex
        self.messages: list[Message] = list(messages or [])
        self.inbox = inbox
        self.steering = steering
        self.refresh = refresh
        self.files = files
        self.cwd_changed = cwd_changed
        self.retry_policy = retry_policy or RetryPolicy()
        self._retry_clock = retry_clock
        self._retry_sleep = retry_sleep
        self._retry_random = retry_random

    def request(self) -> Request:
        return Request(
            model=self.model,
            instructions=self.instructions,
            messages=tuple(self.render(m) for m in self.messages) if self.render else tuple(self.messages),
            tools=tuple(tool.spec for tool in self.tools.values()),
            reasoning_effort=self.reasoning_effort,
            conversation_id=self.conversation_id,
        )

    def _add(self, message: Message) -> MessageAdded:
        self.messages.append(message)
        return MessageAdded(message)

    async def _fire(self, event: str, *, tool: str | None = None, args: dict[str, Any] | None = None,
                    call_id: str | None = None, **extra: Any) -> tuple[list[HookOutcome], list[HookFired]]:
        if self.hooks is None or not self.hooks.has(event):
            return [], []
        if call_id is not None:
            extra["call_id"] = call_id
        outcomes = await self.hooks.fire(event, tool=tool, args=args, turn_id=self.turn_id, **extra)
        fired = [HookFired(o.hook, o.event, o.decision, o.message, o.error, o.seconds, tool, call_id) for o in outcomes]
        for o in outcomes:
            if o.decision != "block":  # a block is reported where it acts (tool result, ended turn)
                for text in (o.message if o.decision == "warn" else "", o.context):
                    if text:
                        self._hook_notes.append(f"[hook {o.hook}] {text}")
        return outcomes, fired

    def _flush_notes(self) -> list[str]:
        notes, self._hook_notes = self._hook_notes, []
        return notes

    async def turn(self, user_text: TurnInput) -> AsyncIterator[LoopEvent]:
        if not self.messages:
            _, fired = await self._fire("SessionStart")
            for event in fired:
                yield event
        if not isinstance(user_text, NoUserMessage):
            message = user_text if isinstance(user_text, Message) else Message("user", (Text(user_text),))
            yield self._add(message)
            prompt = "\n".join(p.text for p in message.parts if isinstance(p, Text))
            outcomes, fired = await self._fire("UserPromptSubmit", prompt=prompt)
            for event in fired:
                yield event
            blocked = next((o for o in outcomes if o.decision == "block"), None)
            if blocked is not None:
                # The prompt stays in the context; the model is not called for it.
                yield self._add(Message("user", (Text(f"[hook {blocked.hook}] blocked this prompt: {blocked.message}"),)))
                yield TurnEnd("blocked", 0)
                return
        for step in range(1, self.max_steps + 1):
            if self.refresh is not None:
                self.refresh(self)
            notices = [*(self.inbox() if self.inbox is not None else []), *self._flush_notes()]
            if notices:
                yield self._add(Message("user", tuple(Text(n) for n in notices)))
            for input_id, message in self.steering() if self.steering else []:
                self.messages.append(message)
                yield MessageAdded(message, input_id)
            yield StepStart(step)
            parts: list[Part] = []
            stop = "other"
            while True:
                try:
                    async for event in self._stream_with_retry():
                        if isinstance(event, PartDone):
                            parts.append(event.part)
                        elif isinstance(event, Done):
                            stop = event.stop
                        yield event
                    break
                except (ProviderError, AuthError, CredentialError) as error:
                    if self._stream_received or not self.fallback_models:
                        raise
                    ref, provider, model = self.fallback_models.pop(0)
                    previous = self.model_ref
                    self.provider, self.model, self.model_ref = provider, model, ref
                    yield ModelFallback(previous, ref, str(error))
                    yield self._add(Message("user", (Text(
                        f"[Nexus notice: model fallback] {previous} failed before a response; "
                        f"continuing with {ref}."),)))
            yield self._add(Message("assistant", tuple(parts)))
            calls = [p for p in parts if isinstance(p, ToolCall)]
            if not calls:
                if self.steering and self.steering():
                    continue
                _, fired = await self._fire("TurnEnd", stop=stop, steps=step)
                for event in fired:
                    yield event
                if self._hook_notes:
                    yield self._add(Message("user", tuple(Text(n) for n in self._flush_notes())))
                yield TurnEnd(stop, step)
                return
            results: list[ToolResult] = []
            for call in calls:
                if self.refresh is not None:
                    self.refresh(self)
                args = _parse_args(call.arguments)
                yield ToolStart(call, args)
                result = None
                if args is not None and call.name in self.tools:
                    outcomes, fired = await self._fire("PreToolUse", tool=call.name, args=args, call_id=call.id)
                    for event in fired:
                        yield event
                    for o in outcomes:
                        if o.decision == "block":
                            result = ToolResult(call.id, f"blocked by hook {o.hook}: {o.message or 'no reason given'}", is_error=True)
                        elif o.decision == "modify" and o.tool_input is not None:
                            args = o.tool_input
                            self._hook_notes.append(
                                f"[hook {o.hook}] changed the input of the {call.name} call {call.id} to "
                                f"{json.dumps(args, ensure_ascii=False)}")
                if result is None:
                    result = await self._run(call, args)
                if args is not None and call.name in self.tools:
                    _, fired = await self._fire("PostToolUse", tool=call.name, args=args, call_id=call.id,
                                                tool_result=result.output, is_error=result.is_error)
                    for event in fired:
                        yield event
                results.append(result)
                yield ToolEnd(call, result)
            yield self._add(Message("tool", tuple(results)))
        yield TurnEnd("max_steps", self.max_steps)

    async def _stream_with_retry(self) -> AsyncIterator[Event | RetryScheduled | RetryStarted | RetryExhausted]:
        """One provider request, retried only before any response stream event.

        Wire traffic is emitted for every attempt. A previous attempt's request is
        visible in the event log even when it fails; the next attempt reuses the
        exact request built for this step (`self.request()` is unchanged until the
        assistant message is appended).
        """
        policy = self.retry_policy
        self._stream_received = False
        request = self.request()  # freeze model, tools and context across backoff
        started = self._retry_clock()
        failed = 0
        while True:
            failed += 1
            received = False
            try:
                async for event in self.provider.stream(request):
                    if not isinstance(event, (WireRequest, WireResponse)):
                        received = True
                        self._stream_received = True
                    yield event
                return
            except ProviderError as error:
                remaining = policy.max_elapsed - (self._retry_clock() - started)
                can_retry = not received and policy.retryable(error)
                delay = policy.delay(
                    failed, retry_after=error.retry_after, remaining=remaining, rng=self._retry_random) \
                    if can_retry else None
                if delay is None:
                    if not received and policy.max_attempts > 1 and policy.retryable(error):
                        yield RetryExhausted(failed, f"att_{uuid.uuid4().hex[:12]}", str(error),
                                             error.status, error.retry_after, policy.max_attempts)
                    raise
                next_attempt = failed + 1
                attempt_id = f"att_{uuid.uuid4().hex[:12]}"
                yield RetryScheduled(failed, next_attempt, attempt_id, delay, str(error),
                                     error.status, error.retry_after, policy.max_attempts)
                await self._retry_sleep(delay)
                if self._retry_clock() - started > policy.max_elapsed:
                    yield RetryExhausted(failed, attempt_id, str(error),
                                         error.status, error.retry_after, policy.max_attempts)
                    raise
                yield RetryStarted(next_attempt, attempt_id)

    async def _run(self, call: ToolCall, args: dict[str, Any] | None) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, f"unknown tool {call.name!r}", is_error=True)
        if args is None:
            return ToolResult(call.id, "arguments were not a JSON object", is_error=True)
        if self.approve is not None:
            try:
                if not await self.approve(call, args):
                    return ToolResult(call.id, "the user declined this tool call", is_error=True)
            except ToolError as exc:  # refused by a permission rule
                return ToolResult(call.id, str(exc), is_error=True)
        try:
            output = await tool.run(args, ToolContext(
                self.workspace, call.id, files=self.files, cwd_changed=self.cwd_changed))
            return ToolResult(call.id, str(output), media=getattr(output, "media", ()))
        except ToolError as exc:
            return ToolResult(call.id, str(exc), is_error=True)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
            return ToolResult(call.id, f"{type(exc).__name__}: {exc}", is_error=True)


def _parse_args(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
