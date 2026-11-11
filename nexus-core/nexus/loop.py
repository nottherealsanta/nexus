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
from typing import Any

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

MAX_STEPS = 500

STEP_BUDGET_NOTICE = (
    "[Nexus notice: step budget] About 80% of this turn's step budget has been used. "
    "Finish the current task and verify the result with the remaining steps. "
    "If work remains, explain what is done and what still needs attention.")

STEP_LIMIT_NOTICE = (
    "[Nexus notice: step limit] The maximum number of steps allowed for this turn has been reached. "
    "Tools are disabled until the next user message. Respond with text only; do not call any tool. "
    "Say that the step limit was reached, summarize what has been done so far, list any remaining "
    "tasks, and recommend what to do next.")
STEP_LIMIT_REFUSAL = "tools are disabled: the step limit for this turn was reached; reply with text only"


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
class Notice:
    """A notice added to the context with structured Core metadata.

    `text` is exactly what the model receives; `metadata` is client-facing and
    never sent to the provider. Older notices are plain strings.
    """

    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MessageAdded:
    message: Message
    input_ids: tuple[str, ...] = ()
    notices: tuple[dict[str, Any], ...] = ()


def merge_steering(messages: list[Message]) -> Message:
    """One user message for steers delivered together, their text separated by newlines."""
    if len(messages) == 1:
        return messages[0]
    parts: list[Part] = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, Text) and parts and isinstance(parts[-1], Text):
                previous = parts[-1]
                parts[-1] = Text(previous.text + "\n" + part.text)
            else:
                parts.append(part)
    return Message("user", tuple(parts))


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
Inbox = Callable[[], list[str | Notice]]  # notices to add (as a user message) before the next model call


