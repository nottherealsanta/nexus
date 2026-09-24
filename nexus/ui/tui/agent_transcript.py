"""Full reducer-backed child transcript inspector modal."""

from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static

from ...view import AgentView
from ..cli.render import escape_controls, sanitize
from .agent_row import AgentRow, agent_status
from .timeline import format_arguments

_FIELD_LIMIT = 4096


class AgentTranscriptScreen(ModalScreen[None]):
    """Inspect one agent and its nested agents; live changes arrive from reducer."""

    def __init__(self, agent: AgentView) -> None:
        super().__init__()
        self.agent = agent
        self.parent_agent_id = agent.parent_agent_id or agent.parent
        self._at_bottom = True

    BINDINGS = [Binding("escape", "return_to_conversation", "Back", priority=True)]

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-inspector"):
            yield Static("", id="agent-inspector-heading", markup=False)
            with VerticalScroll(id="agent-inspector-scroll"):
                yield Markdown("", id="agent-inspector-transcript", open_links=False)
                yield Vertical(id="agent-inspector-children")
            yield Static("Esc returns · scroll to pause live tail", id="agent-inspector-help", markup=False)

    def on_mount(self) -> None:
        self.query_one("#agent-inspector-heading", Static).update(
            f"{sanitize(self.agent.type or self.agent.id, 60)} · {sanitize(agent_status(self.agent), 24)}"
            f" · {sanitize(self.agent.task or self.agent.description or '', 120)}"
        )
        self.run_worker(self._update_content(True), group="agent-transcript", exclusive=True)
        self.query_one("#agent-inspector-transcript", Markdown).focus()

    def refresh_agent(self, agent: AgentView) -> None:
        self.agent = agent
        if not self.is_mounted:
            return
        heading = (
            f"{sanitize(agent.type or agent.id, 60)} · {sanitize(agent_status(agent), 24)}"
            f" · {sanitize(agent.task or agent.description or '', 120)}"
        )
        self.query_one("#agent-inspector-heading", Static).update(heading)
        scroll = self.query_one("#agent-inspector-scroll", VerticalScroll)
        self.run_worker(self._update_content(self._at_bottom), group="agent-transcript", exclusive=True)

    async def _update_content(self, follow: bool) -> None:
        markdown = render_agent(self.agent)
        widget = self.query_one("#agent-inspector-transcript", Markdown)
        await widget.update(markdown)
        children = self.query_one("#agent-inspector-children", Vertical)
        await children.remove_children()
        for agent_id in self.agent.body.agent_order:
            child = self.agent.body.agents.get(agent_id)
            if child is not None and _is_direct_child(child, self.agent.id):
                await children.mount(AgentRow(child, depth=1, classes="agent-row"))
        if follow:
            self.query_one("#agent-inspector-scroll", VerticalScroll).scroll_end(animate=False)

    def on_scroll(self, event) -> None:
        scroll = self.query_one("#agent-inspector-scroll", VerticalScroll)
        self._at_bottom = scroll.scroll_y >= scroll.max_scroll_y

    def on_mouse_scroll_up(self, event) -> None:
        self._at_bottom = False

    def on_scroll_up(self, event) -> None:
        self._at_bottom = False

    def action_return_to_conversation(self) -> None:
        self.app.action_back_from_agent()

    @property
    def scroll_at_bottom(self) -> bool:
        if not self.is_mounted:
            return self._at_bottom
        scroll = self.query_one("#agent-inspector-scroll", VerticalScroll)
        return scroll.scroll_y >= scroll.max_scroll_y


def _is_direct_child(agent: AgentView, parent_id: str) -> bool:
    return agent.parent_agent_id == parent_id or agent.parent == parent_id or agent.parent is None


def render_agent(agent: AgentView) -> str:
    """Render child messages, bounded tool data/errors, and statuses as Markdown."""
    sections: list[str] = []
    for turn in agent.body.turns:
        for message in turn.messages:
            if message.role == "assistant" and message.text:
                sections.append("### Assistant\n\n" + escape_controls(message.text))
            elif message.role == "user" and message.text:
                sections.append("### Input\n\n" + escape_controls(message.text))
        for tool in turn.tools:
            sections.append("### Tool call")
            sections.append(
                f"Name/status: `{_inline_code(sanitize(tool.name or 'tool', 80))}` · "
                f"`{_inline_code(sanitize(tool.status, 24))}`"
            )
            if tool.input:
                # Tool inputs can contain whole Write files; child inspectors use
                # the same bounded, payload-free argument summary as task cards.
                arguments = format_arguments(tool)
                if arguments:
                    sections.append("Arguments:\n```text\n" + _safe_code(arguments) + "\n```")
            if tool.name.casefold() != "write" and (tool.display or tool.context_note):
                sections.append("Output:\n```text\n" + _safe_code(escape_controls(str(tool.display or tool.context_note))[:_FIELD_LIMIT]) + "\n```")
            if tool.name.casefold() != "write" and tool.result:
                sections.append("Result:\n```json\n" + _safe_code(_bounded_json(tool.result)) + "\n```")
            if tool.error:
                sections.append("Error:\n```text\n" + _safe_code(escape_controls(tool.error)[:_FIELD_LIMIT]) + "\n```")
            elif tool.is_error:
                sections.append("Tool reported an error.")
        if turn.error:
            sections.append("### Turn error\n\n" + escape_controls(turn.error)[:_FIELD_LIMIT])
    if agent.error:
        sections.append("### Agent error\n\n" + escape_controls(agent.error)[:_FIELD_LIMIT])
    if not sections:
        sections.append("_No transcript content yet._")
    return "\n\n---\n\n".join(sections)


def _bounded_json(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return escape_controls(text)[:_FIELD_LIMIT]


def _safe_code(text: str) -> str:
    return text.replace("```", "` ` `")


def _inline_code(text: str) -> str:
    return text.replace("\\", "\\\\").replace("`", "\\`")


__all__ = ["AgentTranscriptScreen", "render_agent"]
