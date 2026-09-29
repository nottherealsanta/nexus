"""Scenario DSL (MOCK_PLAN §6.1).

A :class:`Scenario` maps *actor* names to ordered lists of steps. One step is
one assistant message. The mock provider picks the step from the number of
assistant messages already in the request, so a step is a pure function of the
conversation (concurrency-safe, replayable, forkable).

Steps: :class:`Turn` (thinking, text, parallel tool calls, usage), :class:`Fail`
(raise a provider error, optionally only the first ``times`` attempts),
:class:`Hang` (park until cancelled) and :class:`Dyn` (a callable that reads the
conversation and returns another step, which is how a scenario branches).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...model.message import Message, Text, ToolResult, ToolUse

__all__ = [
    "Call",
    "Check",
    "Ctx",
    "Dyn",
    "Fail",
    "Hang",
    "Scenario",
    "Step",
    "Turn",
    "call",
    "calls",
    "dyn",
    "fail",
    "hang",
    "say",
    "task",
    "verdict",
]


@dataclass(frozen=True)
class Call:
    """One scripted tool call. ``child`` names the actor a ``subagent`` call starts."""

    name: str
    input: dict[str, Any] = field(default_factory=dict)
    child: str | None = None


@dataclass(frozen=True)
class Turn:
    text: str = ""
    think: str = ""
    calls: tuple[Call, ...] = ()
    #: Scripted token usage, ``(input, output)``; ``None`` derives a small default.
    usage: tuple[int, int] | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class Fail:
    message: str = "scripted provider failure"
    #: Stateless recovery: succeed (run ``then``) once the conversation holds this
    #: many user text messages (kickoff = 1, a retry = 2). ``None`` = always fail.
    recover_at_user_turn: int | None = None
    then: Step | None = None
    #: Raise a malformed-tool-call error instead of a provider error.
    malformed: bool = False


@dataclass(frozen=True)
class Hang:
    """Stream ``text`` then park until the turn is cancelled."""

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
    """What a :class:`Dyn` step and a :class:`Check` can see."""

    scenario: str
    actor: str
    step: int
    speed: float
    seed: int
    messages: tuple[Message, ...]

    @property
    def last_results(self) -> list[Result]:
        """Tool results answering the most recent assistant message."""
        return self.results_at(len(self.messages) - 1)

    def results_at(self, index: int) -> list[Result]:
        if index < 0 or index >= len(self.messages):
            return []
        msg = self.messages[index]
        if msg.role != "user":
            return []
        names = self.tool_names()
        out: list[Result] = []
        for block in msg.content:
            if isinstance(block, ToolResult):
                text = "".join(b.text for b in block.content if isinstance(b, Text))
                out.append(Result(block.tool_use_id, names.get(block.tool_use_id, "?"), text, block.is_error))
        return out

    def tool_names(self) -> dict[str, str]:
        return {
            b.id: b.name
            for m in self.messages
            if m.role == "assistant"
            for b in m.content
            if isinstance(b, ToolUse)
        }

    @property
    def all_results(self) -> list[Result]:
        out: list[Result] = []
        for i in range(len(self.messages)):
            out.extend(self.results_at(i))
        return out

    @property
    def user_turns(self) -> int:
        """How many user messages carry text (tool-result messages excluded)."""
        return sum(1 for m in self.messages if m.role == "user" and any(isinstance(b, Text) for b in m.content))

    @property
    def last_user_text(self) -> str:
        for msg in reversed(self.messages):
            if msg.role == "user":
                text = "".join(b.text for b in msg.content if isinstance(b, Text))
                if text:
                    return text
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
    tags: tuple[str, ...] = ()
    est_seconds: int = 5
    version: int = 1
    #: The user-visible kickoff prompt (the directive is prepended by the host).
    prompt: str = ""
    #: Whether a human is expected to act (approve, answer, cancel).
    interactive: bool = False
    #: Excluded from the default ``all`` run and CI matrix.
    slow: bool = False


# -- builders --------------------------------------------------------------


def say(text: str, *, think: str = "", usage: tuple[int, int] | None = None) -> Turn:
    return Turn(text=text, think=think, usage=usage)


def call(name: str, /, **input: Any) -> Call:
    return Call(name, dict(input))


def calls(*items: Call, text: str = "", think: str = "", usage: tuple[int, int] | None = None) -> Turn:
    """One assistant message holding the given tool calls (parallel if several)."""
    return Turn(text=text, think=think, calls=tuple(items), usage=usage)


def task(actor: str, prompt: str, *, subagent_type: str = "task", **extra: Any) -> Call:
    """A ``subagent`` call whose child is scripted as ``actor``."""
    payload: dict[str, Any] = {"prompt": prompt, "subagent_type": subagent_type, "description": actor}
    payload.update(extra)
    return Call("subagent", payload, child=actor)


def fail(message: str = "scripted provider failure", *, recover_at_user_turn: int | None = None,
         then: Step | None = None, malformed: bool = False) -> Fail:
    return Fail(message, recover_at_user_turn=recover_at_user_turn, then=then, malformed=malformed)


def hang(text: str = "") -> Hang:
    return Hang(text)


def dyn(fn: Callable[[Ctx], Step]) -> Dyn:
    return Dyn(fn)


def verdict(name: str, checks: Sequence[Check], *, intro: str | Callable[[Ctx], str] = "") -> Dyn:
    """Final root step: evaluate ``checks`` against the whole conversation.

    The verdict is in-band (plain assistant text), so it is durable, replayed
    and rendered by every surface without any client support. A text-only
    message ends the turn, so this must be the *last* step and carries the
    closing words as ``intro`` instead of a separate ``say``.
    """

    def _evaluate(ctx: Ctx) -> Step:
        results: list[tuple[str, bool]] = []
        for check in checks:
            try:
                ok = bool(check.fn(ctx))
            except Exception:  # noqa: BLE001 - a broken check is a failed check
                ok = False
            results.append((check.name, ok))
        passed = sum(1 for _, ok in results if ok)
        mark = "✓" if passed == len(results) else "✗"
        opening = intro(ctx) if callable(intro) else intro
        lines = [opening, ""] if opening else []
        lines += [f"{mark} mock verdict — {name}: {passed}/{len(results)} checks passed"]
        lines += [f"- {'✓' if ok else '✗'} {label}" for label, ok in results]
        return Turn(text="\n".join(lines))

    return Dyn(_evaluate)
