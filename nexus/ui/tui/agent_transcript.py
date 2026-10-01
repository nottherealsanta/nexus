"""Live sub agent page: the child's session laid out exactly like the root.

The page is a full screen, not a dialog: the same top bar, the same
:class:`ConversationTimeline` opened by the same context header (filled from
the request the child actually sent: its system prompt and every tool
definition), and the same details sidebar. The timeline is fed the agent's
reducer-backed ``body`` view, so messages, thoughts, tool cards, diffs, and
nested task cards look and expand exactly as they do at the root. The app
pushes every reducer change through :meth:`refresh_agent`, so a running child
updates live. The composer row is read-only: a subagent takes no input.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Static
from textual.worker import Worker

from ...ui_support.tui_context_header import ContextHeader
from ...ui_support.tui_panels import DetailsSidebar, TopBar
from ...view import AgentView, ToolCallView
from ..cli.render import sanitize
from .agent_row import agent_status
from .timeline import (
    _SPINNER,
    ConversationTimeline,
    format_arguments,
    tool_status,
    tool_summary,
)

#: Agent status → the top bar's status vocabulary (a failure shows as done).
_TOPBAR_STATUS = {"spawned": "working", "running": "working", "completed": "done", "failed": "done"}


class AgentTranscriptScreen(Screen[None]):
    """Inspect one agent and its nested agents; live changes arrive from reducer."""

    def __init__(self, agent: AgentView, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(classes="agent-page")
        self.agent = agent
        self.context = dict(context or {})
        self.parent_agent_id = agent.parent_agent_id or agent.parent
        self._spinner_index = 0
        self._spinner = None
        self._context_pending = False
        self._context_ts = 0.0
        self._timeline_worker: Worker[None] | None = None

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "return_to_conversation", "Back", priority=True)
    ]

    def compose(self) -> ComposeResult:
        yield TopBar(id="top-bar", details_toggle=True)
        with Horizontal(id="main-layout"):
            with Vertical(id="main-column"):
                yield Static("", id="agent-inspector-heading", markup=False)
                yield ConversationTimeline(id="agent-timeline")
                yield Static("", id="agent-inspector-activity", markup=False)
                yield Static(
                    "Sub agent · read-only · Esc returns to the parent",
                    id="agent-inspector-help",
                    markup=False,
                )
            yield DetailsSidebar(id="details-sidebar")

    def on_mount(self) -> None:
        self._render_agent()
        self.set_context(self.context)
        self.query_one("#agent-timeline", ConversationTimeline).focus()

    def refresh_agent(self, agent: AgentView) -> None:
        self.agent = agent
        if self.is_mounted:
            self._render_agent()
            if not self.context:
                self.set_context(None)
            # Opened before the child's first request: pick its context up
            # once it has been recorded (one fetch in flight, at most 1/s).
            now = time.monotonic()
            if not self.context and not self._context_pending and now - self._context_ts >= 1.0:
                self._context_pending, self._context_ts = True, now
                self.run_worker(self._fetch_context(), group="agent-context")

    async def _fetch_context(self) -> None:
        try:
            controller = getattr(self.app, "controller", None)
            result = await controller.get_agent(self.agent.id) if controller else None
            if result and result.get("context"):
                self.set_context(result["context"])
        except Exception:  # noqa: BLE001, S110 - the header retries on the next update
            pass
        finally:
            self._context_pending = False

    def set_context(self, context: Mapping[str, Any] | None) -> None:
        """Fill the context header from the request the child actually sent."""
        self.context = dict(context or {})
        header = next(iter(self.query(ContextHeader)), None)  # None before compose
        if header is None:
            return
        # Recorded once the child sends; children from before that never have one.
        done = (self.agent.status or "").casefold() == "completed"
        header.set_sent_request(
            self.context, session=self.agent.id,
            note="Not recorded for this agent" if done else "Waiting for the first request…",
        )
        if self.context:
            self._render_agent()  # the Task block shows the recorded prompt

    def _render_agent(self) -> None:
        agent = self.agent
        status = agent_status(agent)
        heading = f"{sanitize(agent.type or agent.id, 60)} · {sanitize(status, 24)}"
        self.query_one("#agent-inspector-heading", Static).update(heading)
        header = next(iter(self.query(ContextHeader)), None)
        if header is not None:
            header.set_task(self._task_prompt())
        self.query_one(TopBar).set_session(
            f"{sanitize(agent.type or 'Sub agent', 40)} · {sanitize(agent.description or agent.task or agent.id, 80)}",
            _TOPBAR_STATUS.get((agent.status or "running").casefold(), "idle"),
        )
        self.query_one(DetailsSidebar).set_view(
            agent.body,
            phase="running" if status == "spawned" else status,
            agent=agent.type or "subagent",
            model=agent.model or str(self.context.get("model") or ""),
            effort=None,
        )
        self._refresh_activity()
        timeline = self.query_one("#agent-timeline", ConversationTimeline)
        root = self.app.screen_stack[0].query("#conversation")
        timeline.agent_colors = getattr(root.first(), "agent_colors", {}) if root else {}
        if self._timeline_worker is not None and self._timeline_worker.is_running:
            self._timeline_worker.cancel()

        async def update_timeline() -> None:
            await timeline.set_view(agent.body)

        self._timeline_worker = self.run_worker(
            update_timeline, group="agent-transcript"
        )

    def _task_prompt(self) -> str:
        """What the root agent asked: the recorded prompt, else the call's input."""
        messages = self.context.get("messages") or ()
        blocks = messages[0].get("blocks") if messages and isinstance(messages[0], Mapping) else ()
        recorded = next((str(b.get("text")) for b in blocks or () if isinstance(b, Mapping) and b.get("text")), "")
        call_id, view = self.agent.parent_call_id, getattr(getattr(self.app, "controller", None), "view", None)

        def find(conversation, depth: int = 0) -> str:
            for tool in (tool for turn in conversation.turns for tool in turn.tools):
                if tool.call_id == call_id and isinstance(tool.input, Mapping):
                    return str(tool.input.get("prompt") or "")
            nested = (find(a.body, depth + 1) for a in conversation.agents.values()) if depth < 8 else ()
            return next((found for found in nested if found), "")

        return recorded or (find(view) if call_id and view is not None else "")

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
            self._spinner = self.set_interval(0.12, self._spin)
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
        if self._timeline_worker is not None:
            self._timeline_worker.cancel()
            self._timeline_worker = None

    def on_top_bar_details_toggled(self, event: TopBar.DetailsToggled) -> None:
        event.stop()  # ▐ toggles this page's details; ▌ and + are hidden here
        sidebar = self.query_one(DetailsSidebar)
        sidebar.display = not sidebar.display

    def action_return_to_conversation(self) -> None:
        self.app.action_back_from_agent()

    @property
    def scroll_at_bottom(self) -> bool:
        if not self.is_mounted:
            return True
        return self.query_one("#agent-timeline", ConversationTimeline).at_bottom


__all__ = ["AgentTranscriptScreen"]
