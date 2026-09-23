"""Theme data for the terminal surface (PLAN section 14.11).

Colours are declarative: a mapping from a semantic role to a prompt_toolkit
style string. ``nexus.toml``'s ``[ui]`` section may overlay this mapping, but the
defaults here are the fallback. Importing this module never imports
prompt_toolkit; only :func:`build_style` does, and only when a real terminal
session is being built.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
}


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


__all__ = ["DEFAULT_THEME", "build_style", "merged"]
