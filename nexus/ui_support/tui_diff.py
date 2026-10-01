"""Inline file diffs under Edit and Patch activity rows (textual-diff-view).

The durable ``diff`` artifact on a :class:`ToolCallView` is the only input;
nothing is read from disk. :func:`diff_sections` turns its bounded unified
hunk into per-file before/after text, and each file becomes one ``DiffView``.
Split layout always places original text on the left and updated text on the right.
"""
from __future__ import annotations

from collections.abc import Mapping

from textual.content import Content
from textual.containers import Vertical
from textual.events import MouseScrollDown, MouseScrollUp
from textual.widget import Widget
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


class FileDiffView(DiffView):
    """A clickable, wrapping preview whose wheel scrolls the conversation."""

    DEFAULT_CSS = """
    FileDiffView DiffScrollContainer {
        overflow: hidden hidden;
    }
    """

    async def on_mount(self) -> None:
        await super().on_mount()
        self.call_after_refresh(self._disable_preview_selection)

    async def recompose(self) -> None:
        await super().recompose()
        self._disable_preview_selection()

    def _disable_preview_selection(self) -> None:
        # This preview is a click target for the tool's details. The library's
        # wrapped code visuals should not start a competing text selection.
        for widget in self.walk_children(Widget):
            widget.ALLOW_SELECT = False

    def _scroll_conversation(self, event: MouseScrollDown | MouseScrollUp, direction: int) -> None:
        for ancestor in self.ancestors:
            if isinstance(ancestor, Widget) and ancestor.allow_vertical_scroll:
                ancestor.scroll_relative(
                    y=direction * self.app.scroll_sensitivity_y,
                    animate=False, immediate=True,
                )
                event.stop()
                return

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        self._scroll_conversation(event, 1)

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        self._scroll_conversation(event, -1)

    def get_title(self) -> Content:
        additions, removals = self.counts
        return Content.from_markup(
            "[dim]$path[/dim] ([$text-success][b]+$additions[/b][/], [$text-error][b]-$removals[/b][/])",
            path=self.path_modified, additions=additions, removals=removals,
        ).stylize_before("$text")


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
            yield FileDiffView(
                section.path,
                section.path,
                section.before,
                section.after,
                split=True,
                auto_split=False,
                annotations=True,
                wrap=True,
            )
        if self._diff.get("truncated"):
            yield Static("Diff preview clipped · Enter for details", classes="tool-diff-clipped", markup=False)
