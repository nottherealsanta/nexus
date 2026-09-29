"""Inline file diffs under Edit and Patch activity rows (textual-diff-view).

The durable ``diff`` artifact on a :class:`ToolCallView` is the only input;
nothing is read from disk. :func:`diff_sections` turns its bounded unified
hunk into per-file before/after text, and each file becomes one ``DiffView``.
Unified layout by default, split automatically when the code fits side by side.
"""
from __future__ import annotations

from collections.abc import Mapping

from textual.containers import Vertical
from textual.widgets import Static
from textual_diff_view import DiffView

from ..view import ToolCallView
from .timeline import DIFF_TOOLS, diff_sections, tool_status

__all__ = ["ToolDiff", "tool_diff_signature"]


def tool_diff_signature(tool: ToolCallView) -> tuple[object, ...] | None:
    """What an inline diff for ``tool`` depends on, or ``None`` for no diff."""
    diff = tool.diff
    if (
        tool.name.casefold() not in DIFF_TOOLS
        or tool_status(tool) != "completed"
        or not isinstance(diff, Mapping)
        or not diff.get("hunk")
    ):
        return None
    return (diff.get("path"), diff.get("hunk"), bool(diff.get("truncated")))


class ToolDiff(Vertical):
    """One ``DiffView`` per changed file, plus a clipped-preview note."""

    DEFAULT_CSS = """
    ToolDiff {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
    }
    ToolDiff DiffView {
        margin: 1 0 0 0;
    }
    ToolDiff .tool-diff-clipped {
        color: $text-muted;
        height: 1;
    }
    """

    def __init__(self, tool: ToolCallView, **kwargs: object) -> None:
        super().__init__(classes="tool-diff", **kwargs)  # type: ignore[arg-type]
        self.signature = tool_diff_signature(tool)
        self._diff: Mapping[str, object] = tool.diff if isinstance(tool.diff, Mapping) else {}

    def compose(self):
        for section in diff_sections(self._diff):
            yield DiffView(
                section.path,
                section.path,
                section.before,
                section.after,
                split=False,
                auto_split=True,
                annotations=True,
                wrap=True,
            )
        if self._diff.get("truncated"):
            yield Static("Diff preview clipped · Enter for details", classes="tool-diff-clipped", markup=False)
