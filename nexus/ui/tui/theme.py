"""Nexus Textual themes: an opencode-style dark workbench and its light twin.

``app.tcss`` styles everything through the ``$nx-*`` variables below, so a
theme switch recolors the whole shell without per-widget overrides.
"""

from __future__ import annotations

from textual.theme import Theme

_DARK = {
    "nx-bg": "#0a0a0a",
    "nx-panel": "#141414",
    "nx-element": "#1e1e1e",
    "nx-element-hi": "#282828",
    "nx-border": "#2c2c2c",
    "nx-border-strong": "#484848",
    "nx-text": "#eeeeee",
    "nx-muted": "#a3a3a3",
    "nx-quiet": "#6f6f6f",
    "nx-accent": "#fab283",
    "nx-on-accent": "#1a0f08",
    "nx-blue": "#5c9cf5",
    "nx-purple": "#9d7cd8",
    "nx-success": "#7fd88f",
    "nx-warning": "#f5a742",
    "nx-error": "#e06c75",
}
_LIGHT = {
    "nx-bg": "#ffffff",
    "nx-panel": "#f5f5f4",
    "nx-element": "#ececea",
    "nx-element-hi": "#e2e2df",
    "nx-border": "#dcdcd8",
    "nx-border-strong": "#b9b9b4",
    "nx-text": "#1b1b1b",
    "nx-muted": "#555555",
    "nx-quiet": "#8a8a8a",
    "nx-accent": "#c8672f",
    "nx-on-accent": "#ffffff",
    "nx-blue": "#2f6fd6",
    "nx-purple": "#7a52c7",
    "nx-success": "#268044",
    "nx-warning": "#a86200",
    "nx-error": "#c23a4a",
}


def _theme(name: str, values: dict[str, str], *, dark: bool) -> Theme:
    return Theme(
        name=name,
        primary=values["nx-accent"],
        secondary=values["nx-blue"],
        accent=values["nx-purple"],
        foreground=values["nx-text"],
        background=values["nx-bg"],
        surface=values["nx-panel"],
        panel=values["nx-element"],
        success=values["nx-success"],
        warning=values["nx-warning"],
        error=values["nx-error"],
        dark=dark,
        variables=dict(values),
    )


NEXUS_DARK = _theme("nexus-dark", _DARK, dark=True)
NEXUS_LIGHT = _theme("nexus-light", _LIGHT, dark=False)
#: Selectable in Settings, in display order.
NEXUS_THEMES = (NEXUS_DARK, NEXUS_LIGHT)

__all__ = ["NEXUS_DARK", "NEXUS_LIGHT", "NEXUS_THEMES"]
