"""Keyboard and mouse selectable row for one reducer-owned AgentView."""

from __future__ import annotations

from textual import on
from textual.widgets import Button

from ...view import AgentView
from ..cli.render import sanitize
from .messages import AgentOpenRequested
from .timeline import format_arguments, tool_status, tool_summary


def latest_activity(agent: AgentView) -> str:
    """Compact latest reducer-visible activity; never stores a parallel log."""
    for turn in reversed(agent.body.turns):
        if turn.tools:
            tool = max(turn.tools, key=lambda item: item.event_seq)
            args = format_arguments(tool)
            detail = tool_summary(tool)
            state = tool_status(tool)
            activity = (
                f"{tool.name or 'tool'}" + (f" {args}" if args else "") + f" · {state}"
            )
            return activity + (f": {detail}" if detail else "")
        for message in reversed(turn.messages):
            if message.role == "assistant" and (message.model or message.provider):
                return f"model: {message.provider or '?'} / {message.model or '?'}"
    if agent.model:
        return f"model: {agent.model}"
    return "waiting for activity"


def agent_status(agent: AgentView) -> str:
    status = (agent.status or "running").casefold()
    if agent.is_error or agent.ok is False:
        return "failed"
    if agent.clamped:
        return f"clamped · {status}"
    return status


class AgentRow(Button):
    """One bounded summary row, addressable by Enter or mouse click."""

    def __init__(self, agent: AgentView, *, depth: int = 0, **kwargs) -> None:
        self.agent_id = agent.id
        self.agent = agent
        self.depth = depth
        self._label_text = self._label()
        super().__init__(self._label_text, **kwargs)

    def _label(self) -> str:
        agent = self.agent
        canonical = sanitize(agent.type or agent.id, 60)
        task = sanitize(agent.task or agent.description or agent.id, 100)
        activity = sanitize(latest_activity(agent), 100)
        status = sanitize(agent_status(agent), 24)
        indent = "  " * min(self.depth, 8)
        return f"{indent}{canonical} · {task} · {status}\n{indent}  ↳ {activity}"

    def refresh_agent(self, agent: AgentView) -> None:
        self.agent = agent
        self._label_text = self._label()
        self.label = self._label_text

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button is self:
            self.post_message(AgentOpenRequested(self.agent_id))


__all__ = ["AgentRow", "agent_status", "latest_activity"]
