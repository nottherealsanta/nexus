"""Reducer-backed conversation timeline and compact tool activity rows."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Click, Key
from textual.markup import escape
from textual.widget import Widget
from textual.widgets import Button, Markdown, Static

from ...ui_support.text import redact
from ...ui_support.timeline import (
    _agent_metrics,
    _has_message_content,
    _latest_activity,
    _literal,
    _message_markdown,
    _setup_failure,
    _stale_greeting,
    _text,
    _turn_duration,
    _turn_models,
    _turn_setup_failure,
    format_arguments,
    thought_title,
    tool_heading,
    tool_output,
    tool_status,
    tool_summary,
)
from ...ui_support.tui_context_header import ContextHeader
from ...view import AgentView, ConversationView, MessageView, ToolCallView, TurnView
from ..cli.render import escape_controls
from .messages import AgentOpenRequested
from .tool_details import ToolDetailsScreen

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FALLBACK_AGENT_COLOR = "$nx-blue"


def _agent_key(turn: TurnView) -> str:
    agent = turn.agent if isinstance(turn.agent, Mapping) else {}
    name = agent.get("name")
    return name.casefold() if isinstance(name, str) else ""


def _turn_footer(turn: TurnView) -> str:
    """Model and elapsed time for a completed turn (the agent shows elsewhere)."""
    model = _turn_models(turn).split(", ")[0].rsplit("/", 1)[-1]
    parts = [
        escape(part)
        for part in (model if model != "unknown" else "", _turn_duration(turn) or "")
        if part
    ]
    return f"[$nx-quiet]{' · '.join(parts)}[/]" if parts else ""
class AssistantMessage(Markdown):
    """One stable streamed Markdown message, updating only its appended suffix."""

    def __init__(self, message: MessageView, **kwargs: Any) -> None:
        self.message_id = message.id
        self._rendered = escape_controls(_message_markdown(message))
        self._stream = None
        self._done = message.done
        super().__init__(self._rendered, open_links=False, **kwargs)

    async def set_message(self, message: MessageView) -> None:
        text = escape_controls(_message_markdown(message))
        if text == self._rendered:
            self._done = message.done
            if self._done:
                await self._stop_stream()
            return
        if (
            self.is_mounted
            and text.startswith(self._rendered)
            and self._stream is not None
        ):
            try:
                await self._stream.write(text[len(self._rendered) :])
            except RuntimeError:
                # Streaming is an optimization; a full parse is the fallback.
                await self.update(text)
                await self._stop_stream()
        else:
            restart_stream = self.is_mounted and not message.done
            await self._stop_stream()
            await self.update(text)
            if restart_stream:
                self._stream = Markdown.get_stream(self)
        self._rendered = text
        self._done = message.done
        if self._done:
            await self._stop_stream()

    def on_mount(self) -> None:
        # Markdown's stream parser keeps delta rendering cheap while preserving
        # regular Markdown semantics for the finished message.
        if not self._done:
            self._stream = Markdown.get_stream(self)

    async def _stop_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            await stream.stop()

    async def on_unmount(self) -> None:
        await self._stop_stream()


class UserMessage(Static):
    """The prompt block, with no metadata inside the body."""

    def __init__(self, message: MessageView, ts: float | None = None, **kwargs: Any) -> None:
        self.message_id = message.id
        self._message = message
        self._ts = message.ts or ts
        self.collapsed = False
        super().__init__(self._content(message), **kwargs)

    def _content(self, message: MessageView) -> str:
        chevron = "▶" if self.collapsed else "▼"
        return f"[$nx-border-strong]{chevron}[/]  {escape(_literal(message.text))}"

    def set_message(self, message: MessageView) -> None:
        self._message = message
        self._ts = message.ts or self._ts
        self.update(self._content(message))

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.update(self._content(self._message))


class ThoughtLine(Static):
    """Provider thinking collapsed to ``Thought: title``; Enter or click expands."""

    can_focus = True

    def __init__(self, message: MessageView, **kwargs: Any) -> None:
        self.message = message
        self.expanded = False
        super().__init__("", **kwargs)
        self.set_message(message)

    def set_message(self, message: MessageView) -> None:
        self.message = message
        text = message.thinking
        head = f"Thought: {escape(thought_title(text))}"
        if self.expanded:
            head += f"\n\n[$nx-muted]{escape(_literal(text))}[/]"
        self.update(head)

    def on_click(self, event: Click) -> None:
        event.stop()
        self.expanded = not self.expanded
        self.set_message(self.message)

    def on_key(self, event: Key) -> None:
        if event.key in {"enter", "space"}:
            event.stop()
            self.on_click(event)  # type: ignore[arg-type]


class ToolActivityWidget(Widget):
    """Focusable compact card for a stable ToolCallView call id."""

    can_focus = True

    def __init__(self, tool: ToolCallView, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.call_id = tool.call_id
        self.tool = tool
        self._spinner_index = 0
        self._spinner = None

    def compose(self) -> ComposeResult:
        yield Static("", id="tool-header", markup=False)

    async def on_mount(self) -> None:
        await self.set_tool(self.tool)

    async def set_tool(self, tool: ToolCallView) -> None:
        self.tool = tool
        active = tool_status(tool) == "running"
        if active and self._spinner is None:
            self._spinner = self.set_interval(0.4, self._spin)
        elif not active and self._spinner is not None:
            self._spinner.stop()
            self._spinner = None
        self._render_header()
        self.set_class(tool_status(tool) == "failed", "-failed")

    def _render_header(self) -> None:
        tool = self.tool
        marker = tool_status(tool)
        indicator = f"{_SPINNER[self._spinner_index]} " if marker == "running" else ""
        summary = ""
        if marker == "completed" and tool.display:
            summary = _text(tool.display.splitlines()[0], 88)
            if tool.name.casefold() == "write":
                content = tool.input.get("content") if isinstance(tool.input, Mapping) else None
                lines = content.count("\n") + 1 if isinstance(content, str) and content else 0
                summary = f"written · {lines} lines" if lines else "written"
        elif marker == "failed" and tool.error:
            summary = _text(tool.error.splitlines()[0], 88)
        if summary.casefold().startswith(f"{tool.name.casefold()}:"):
            summary = summary[len(tool.name) + 1 :].strip()
        summary = redact(summary)
        suffix = f" · {summary}" if summary else (f" · {marker}" if marker != "completed" else "")
        header = self.query_one("#tool-header", Static)
        header.update(f"{indicator}{tool_heading(tool)}{suffix}")
        self._style_header()

    def _style_header(self) -> None:
        header = self.query_one("#tool-header", Static)
        header.styles.text_overflow = "ellipsis"
        header.styles.text_wrap = "nowrap"
        header.styles.overflow_x = "hidden"

    def _spin(self) -> None:
        if tool_status(self.tool) != "running":
            return
        self._spinner_index = (self._spinner_index + 1) % len(_SPINNER)
        self._render_header()

    def on_unmount(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None

    def _details_text(self) -> str:
        tool = self.tool
        sections = [f"Status: {tool_status(tool)}"]
        if tool.duration_ms is not None:
            sections.append(f"Duration: {tool.duration_ms} ms")
        if tool.input:
            payload = json.dumps(tool.input, ensure_ascii=True, indent=2, default=str)
            sections.append("Call parameters:\n" + redact(escape_controls(payload))[:8192])
        if tool.progress:
            progress = "\n".join(redact(_literal(item, 300)) for item in tool.progress)
            sections.append("Progress:\n" + progress)
        if tool.display:
            sections.append("Summary:\n" + redact(_literal(tool.display, 8192)))
        if tool.result:
            result = json.dumps(tool.result, ensure_ascii=True, indent=2, default=str)
            sections.append("Result:\n" + redact(escape_controls(result))[:8192])
        if tool.error:
            sections.append("Error:\n" + tool_output(tool))
        if tool.context_note:
            sections.append("Context:\n" + redact(_literal(tool.context_note, 400)))
        if isinstance(tool.diff, Mapping):
            path = _text(tool.diff.get("path") or "edit", 160)
            hunk = tool.diff.get("hunk")
            diff = f"{path}: +{tool.diff.get('added_lines', 0)} -{tool.diff.get('removed_lines', 0)}"
            if tool.diff.get("truncated"):
                diff += " (preview truncated)"
            if isinstance(hunk, str) and hunk:
                diff += "\n" + redact(_literal(hunk, 8192))
            sections.append("Diff:\n" + diff)
        text = "\n\n".join(sections)
        return text if len(text) <= 32_000 else text[:32_000] + "\n[Details clipped]"

    async def open_details(self) -> None:
        title = f"{tool_heading(self.tool)} · {tool_status(self.tool)}"
        await self.app.push_screen(ToolDetailsScreen(title, self._details_text()))

    async def on_click(self, event: Click) -> None:
        event.stop()
        await self.open_details()

    async def on_key(self, event: Key) -> None:
        if event.key in {"enter", "space"}:
            event.stop()
            await self.open_details()


class TaskActivityWidget(ToolActivityWidget):
    """A Task card projected solely from its call and linked AgentView children."""

    def __init__(
        self, tool: ToolCallView, agents: Mapping[str, AgentView], **kwargs: Any
    ) -> None:
        super().__init__(tool, **kwargs)
        self.agents = agents

    async def set_task(
        self, tool: ToolCallView, agents: Mapping[str, AgentView]
    ) -> None:
        self.agents = agents
        await self.set_tool(tool)

    def _render_header(self) -> None:
        tool = self.tool
        marker = tool_status(tool)
        mark = {"completed": "✓", "failed": "✗"}.get(marker, _SPINNER[self._spinner_index])
        child = next(iter(self._children()), None)
        kind = (child.type if child and child.type else tool.input.get("subagent_type") if isinstance(tool.input, Mapping) else None) or "General"
        description = format_arguments(tool) or "Task"
        self.query_one("#tool-header", Static).update(f"{mark} {_text(str(kind), 32).title()} Task — {description}")
        self._style_header()

    async def set_tool(self, tool: ToolCallView) -> None:
        await super().set_tool(tool)
        self.add_class("task-card")

    def _children(self) -> Iterable[AgentView]:
        return (
            self.agents[agent_id]
            for agent_id in self.tool.child_agent_ids
            if agent_id in self.agents
        )

    def _details_text(self) -> str:
        details = super()._details_text()
        rows = []
        for agent in self._children():
            rows.append(
                f"{_text(agent.type or agent.id, 48)} · {_text(agent.task or agent.description, 96)}"
                f" · {_text(agent.status, 24)} · {_agent_metrics(agent)}\n  {_latest_activity(agent)}"
            )
        return details + ("\n\nChild agents:\n" + "\n".join(rows) if rows else "")

    async def open_details(self) -> None:
        title = f"{tool_heading(self.tool)} · {tool_status(self.tool)}"
        child = next(iter(self._children()), None)
        await self.app.push_screen(
            ToolDetailsScreen(
                title, self._details_text(), agent_id=child.id if child else None
            )
        )

class AgentActivityLink(Button):
    """Focusable child projection; it holds no activity state of its own."""

    def __init__(self, agent: AgentView, **kwargs: Any) -> None:
        self.agent = agent
        super().__init__(self._label(), **kwargs)

    def _label(self) -> str:
        activity = _latest_activity(self.agent)
        tool = next(
            (
                tool
                for turn in reversed(self.agent.body.turns)
                for tool in reversed(turn.tools)
            ),
            None,
        )
        if tool is not None:
            status = tool_status(tool)
            activity += f" · {status}"
        return (
            f"{_text(self.agent.type or self.agent.id, 36)} · "
            f"{_text(self.agent.status, 18)} · {_agent_metrics(self.agent)} · {activity}"
        )

    def set_agent(self, agent: AgentView) -> None:
        self.agent = agent
        self.label = self._label()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button is self:
            self.post_message(AgentOpenRequested(self.agent.id))


class TurnWidget(Widget):
    """One turn reconciled by durable message and call identifiers."""

    def __init__(
        self, turn: TurnView, agents: Mapping[str, AgentView], **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.turn_id = turn.id
        self._items: dict[str, Widget] = {}
        self.turn = turn
        self.agents = agents
        self._summary_widget: Static | None = None
        self._collapsed_summary: Static | None = None
        self.collapsed = False
        #: Host-reported agent colors by lowercase name (set by the timeline).
        self.agent_colors: Mapping[str, str] = {}

    def render(self) -> str:
        """Never let Textual's default childless-widget label reach the screen."""
        return ""

    async def on_mount(self) -> None:
        await self.set_turn(self.turn, self.agents)

    async def set_turn(
        self,
        turn: TurnView,
        agents: Mapping[str, AgentView],
        *,
        hide_setup_error: bool = False,
        hide_greeting_key: str | None = None,
    ) -> None:
        self.turn = turn
        self.agents = agents
        if turn.phase == "active" and self.collapsed:
            self.collapsed = False
        entries: list[tuple[int, str, object]] = []
        for index, message in enumerate(turn.messages):
            if f"{self.turn_id}:message:{message.id or index}" == hide_greeting_key:
                continue
            if message.role == "assistant" and message.thinking:
                entries.append((message.event_seq, f"message-thought:{message.id or index}", ("thought", message)))
            if message.text if message.role == "assistant" else _has_message_content(message):
                entries.append((message.event_seq, f"message:{message.id or index}", message))
        entries.extend(
            (tool.event_seq, f"tool:{tool.call_id}", tool) for tool in turn.tools
        )
        if hide_setup_error:
            entries = [
                entry
                for entry in entries
                if not (
                    isinstance(entry[2], ToolCallView)
                    and _setup_failure(entry[2].error)
                )
            ]
        if turn.terminal and turn.error and not hide_setup_error:
            entries.append(
                (
                    max((seq for seq, _, _ in entries), default=0) + 1,
                    "turn-error",
                    turn.error,
                )
            )
        entries.sort(
            key=lambda entry: (
                entry[0],
                0 if entry[1].startswith("message") else 1,
                entry[1],
            )
        )
        if self.collapsed:
            entries = [entry for entry in entries if isinstance(entry[2], MessageView) and entry[2].role == "user"]
        wanted = {key for _, key, _ in entries}
        for key, widget in tuple(self._items.items()):
            if key not in wanted:
                await widget.remove()
                del self._items[key]
        for _, key, value in entries:
            widget = self._items.get(key)
            if isinstance(value, tuple):
                if widget is None:
                    widget = ThoughtLine(value[1], classes="timeline-thought")
                    self._items[key] = widget
                    await self.mount(widget)
                elif isinstance(widget, ThoughtLine):
                    widget.set_message(value[1])
            elif isinstance(value, MessageView):
                if widget is None:
                    widget = (
                        UserMessage(value, turn.user_ts, classes="timeline-user")
                        if value.role == "user"
                        else AssistantMessage(value, classes="timeline-assistant")
                    )
                    self._items[key] = widget
                    await self.mount(widget)
                elif isinstance(widget, (UserMessage, AssistantMessage)):
                    await widget.set_message(value) if isinstance(
                        widget, AssistantMessage
                    ) else widget.set_message(value)
            elif isinstance(value, ToolCallView):
                if widget is None:
                    widget = (
                        TaskActivityWidget(value, agents, classes="tool-card")
                        if value.name.casefold() == "task"
                        else ToolActivityWidget(value, classes="tool-card")
                    )
                    self._items[key] = widget
                    await self.mount(widget)
                    if isinstance(widget, TaskActivityWidget):
                        await widget.set_task(value, agents)
                elif isinstance(widget, TaskActivityWidget):
                    await widget.set_task(value, agents)
                elif isinstance(widget, ToolActivityWidget):
                    await widget.set_tool(value)
            elif isinstance(value, str):
                if widget is None:
                    widget = Static(
                        f"Error: {_text(value)}", markup=False, classes="timeline-error"
                    )
                    self._items[key] = widget
                    await self.mount(widget)
                elif isinstance(widget, Static):
                    widget.update(f"Error: {_text(value)}")

        for item in self._items.values():
            if isinstance(item, UserMessage):
                item.set_collapsed(self.collapsed)
        if self.collapsed:
            reply = next((message.text.splitlines()[0] for message in turn.messages if message.role == "assistant" and message.text), "")
            metrics = " · ".join(part for part in (str(turn.usage.total_tokens) + " tokens" if turn.usage.total_tokens else "", _turn_duration(turn) or "") if part)
            collapsed = f"[$nx-muted]{len(turn.tools)} tools · {escape(_text(reply, 120))}[/]  [$nx-quiet]{metrics}[/]"
            if self._collapsed_summary is None:
                self._collapsed_summary = Static(collapsed, classes="turn-collapsed")
                await self.mount(self._collapsed_summary)
            else:
                self._collapsed_summary.update(collapsed)
        elif self._collapsed_summary is not None:
            await self._collapsed_summary.remove()
            self._collapsed_summary = None
        if turn.terminal and not self.collapsed:
            summary = _turn_footer(turn)
            if self._summary_widget is None:
                self._summary_widget = Static(summary, classes="timeline-summary")
                await self.mount(self._summary_widget)
            else:
                self._summary_widget.update(summary)
        elif self._summary_widget is not None:
            await self._summary_widget.remove()
            self._summary_widget = None

    async def on_click(self, event: Click) -> None:
        if not isinstance(event.widget, UserMessage) or event.offset.x > 3:
            return
        event.stop()
        if self.turn.phase == "active":
            return
        self.collapsed = not self.collapsed
        await self.set_turn(self.turn, self.agents)


