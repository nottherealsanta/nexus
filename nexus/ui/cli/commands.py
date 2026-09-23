"""Slash commands as data (PLAN section 14.11).

Commands are declared once, in :data:`SPECS`, and rendered into help, completion,
and dispatch from that one table. Declarative-first mirrors the tool rule in
PLAN section 0: the command set is data a surface reads, not behaviour a plugin
registers. The interactive app interprets a :class:`ParsedCommand`; this module
never touches a client, so it is trivially unit-testable.

Multiline input is a parser concern too: :func:`is_continuation` decides when a
line ending in a backslash (or an unclosed triple quote) keeps the prompt open,
so the reader stays a dumb line source.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BY_NAME",
    "SPECS",
    "CommandSpec",
    "ParsedCommand",
    "help_text",
    "is_command",
    "is_continuation",
    "parse",
    "strip_continuation",
]


@dataclass(frozen=True)
class CommandSpec:
    """One slash command: its name, one-line summary, and optional usage."""

    name: str
    summary: str
    usage: str = ""


#: The PLAN section 14.11 command set, plus ``/help`` and ``/exit``.
SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("/new", "start a new session", "[id]"),
    CommandSpec("/sessions", "list sessions and switch", "[id]"),
    CommandSpec("/model", "list models or set this session's model", "[list|tier|provider/model|id]"),
    CommandSpec("/tools", "list tools used in this transcript"),
    CommandSpec("/cancel", "cancel the active turn"),
    CommandSpec("/fork", "branch this session", "[at_seq]"),
    CommandSpec("/export", "export this session", "[json|markdown|jsonl]"),
    CommandSpec("/help", "show this help"),
    CommandSpec("/exit", "leave the chat"),
)

BY_NAME: dict[str, CommandSpec] = {spec.name: spec for spec in SPECS}


@dataclass(frozen=True)
class ParsedCommand:
    """A recognised slash command and its whitespace-split arguments."""

    name: str
    args: tuple[str, ...]
    raw: str

    @property
    def spec(self) -> CommandSpec | None:
        return BY_NAME.get(self.name)


def is_command(text: str) -> bool:
    """Whether a line should be interpreted as a slash command."""
    stripped = text.lstrip()
    return stripped.startswith("/") and bool(stripped[1:].strip())


def parse(text: str) -> ParsedCommand | None:
    """Split a slash line into ``(name, args)``; ``None`` if it is not one."""
    stripped = text.strip()
    if not is_command(stripped):
        return None
    head, _, tail = stripped.partition(" ")
    return ParsedCommand(name=head, args=tuple(tail.split()), raw=stripped)


def _trailing_backslashes(text: str) -> int:
    count = 0
    for char in reversed(text):
        if char == "\\":
            count += 1
        else:
            break
    return count


def is_continuation(text: str) -> bool:
    """Whether a line asks for more input.

    A line whose trailing run of backslashes is odd continues; so does a line
    with an odd number of triple-quote fences, which lets a pasted block be
    delimited without escaping every line.
    """
    if _trailing_backslashes(text) % 2 == 1:
        return True
    return text.count('"""') % 2 == 1


def strip_continuation(text: str) -> str:
    """Remove the one backslash that requested a continuation."""
    if _trailing_backslashes(text) % 2 == 1:
        return text[:-1]
    return text


def help_text() -> str:
    """A deterministic help block built from :data:`SPECS`."""
    width = max(len(spec.name) for spec in SPECS)
    lines = ["Commands:"]
    for spec in SPECS:
        usage = f" {spec.usage}" if spec.usage else ""
        lines.append(f"  {spec.name:<{width}}{usage}  {spec.summary}")
    return "\n".join(lines)
