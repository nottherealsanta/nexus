"""Nexus Textual theme definition."""

from __future__ import annotations

from textual.theme import Theme
NEXUS_DARK = Theme(
    name="nexus-dark",
    primary="#d18a38",
    secondary="#827b91",
    accent="#a78bfa",
    foreground="#d6d3d1",
    background="#0c0b0b",
    surface="#141212",
    panel="#1d1918",
    success="#86b97a",
    warning="#d18a38",
    error="#d77b72",
    dark=True,
)

__all__ = ["NEXUS_DARK"]
