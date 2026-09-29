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

import os
from dataclasses import dataclass

__all__ = [
    "BY_NAME",
    "DEV_SPECS",
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
    """One slash command: its name, one-line summary, and optional usage.

    ``aliases`` are alternate names for the same command (``/clear`` for
    ``/new``). :func:`parse` resolves them to ``name``, so a surface dispatches on
    the canonical name only.
    """

    name: str
    summary: str
    usage: str = ""
    hidden: bool = False
    aliases: tuple[str, ...] = ()


#: The PLAN section 14.11 command set, plus ``/help`` and ``/exit``.
SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("/new", "Start a new session", "[id]", aliases=("/clear",)),
    CommandSpec("/sessions", "List sessions and switch", "[id]", aliases=("/session",)),
    CommandSpec("/model", "List models or set this session's model", "[list|tier|provider/model|id]"),
    CommandSpec("/effort", "Choose this session's reasoning effort", "[LEVEL]", aliases=("/reasoning",)),
    CommandSpec("/agent", "List or select this session's root agent", "[list|current|reset|NAME]"),
    CommandSpec("/tools", "List tools used in this transcript"),
    CommandSpec("/details", "Show model, context, queue, approvals, and subagents"),
    CommandSpec("/context", "Show assembled prompt, tools, messages, and accounting"),
    CommandSpec("/reconnect", "Reattach and replay missed events"),
    CommandSpec("/cancel", "Cancel the active turn"),
    CommandSpec("/fork", "Branch this session", "[at_seq]"),
    CommandSpec("/export", "Export this session", "[json|markdown|jsonl]"),
    CommandSpec("/help", "Show this help"),
    CommandSpec("/hotkeys", "Show keyboard shortcuts"),
    CommandSpec("/exit", "Leave the chat", aliases=("/quit",)),
    CommandSpec("/worktrees", "Review and manage child worktrees"),
    CommandSpec("/copy", "Copy assembled context as JSON"),
    CommandSpec("/diff", "Show workspace Git diff", "[--staged] [ref]"),
    CommandSpec("/cost", "Show token usage and available cost estimate"),
    CommandSpec("/theme", "Switch between dark and light themes", "[dark|light]"),
    CommandSpec("/settings", "Open Settings"),
    CommandSpec("/verbose", "Toggle full tool output previews"),
    CommandSpec("/mcp", "Show MCP servers"),
    CommandSpec("/skills", "Show active skills"),
    CommandSpec("/tasks", "Show background agent tasks"),
    CommandSpec("/reload", "Reload extensions and MCP"),
    CommandSpec("/review", "Ask the agent to review changes"),
    CommandSpec("/commit", "Ask the agent to commit changes"),
    CommandSpec("/archived", "Browse archived sessions", aliases=("/resume",)),
)

#: ``/mock`` exists only in dev mode (``NEXUS_DEV=1``; MOCK_PLAN §3.2).
DEV_SPECS = (CommandSpec("/mock", "Run a scripted mock scenario", "[list|NAME|clean] [--speed N] [--seed N]"),)
if os.environ.get("NEXUS_DEV", "").strip().lower() in {"1", "true", "yes", "on"}:
    SPECS += DEV_SPECS

#: Common typos accepted silently; unlike ``aliases`` they are never listed.
_TYPOS: dict[str, str] = {"/sesssion": "/sessions"}

#: Every accepted name, canonical and alias, mapped to its spec.
BY_NAME: dict[str, CommandSpec] = {
    name: spec for spec in SPECS for name in (spec.name, *spec.aliases)
}
BY_NAME.update({typo: BY_NAME[name] for typo, name in _TYPOS.items()})


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
    """Split a slash line into ``(name, args)``; ``None`` if it is not one.

    An alias resolves to its command's canonical name.
    """
    stripped = text.strip()
    if not is_command(stripped):
        return None
    head, _, tail = stripped.partition(" ")
    spec = BY_NAME.get(head)
    return ParsedCommand(name=spec.name if spec else head, args=tuple(tail.split()), raw=stripped)


def _trailing_backslashes(text: str) -> int:
    return len(text) - len(text.rstrip("\\"))


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
    visible = [spec for spec in SPECS if not spec.hidden]
    names = {spec.name: ", ".join((spec.name, *spec.aliases)) for spec in visible}
    width = max(len(name) for name in names.values())
    lines = ["Commands:"]
    for spec in sorted(visible, key=lambda spec: spec.name):
        usage = f" {spec.usage}" if spec.usage else ""
        lines.append(f"  {names[spec.name]:<{width}}{usage}  {spec.summary}")
    lines.append(
        "Controls: Enter submits; Shift+Enter inserts a line; Ctrl+P opens "
        "commands (see \"Show keyboard shortcuts\"); Ctrl+C cancels."
    )
    return "\n".join(lines)