class ConversationTimeline(VerticalScroll):
    """Single-column reducer projection with tail-follow only when pinned."""

    can_focus = True

    def __init__(self, *, header: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._turns: dict[str, TurnWidget] = {}
        self.agent_colors: dict[str, str] = {}
        self._header = header

    def compose(self) -> ComposeResult:
        if self._header:
            yield ContextHeader(id="context-header")

    @property
    def at_bottom(self) -> bool:
        return self.scroll_offset.y >= self.max_scroll_y - 1

    async def set_view(self, view: ConversationView) -> None:
        follow = self.at_bottom
        first_user_seq = min(
            (
                message.event_seq
                for turn in view.turns
                for message in turn.messages
                if message.role == "user"
            ),
            default=0,
        )
        initial_greeting_key: str | None = None
        if first_user_seq:
            for turn in view.turns:
                for index, message in enumerate(turn.messages):
                    if (
                        message.event_seq < first_user_seq
                        and _stale_greeting(message)
                    ):
                        initial_greeting_key = (
                            f"{turn.id}:message:{message.id or index}"
                        )
                        break
                if initial_greeting_key is not None:
                    break

        later_success = False
        hidden_errors: set[str] = set()
        for turn in reversed(view.turns):
            if later_success and turn.phase == "failed" and _turn_setup_failure(turn):
                hidden_errors.add(turn.id)
            if turn.phase == "completed":
                later_success = True
        wanted = {turn.id for turn in view.turns}
        for turn_id, widget in tuple(self._turns.items()):
            if turn_id not in wanted:
                await widget.remove()
                del self._turns[turn_id]
        for turn in view.turns:
            widget = self._turns.get(turn.id)
            if widget is None:
                widget = TurnWidget(turn, view.agents, classes="turn")
                self._turns[turn.id] = widget
                await self.mount(widget)
            widget.agent_colors = self.agent_colors
            await widget.set_turn(
                turn,
                view.agents,
                hide_setup_error=turn.id in hidden_errors,
                hide_greeting_key=initial_greeting_key,
            )
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False)

__all__ = [
    "ConversationTimeline",
    "TaskActivityWidget",
    "ThoughtLine",
    "ToolActivityWidget",
    "TurnWidget",
    "format_arguments",
    "tool_summary",
]
