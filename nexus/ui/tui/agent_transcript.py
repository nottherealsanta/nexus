"""Full reducer-backed child transcript inspector modal."""

from __future__ import annotations

import json
import re
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static

from ...view import AgentView, ToolCallView
from ..cli.render import escape_controls, sanitize
from .agent_row import AgentRow, agent_status
from .timeline import _SPINNER, format_arguments, tool_status, tool_summary

_FIELD_LIMIT = 4096


class AgentTranscriptScreen(ModalScreen[None]):
    """Inspect one agent and its nested agents; live changes arrive from reducer."""

    def __init__(self, agent: AgentView) -> None:
        super().__init__()
        self.agent = agent
        self.parent_agent_id = agent.parent_agent_id or agent.parent
        self._at_bottom = True
        self._spinner_index = 0
        self._spinner = None

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "return_to_conversation", "Back", priority=True)
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-inspector"):
            yield Static("", id="agent-inspector-heading", markup=False)
            with VerticalScroll(id="agent-inspector-scroll"):
                yield Static("", id="agent-inspector-activity", markup=False)
                yield Markdown("", id="agent-inspector-transcript", open_links=False)
                yield Vertical(id="agent-inspector-children")
            yield Static(
                "Esc returns · scroll to pause live tail",
                id="agent-inspector-help",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#agent-inspector-heading", Static).update(
            f"{sanitize(self.agent.type or self.agent.id, 60)} · {sanitize(agent_status(self.agent), 24)}"
            f" · {sanitize(self.agent.task or self.agent.description or '', 120)}"
        )
        self.run_worker(
            self._update_content(True), group="agent-transcript", exclusive=True
        )
        self._refresh_activity()
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
        self._refresh_activity()
        self.run_worker(
            self._update_content(self._at_bottom),
            group="agent-transcript",
            exclusive=True,
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
        if active and self._spinner is None and self.is_mounted:
            self._spinner = self.set_interval(0.4, self._spin)
        elif not active and self._spinner is not None:
            self._spinner.stop()
            self._spinner = None
        self._show_activity()

    def _show_activity(self) -> None:
        if not self.is_mounted:
            return
        tool = self._latest_tool()
        if tool is None:
            text = ""
        else:
            status = tool_status(tool)
            active = status == "running"
            prefix = f"{_SPINNER[self._spinner_index]} " if active else ""
            args = format_arguments(tool)
            summary = tool_summary(tool)
            text = (
                f"{prefix}{tool.name or 'tool'}"
                + (f" · {args}" if args else "")
                + f" · [{status}]"
            )
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
            self.query_one("#agent-inspector-scroll", VerticalScroll).scroll_end(
                animate=False
            )

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
    return (
        agent.parent_agent_id == parent_id
        or agent.parent == parent_id
        or agent.parent is None
    )


def render_agent(agent: AgentView) -> str:
    """Render child messages, bounded tool data/errors, and statuses as Markdown."""
    sections: list[str] = []
    for turn in agent.body.turns:
        for message in turn.messages:
            if message.role == "assistant" and message.text:
                sections.append("### Assistant\n\n" + escape_controls(message.text))
            elif message.role == "user" and message.text:
                sections.append("### Input\n\n" + escape_controls(message.text))
            if message.role == "assistant" and any(
                block.kind == "thinking" for block in message.blocks
            ):
                thought = message.thinking
                sections.append(
                    "### Thought"
                    + ("\n\n" + escape_controls(thought) if thought else "")
                )
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
                    sections.append("Arguments:\n" + _fenced("text", arguments))
            if tool.name.casefold() != "write" and (tool.display or tool.context_note):
                sections.append(
                    "Output:\n"
                    + _fenced(
                        "text",
                        escape_controls(str(tool.display or tool.context_note))[
                            :_FIELD_LIMIT
                        ],
                    )
                )
            if tool.name.casefold() != "write" and tool.result:
                sections.append("Result:\n" + _fenced("json", _bounded_json(tool.result)))
            if tool.error:
                sections.append(_error_section("Error", tool.error))
            elif tool.is_error:
                sections.append("Tool reported an error.")
        if turn.error:
            sections.append(_error_section("Turn error", turn.error, heading=True))
    if agent.error:
        sections.append(_error_section("Agent error", agent.error, heading=True))
    if not sections:
        sections.append("_No transcript content yet._")
    return "\n\n---\n\n".join(sections)


def _bounded_json(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return escape_controls(text)[:_FIELD_LIMIT]


def _fenced(language: str, text: str) -> str:
    """Fence literal tool data without altering embedded backticks."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def _error_section(title: str, value: str, *, heading: bool = False) -> str:
    prefix = f"### {title}" if heading else title + ":"
    return f"{prefix}\n{_fenced('text', sanitize(value, _FIELD_LIMIT))}"


def _inline_code(text: str) -> str:
    return text.replace("\\", "\\\\").replace("`", "\\`")


__all__ = ["AgentTranscriptScreen", "render_agent"]
