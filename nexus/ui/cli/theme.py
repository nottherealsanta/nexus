"""Theme data and colour detection for the terminal surface (PLAN section 14.11).

A semantic role maps to a prompt_toolkit style string for the input editor and
to ANSI SGR parameters for scrollback prose. Importing this module never imports
prompt_toolkit; only :func:`build_style` does, and only for a real session.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, TextIO

#: Semantic role -> prompt_toolkit style string.
DEFAULT_THEME: dict[str, str] = {
    "prompt": "bold #5fafff",
    "user": "#5fafff",
    "assistant": "",
    "tool": "ansiyellow",
    "permission": "ansimagenta",
    "error": "ansired",
    "status": "ansibrightblack",
    "thinking": "ansibrightblack italic",
    "agent": "ansicyan",
    "context": "ansicyan",
}

#: Semantic role -> ANSI SGR parameters (scrollback prose); absent means plain.
ANSI_THEME: dict[str, str] = {
    "prompt": "1;34", "user": "34", "tool": "33", "permission": "35",
    "error": "31", "status": "90", "thinking": "2;3", "context": "36", "agent": "36",
}

_RESET = "\x1b[0m"


def merged(theme: Mapping[str, str] | None = None) -> dict[str, str]:
    """The default theme overlaid with a caller's overrides."""
    result = dict(DEFAULT_THEME)
    if theme:
        result.update({str(k): str(v) for k, v in theme.items()})
    return result


def build_style(theme: Mapping[str, str] | None = None) -> Any:
    """Build a prompt_toolkit ``Style``; requires the ``cli`` extra."""
    from .keys import require

    require()
    from prompt_toolkit.styles import Style

    return Style.from_dict(merged(theme))


def color_enabled(
    stream: TextIO | None = None, environ: Mapping[str, str] | None = None
) -> bool:
    """Whether ANSI colour is on: NO_COLOR wins, then FORCE_COLOR, then a TTY.

    ``NO_COLOR`` is honoured by **presence**, not by value: setting it to an
    empty string still disables colour (the spec's intent is opt-out by
    presence), so a caller cannot accidentally re-enable it with ``NO_COLOR=``.
    """
    env = os.environ if environ is None else environ
    if "NO_COLOR" in env:
        return False
    if env.get("FORCE_COLOR"):
        return True
    if env.get("TERM") == "dumb" or stream is None:
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def paint(text: str, role: str, enabled: bool = True) -> str:
    """Wrap ``text`` in the role's ANSI code, or return it unchanged."""
    code = ANSI_THEME.get(role, "")
    return f"\x1b[{code}m{text}{_RESET}" if enabled and code and text else text


__all__ = ["ANSI_THEME", "DEFAULT_THEME", "build_style", "color_enabled", "merged", "paint"]
