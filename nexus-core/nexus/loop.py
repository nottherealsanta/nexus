"""The agent loop (PLAN.md step 2): model → tool calls → results → model, until
the model stops calling tools.

The loop owns no storage. It starts from `messages` (earlier history) and
yields `MessageAdded` each time it appends a message, so the caller can save
it. `turn()` also yields every provider event unchanged plus the loop's own
events, so a client sees the wire traffic, the deltas, and each tool run in
order.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .llmhub import Provider
from .llmhub.types import Done, Event, Message, Part, PartDone, Request, Text, ToolCall, ToolResult
from .tools import DEFAULT_TOOLS, Tool, ToolContext, ToolError

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


@dataclass(frozen=True, slots=True)
class TurnEnd:
    stop: str
    steps: int


LoopEvent = Event | StepStart | ToolStart | ToolEnd | MessageAdded | TurnEnd
Approve = Callable[[ToolCall, dict[str, Any]], Awaitable[bool]]
Inbox = Callable[[], list[str]]  # notices to add (as a user message) before the next model call


def default_instructions(workspace: Path) -> str:
    return (
        "You are a coding agent working in a local workspace.\n"
        f"Workspace: {workspace}\n"
        f"Today: {datetime.now().astimezone().date().isoformat()}\n"
        "Tools: read (files, directories), bash (commands run in the workspace), "
        "edit (exact replacement or apply_patch), write (whole files). "
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
    ) -> None:
        self.provider = provider
        self.model = model
        self.workspace = workspace
        self.tools = {tool.spec.name: tool for tool in tools}
        self.instructions = instructions or default_instructions(workspace)
        self.reasoning_effort = reasoning_effort
        self.approve = approve
        self.max_steps = max_steps
        self.conversation_id = conversation_id or uuid.uuid4().hex
        self.messages: list[Message] = list(messages or [])
        self.inbox = inbox

    def request(self) -> Request:
        return Request(
            model=self.model,
            instructions=self.instructions,
            messages=tuple(self.messages),
            tools=tuple(tool.spec for tool in self.tools.values()),
            reasoning_effort=self.reasoning_effort,
            conversation_id=self.conversation_id,
        )

    def _add(self, message: Message) -> MessageAdded:
        self.messages.append(message)
        return MessageAdded(message)

    async def turn(self, user_text: str) -> AsyncIterator[LoopEvent]:
        yield self._add(Message("user", (Text(user_text),)))
        for step in range(1, self.max_steps + 1):
            notices = self.inbox() if self.inbox is not None else []
            if notices:
                yield self._add(Message("user", tuple(Text(n) for n in notices)))
            yield StepStart(step)
            parts: list[Part] = []
            stop = "other"
            async for event in self.provider.stream(self.request()):
                if isinstance(event, PartDone):
                    parts.append(event.part)
                elif isinstance(event, Done):
                    stop = event.stop
                yield event
            yield self._add(Message("assistant", tuple(parts)))
            calls = [p for p in parts if isinstance(p, ToolCall)]
            if not calls:
                yield TurnEnd(stop, step)
                return
            results: list[ToolResult] = []
            for call in calls:
                args = _parse_args(call.arguments)
                yield ToolStart(call, args)
                result = await self._run(call, args)
                results.append(result)
                yield ToolEnd(call, result)
            yield self._add(Message("tool", tuple(results)))
        yield TurnEnd("max_steps", self.max_steps)

    async def _run(self, call: ToolCall, args: dict[str, Any] | None) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, f"unknown tool {call.name!r}", is_error=True)
        if args is None:
            return ToolResult(call.id, "arguments were not a JSON object", is_error=True)
        if self.approve is not None and not await self.approve(call, args):
            return ToolResult(call.id, "the user declined this tool call", is_error=True)
        try:
            return ToolResult(call.id, await tool.run(args, ToolContext(self.workspace, call.id)))
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
