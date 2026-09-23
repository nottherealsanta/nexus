"""Terminal-side approval (PLAN sections 14.6, 14.11).

The daemon is the arbiter of a permission race; a client only *offers* an answer
and learns whether it won. This module owns the small, testable part: turning a
line of input into one of the four decisions, and rendering the request without
letting untrusted text inject terminal escapes. It never imports the permission
engine — the four decision strings are part of the wire contract, not the
engine's enums.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TextIO

from .render import sanitize

#: ``read(prompt) -> str``; raises ``EOFError`` at end of input.
Reader = Callable[[str], Awaitable[str]]

PROMPT = "approve? [y] once / [a] always / [n] deny once / never: "

#: Accepted answers, lower-cased. An unrecognized answer re-prompts, and
#: exhaustion falls back to deny-once: input exhaustion never auto-allows.
DECISIONS: dict[str, str] = {
    "y": "allow_once",
    "yes": "allow_once",
    "once": "allow_once",
    "allow": "allow_once",
    "a": "allow_always",
    "always": "allow_always",
    "n": "deny_once",
    "no": "deny_once",
    "deny": "deny_once",
    "never": "deny_always",
    "deny_always": "deny_always",
}

DENY_ONCE = "deny_once"
MAX_ATTEMPTS = 3


def describe(data: Mapping[str, Any], index: int = 0) -> str:
    """A control-free, multi-line rendering of one approval request."""
    tool = sanitize(data.get("tool") or "?", 80)
    header = f"Permission requested: {tool}"
    lines = [header]
    if data.get("key"):
        lines.append(f"  key: {sanitize(data['key'])}")
    elif data.get("preview"):
        lines.append(f"  preview: {sanitize(data['preview'])}")
    suggestions = data.get("suggestions") or ()
    if suggestions:
        shown = ", ".join(sanitize(item, 80) for item in list(suggestions)[:4])
        lines.append(f"  suggestions: {shown}")
    if data.get("default_rule") and data.get("persistence_available", True):
        lines.append(f"  always persists: {sanitize(data['default_rule'], 160)}")
    elif data.get("default_rule"):
        lines.append("  always persists: unavailable; [a] grants once only")
    return "\n".join(lines)


def parse_decision(raw: str) -> str | None:
    """Map a line of input to a decision string, or ``None`` if unrecognized."""
    return DECISIONS.get(raw.strip().lower())


class Approver:
    """Ask a reader for a decision; never auto-allow on exhausted input."""

    def __init__(self, reader: Reader, *, stderr: TextIO, max_attempts: int = MAX_ATTEMPTS):
        self.reader = reader
        self.stderr = stderr
        self.max_attempts = max(1, max_attempts)

    def _write(self, text: str) -> None:
        self.stderr.write(text)
        self.stderr.flush()

    async def ask(self, data: Mapping[str, Any], index: int = 0) -> str:
        self._write(describe(data, index) + "\n")
        for _ in range(self.max_attempts):
            try:
                raw = await self.reader(PROMPT)
            except EOFError:
                self._write("  input closed; denying this request once\n")
                return DENY_ONCE
            decision = parse_decision(raw)
            if decision is not None:
                self._write(f"  -> {decision}\n")
                return decision
            self._write("  unrecognized choice; enter y, a, n, or never\n")
        self._write("  too many invalid entries; denying this request once\n")
        return DENY_ONCE


__all__ = [
    "DECISIONS",
    "DENY_ONCE",
    "MAX_ATTEMPTS",
    "PROMPT",
    "Approver",
    "Reader",
    "describe",
    "parse_decision",
]
