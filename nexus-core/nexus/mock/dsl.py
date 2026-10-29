"""Scenario DSL adapted from old Nexus nexus/devtools/mock/dsl.py."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..llmhub.types import Message, Text, ToolCall, ToolResult


@dataclass(frozen=True)
class Call:
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Turn:
    text: str = ""
    think: str = ""
    calls: tuple[Call, ...] = ()
    usage: tuple[int, int] | None = None


@dataclass(frozen=True)
class Fail:
    message: str = "scripted provider failure"
    recover_at_user_turn: int | None = None
    then: Step | None = None


@dataclass(frozen=True)
class Hang:
    text: str = ""


@dataclass(frozen=True)
class Dyn:
    fn: Callable[[Ctx], Step]


Step = Turn | Fail | Hang | Dyn


@dataclass(frozen=True)
class Result:
    id: str
    name: str
    text: str
    is_error: bool


@dataclass(frozen=True)
class Ctx:
    scenario: str
    actor: str
    step: int
    messages: tuple[Message, ...]

    @property
    def tool_names(self) -> dict[str, str]:
        return {p.id: p.name for m in self.messages if m.role == "assistant" for p in m.parts if isinstance(p, ToolCall)}

    @property
    def all_results(self) -> list[Result]:
        names = self.tool_names
        return [
            Result(p.call_id, names.get(p.call_id, "?"), p.output, p.is_error)
            for m in self.messages
            for p in m.parts
            if isinstance(p, ToolResult)
        ]

    @property
    def last_results(self) -> list[Result]:
        names = self.tool_names
        return [
            Result(p.call_id, names.get(p.call_id, "?"), p.output, p.is_error)
            for m in self.messages[-1:]
            for p in m.parts
            if isinstance(p, ToolResult)
        ]

    @property
    def user_turns(self) -> int:
        return sum(m.role == "user" for m in self.messages)

    @property
    def last_user_text(self) -> str:
        for m in reversed(self.messages):
            if m.role == "user":
                return "".join(p.text for p in m.parts if isinstance(p, Text))
        return ""


@dataclass(frozen=True)
class Check:
    name: str
    fn: Callable[[Ctx], bool]


@dataclass(frozen=True)
class Scenario:
    name: str
    summary: str
    actors: dict[str, Sequence[Step]]
    prompt: str = ""
    slow: bool = False


def say(text: str, *, think: str = "", usage: tuple[int, int] | None = None) -> Turn:
    return Turn(text, think, usage=usage)


def call(name: str, /, **input: Any) -> Call:
    return Call(name, dict(input))


def calls(*items: Call, text: str = "", think: str = "", usage: tuple[int, int] | None = None) -> Turn:
    return Turn(text, think, tuple(items), usage)


def fail(message: str = "scripted provider failure", *, recover_at_user_turn: int | None = None, then: Step | None = None) -> Fail:
    return Fail(message, recover_at_user_turn, then)


def hang(text: str = "") -> Hang:
    return Hang(text)


def dyn(fn: Callable[[Ctx], Step]) -> Dyn:
    return Dyn(fn)


def verdict(name: str, checks: Sequence[Check], *, intro: str | Callable[[Ctx], str] = "") -> Dyn:
    def evaluate(ctx: Ctx) -> Step:
        results = []
        for c in checks:
            try:
                ok = bool(c.fn(ctx))
            except Exception:  # noqa: BLE001 - one broken scenario check should not stop the run
                ok = False
            results.append((c.name, ok))
        opening = intro(ctx) if callable(intro) else intro
        lines = [opening, ""] if opening else []
        lines += [
            f"{'✓' if all(ok for _, ok in results) else '✗'} mock verdict — {name}: {sum(ok for _, ok in results)}/{len(results)} checks passed"
        ]
        lines += [f"- {'✓' if ok else '✗'} {label}" for label, ok in results]
        return Turn(text="\n".join(lines))

    return Dyn(evaluate)