def default_instructions(workspace: Path) -> str:
    return (
        "You are a coding agent working in a local workspace.\n"
        f"Workspace: {workspace}\n"
        f"Today: {datetime.now().astimezone().date().isoformat()}\n"
        "Tools: read (files, directories), bash (commands run in the workspace), "
        "edit (text replacement), patch (apply_patch), write (whole files), ask (questions to the user). "
        "Keep answers short and say what you changed."
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
        model_efforts: dict[str, str | None] | None = None,
        approve: Approve | None = None,
        max_steps: int = MAX_STEPS,
        messages: list[Message] | None = None,
        conversation_id: str | None = None,
        inbox: Inbox | None = None,
        steering: Callable[[], list[tuple[str, Message]]] | None = None,
        refresh: Callable[[Any], None] | None = None,
        file_lock: asyncio.Lock | None = None,
        cwd_changed: bool = False,
        save_media: Callable[[bytes, str, str], str] | None = None,
        retry_policy: RetryPolicy | None = None,
        retry_clock: Callable[[], float] = time.monotonic,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_random: Callable[[], float] | None = None,
        hooks: HookRunner | None = None,
        turn_id: str | None = None,
        render: Callable[[Message], Message] | None = None,
        fallback_models: list[tuple[str, Provider, str]] | None = None,
        model_ref: str = "",
        max_tool_calls: int | None = None,
        max_tool_seconds: float | None = None,
        publish_hook: Callable[[HookFired], None] | None = None,
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
        self.model_efforts = model_efforts or {}  # a fallback model runs with its own effort
        self.approve = approve
        self.max_steps = max_steps
        self.conversation_id = conversation_id or uuid.uuid4().hex
        self.messages: list[Message] = list(messages or [])
        self.inbox = inbox
        self.steering = steering
        self.refresh = refresh
        self.file_lock = file_lock
        self.cwd_changed = cwd_changed
        self.save_media = save_media
        self.retry_policy = retry_policy or RetryPolicy()
        self._retry_clock = retry_clock
        self._retry_sleep = retry_sleep
        self._retry_random = retry_random
        self.max_tool_calls = max_tool_calls
        self.max_tool_seconds = max_tool_seconds
        self.publish_hook = publish_hook
        self._tool_calls = 0
        self._tool_deadline: float | None = None
        self._budget_notified = False
        self._nested_events: list[HookFired] = []

    def request(self) -> Request:
        return Request(
            model=self.model,
            instructions=self.instructions,
            messages=tuple(self.render(m) for m in self.messages) if self.render else tuple(self.messages),
            tools=() if self._budget_error() else tuple(tool.spec for tool in self.tools.values()),
            reasoning_effort=self.reasoning_effort,
            conversation_id=self.conversation_id,
        )

    def _add(self, message: Message, notices: tuple[dict[str, Any], ...] = ()) -> MessageAdded:
        self.messages.append(message)
        return MessageAdded(message, notices=notices)

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

    async def turn(self, user_text: TurnInput, *,
                   notices: tuple[dict[str, Any], ...] = ()) -> AsyncIterator[LoopEvent]:
        self._tool_calls = 0
        self._tool_deadline = time.monotonic() + self.max_tool_seconds if self.max_tool_seconds is not None else None
        self._budget_notified = False
        if not self.messages:
            _, fired = await self._fire("SessionStart")
            for event in fired:
                yield event
        if not isinstance(user_text, NoUserMessage):
            message = user_text if isinstance(user_text, Message) else Message("user", (Text(user_text),))
            yield self._add(message, notices)
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
            final = step == self.max_steps and self.max_steps > 1
            if self.refresh is not None:
                self.refresh(self)
            notices = [*(self.inbox() if self.inbox is not None else []), *self._flush_notes()]
            if self._budget_error() and not self._budget_notified:
                notices.append(self._budget_notice())
            if final:
                notices.append(STEP_LIMIT_NOTICE)
            elif step == max(2, (self.max_steps * 4 + 4) // 5):
                notices.append(STEP_BUDGET_NOTICE)
            if notices:
                meta = tuple(n.metadata for n in notices if isinstance(n, Notice))
                yield self._add(
                    Message("user", tuple(Text(n.text if isinstance(n, Notice) else n) for n in notices)),
                    meta,
                )
            steering = self.steering() if self.steering else []
            if steering:
                message = merge_steering([message for _, message in steering])
                self.messages.append(message)
                yield MessageAdded(message, tuple(input_id for input_id, _ in steering))
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
                    if ref in self.model_efforts:
                        self.reasoning_effort = self.model_efforts[ref]
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
                yield TurnEnd("max_steps" if final else stop, step)
                return
            results: list[ToolResult] = []
            for call in calls:
                if self.refresh is not None:
                    self.refresh(self)
                args = _parse_args(call.arguments)
                yield ToolStart(call, args)
                if final:
                    result = ToolResult(call.id, STEP_LIMIT_REFUSAL, is_error=True)
                    results.append(result)
                    yield ToolEnd(call, result)
                    continue
                async for event in self.execute_tool(call, args):
                    if isinstance(event, ToolResult):
                        result = event
                    else:
                        yield event
                for event in self._nested_events:
                    yield event
                self._nested_events.clear()
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

    def _budget_error(self) -> str | None:
        if self.max_tool_calls is not None and self._tool_calls >= self.max_tool_calls:
            return f"the {self.max_tool_calls}-call tool budget is exhausted"
        if self._tool_deadline is not None and time.monotonic() >= self._tool_deadline:
            return "the tool time budget is exhausted"
        return None

    def _budget_notice(self) -> str:
        self._budget_notified = True
        return (f"[Nexus notice: tool budget] {self._budget_error()}. Tools are disabled for this turn. "
                "Reply with an honest partial report: what you checked, what you found, and what remains unverified.")

    async def execute_tool(self, call: ToolCall, args: dict[str, Any] | None,
                           ctx: ToolContext | None = None) -> AsyncIterator[HookFired | ToolResult]:
        error = self._budget_error()
        if error:
            yield ToolResult(call.id, f"refused: {error}; answer now without tools", is_error=True)
            return
        self._tool_calls += 1
        async for event in self._execute_tool(call, args, ctx):
            yield event

    def _remaining_tool_seconds(self) -> float | None:
        return max(0, self._tool_deadline - time.monotonic()) if self._tool_deadline is not None else None

    async def _execute_tool(self, call: ToolCall, args: dict[str, Any] | None,
                            ctx: ToolContext | None) -> AsyncIterator[HookFired | ToolResult]:
        result = None
        if args is not None and call.name in self.tools:
            try:
                async with asyncio.timeout(self._remaining_tool_seconds()):
                    outcomes, fired = await self._fire("PreToolUse", tool=call.name, args=args, call_id=call.id)
            except TimeoutError:
                yield ToolResult(call.id, "the tool time budget expired before execution; this tool did not run", is_error=True)
                return
            for event in fired:
                yield event
            for outcome in outcomes:
                if outcome.decision == "block":
                    result = ToolResult(call.id, f"blocked by hook {outcome.hook}: {outcome.message or 'no reason given'}", is_error=True)
                elif outcome.decision == "modify" and outcome.tool_input is not None:
                    args = outcome.tool_input
                    self._hook_notes.append(
                        f"[hook {outcome.hook}] changed the input of the {call.name} call {call.id} to "
                        f"{json.dumps(args, ensure_ascii=False)}")
        if result is None:
            try:
                async with asyncio.timeout(self._remaining_tool_seconds()):
                    result = await self._run(call, args, ctx)
            except TimeoutError:
                result = ToolResult(call.id, "the tool time budget expired; execution was cancelled but effects may already exist; "
                                    "do not blindly retry; report partial findings", is_error=True)
        yield result
        if args is not None and call.name in self.tools:
            try:
                async with asyncio.timeout(self._remaining_tool_seconds()):
                    _, fired = await self._fire("PostToolUse", tool=call.name, args=args, call_id=call.id,
                                                tool_result=result.output, is_error=result.is_error)
                for event in fired:
                    yield event
            except TimeoutError:
                self._hook_notes.append(f"[Nexus notice: post-tool hook] The time budget expired after `{call.name}` "
                                        f"({call.id}); its completed result is preserved. Post-processing is incomplete.")

    async def run_nested_tool(self, call: ToolCall, args: dict[str, Any], ctx: ToolContext) -> str:
        result = None
        try:
            async for event in self.execute_tool(call, args, ctx):
                if isinstance(event, HookFired):
                    if self.publish_hook is not None:
                        self.publish_hook(event)
                    else:
                        self._nested_events.append(event)
                else:
                    result = event
        except asyncio.CancelledError:
            if result is None:
                raise
            # A cancelled post-hook cannot turn a completed external effect into an unknown result.
            self._hook_notes.append(f"[Nexus notice: post-tool hook] Post-processing of {call.id} was interrupted; "
                                    "its completed nested result is preserved.")
        if result is None:
            raise ToolError("nested tool returned no result")
        if result.is_error:
            raise ToolError(result.output)
        return result.output

    async def _run(self, call: ToolCall, args: dict[str, Any] | None,
                   ctx: ToolContext | None = None) -> ToolResult:
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
            output = await tool.run(args, ctx or ToolContext(
                self.workspace, call.id, file_lock=self.file_lock, cwd_changed=self.cwd_changed,
                save_media=self.save_media, execute=self.run_nested_tool))
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
