"""Code-mode kernel rows for the native client (kernel plan §3.11).

The shared rendering lives in ``ui_support/tool_details.py`` (``kernel_parts``,
``_cell_sections``, ``_kernel_sections``, ``kernel_signal_rows``) and the Kernel rows of the
details sidebar in ``ui_support/details.py``. This module only adapts them to the bridge's blocks:
a run member reads ``⧉ kernel started  github (+2 tools) · purpose`` or the notebook header
``In [3]  ✓ 0.04 s · 1 call · purpose``, and the Session tab gains the active agent's Kernel rows.
Old ``code`` rows are untouched. The client has no Python highlighter, so source is numbered
plain text; Rust is unchanged because every row kind used here already exists.
"""
from __future__ import annotations

from ...ui_support.details import kernel_rows
from ...ui_support.text import escape_controls
from ...ui_support.tool_details import kernel_parts


def member_fields(tool, member: dict) -> dict:
    """Title and text of a verb-group member for a kernel tool; ``{}`` for every other tool."""
    parts = kernel_parts(tool)
    if parts is None:
        return {}
    title, rest = parts
    return {"title": escape_controls(title), "text": escape_controls(rest),
            "heading": escape_controls(f"{title} {rest}".rstrip())}


def sidebar_rows(view) -> list[tuple[str, str]]:
    """The Kernel rows appended to the Session tab; empty when the agent never started a kernel."""
    return kernel_rows(view)
