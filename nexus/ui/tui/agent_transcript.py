"""Live subagent modal: the child's conversation rendered like the root.

The body is the same :class:`ConversationTimeline` the main screen uses, fed
the agent's reducer-backed ``body`` view, so messages, thoughts, tool cards,
diffs, and nested task cards look and expand exactly as they do at the root.
The app pushes every reducer change through :meth:`refresh_agent`, so a
running child updates live.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from ...view import AgentView, ToolCallView
from ..cli.render import sanitize
from .agent_row import agent_status
from .timeline import _SPINNER, ConversationTimeline, format_arguments, tool_status, tool_summary


class AgentTranscriptScreen(ModalScreen[None]):
    """Inspect one agent and its nested agents; live changes arrive from reducer."""

    def __init__(self, agent: AgentView) -> None:
        super().__init__()
        self.agent = agent
        self.parent_agent_id = agent.parent_agent_id or agent.parent
        self._spinner_index = 0
        self._spinner = None

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "return_to_conversation", "Back", priority=True)
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-inspector"):
            yield Static("", id="agent-inspector-heading", markup=False)
            yield Static("", id="agent-inspector-activity", markup=False)
            yield ConversationTimeline(header=False, id="agent-timeline")
            yield Static(
                "Esc returns · scroll up to pause the live tail",
                id="agent-inspector-help",
                markup=False,
            )

    def on_mount(self) -> None:
        self._render_agent()
        self.query_one("#agent-timeline", ConversationTimeline).focus()

    def refresh_agent(self, agent: AgentView) -> None:
        self.agent = agent
        if self.is_mounted:
            self._render_agent()

    def _render_agent(self) -> None:
        agent = self.agent
        heading = (
            f"{sanitize(agent.type or agent.id, 60)} · {sanitize(agent_status(agent), 24)}"
            f" · {sanitize(agent.task or agent.description or '', 120)}"
        )
        self.query_one("#agent-inspector-heading", Static).update(heading)
        self._refresh_activity()
        timeline = self.query_one("#agent-timeline", ConversationTimeline)
        root = self.app.screen_stack[0].query("#conversation")
        timeline.agent_colors = getattr(root.first(), "agent_colors", {}) if root else {}
        self.run_worker(
            timeline.set_view(agent.body), group="agent-transcript", exclusive=True
        )

    def _latest_tool(self) -> ToolCallView | None:
        return max(
            (tool for turn in self.agent.body.turns for tool in turn.tools),
            key=lambda tool: tool.event_seq,
            default=None,
        )

    def _refresh_activity(self) -> None:
        tool = self._latest_tool()
        active = tool is not None and tool_status(tool) == "running"
        if active and self._spinner is None:
            self._spinner = self.set_interval(0.4, self._spin)
        elif not active and self._spinner is not None:
            self._spinner.stop()
            self._spinner = None
        self._show_activity()

    def _show_activity(self) -> None:
        tool = self._latest_tool()
        text = ""
        if tool is not None:
            status = tool_status(tool)
            prefix = f"{_SPINNER[self._spinner_index]} " if status == "running" else ""
            args = format_arguments(tool)
            summary = tool_summary(tool)
            text = f"{prefix}{tool.name or 'tool'}" + (f" · {args}" if args else "") + f" · [{status}]"
            if summary and summary != status:
                text += f" · {sanitize(summary, 140)}"
        self.query_one("#agent-inspector-activity", Static).update(text)

    def _spin(self) -> None:
        tool = self._latest_tool()
        if tool is None or tool_status(tool) != "running":
            return
        self._spinner_index = (self._spinner_index + 1) % len(_SPINNER)
        self._show_activity()

    def on_unmount(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None

    def action_return_to_conversation(self) -> None:
        self.app.action_back_from_agent()

    @property
    def scroll_at_bottom(self) -> bool:
        if not self.is_mounted:
            return True
        return self.query_one("#agent-timeline", ConversationTimeline).at_bottom


__all__ = ["AgentTranscriptScreen"]
