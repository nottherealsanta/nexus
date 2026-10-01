"""Reducer-backed conversation timeline and compact tool activity rows."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Click, Key
from textual.markup import escape
from textual.widget import Widget
from textual.widgets import Button, Markdown, Static

from ...ui_support.text import redact
from ...ui_support.timeline import (
    BATCH_GLYPHS,
    tool_batches,
    _agent_link_label,
    _agent_metrics,
    _has_message_content,
    _latest_activity,
    _literal,
    _message_markdown,
    _setup_failure,
    _stale_greeting,
    _task_child_activity,
    _task_child_details,
    _task_children,
    _task_header,
    _task_metrics,
    _task_phrase,
    _task_result,
    _task_short_phrase,
    _text,
    _turn_duration,
    _turn_models,
    _turn_setup_failure,
    format_arguments,
    thought_title,
    submitted_attachment_summary,
    tool_heading,
    todo_preview,
    tool_status,
    tool_summary,
)
from ...ui_support.tui_context_header import ContextHeader, ContextModal
from ...ui_support.tui_diff import ToolDiff, tool_diff_signature
from ...view import AgentView, ConversationView, MessageView, ToolCallView, TurnView
from ..cli.render import escape_controls
from .messages import AgentOpenRequested
from ...ui_support.tool_details import (
    DetailRow,
    DetailSection,
    sections_to_text,
    tool_detail_sections,
)
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
        prompt, attachments = submitted_attachment_summary(message)
        body = f"[$nx-border-strong]{chevron}[/]  {escape(_literal(prompt))}"
        if attachments and not self.collapsed:
            body += "\n\n" + "\n".join(f"  [$nx-blue]{escape(label)}[/]" for label in attachments)
            body += "\n  [$nx-muted]Click to inspect attached context[/]"
        return body

    def on_click(self, event: Click) -> None:
        if event.offset.x > 3 and submitted_attachment_summary(self._message)[1]:
            event.stop()
            self.app.push_screen(ContextModal("Attached context", self._message.text))

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
        self.batch: str | None = None

    def set_batch(self, position: str | None) -> None:
        """Join this card to the calls its model response issued together."""
        if position == self.batch:
            return
        self.batch = position
        self.set_class(position is not None, "-batched")
        if self.is_mounted:
            self._render_header()

    @property
    def _gutter(self) -> str:
        glyph = BATCH_GLYPHS.get(self.batch or "")
        return f"{glyph} " if glyph else ""

    def compose(self) -> ComposeResult:
        yield Static("", id="tool-header", markup=False)

    async def on_mount(self) -> None:
        await self.set_tool(self.tool)

    async def set_tool(self, tool: ToolCallView) -> None:
        self.tool = tool
        active = tool_status(tool) == "running"
        if active and self._spinner is None:
            self._spinner = self.set_interval(0.12, self._spin)
        elif not active and self._spinner is not None:
            self._spinner.stop()
            self._spinner = None
        self._render_header()
        self.set_class(tool_status(tool) == "failed", "-failed")
        await self._sync_diff()

    async def _sync_diff(self) -> None:
        """Show the Edit/Patch diff inline once the call has completed."""
        current = next(iter(self.query(ToolDiff)), None)
        signature = tool_diff_signature(self.tool)
        if current is not None and current.signature == signature:
            return
        if current is not None:
            await current.remove()
        if signature is not None:
            await self.mount(ToolDiff(self.tool))

    def _render_header(self) -> None:
        tool = self.tool
        marker = tool_status(tool)
        indicator = f"{_SPINNER[self._spinner_index]} " if marker == "running" else ""
        summary = ""
        if marker == "completed" and tool.display and tool.name.casefold() not in {"read", "grep", "todowrite"}:
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
        rows = todo_preview(tool)
        heading = "☐ Todo " + rows[0] if rows else tool_heading(tool)
        text = f"{self._gutter}{indicator}{heading}{suffix}"
        if rows:
            text += "".join(f"\n{self._gutter}  {row}" for row in rows[1:])
        header.styles.height = max(1, len(rows))
        header.update(text)
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

    def _detail_sections(self) -> list[DetailSection]:
        return tool_detail_sections(self.tool)

    def _details_text(self) -> str:
        return sections_to_text(self._detail_sections())

    async def open_details(self) -> None:
        title = f"{tool_heading(self.tool)} · {tool_status(self.tool)}"
        await self.app.push_screen(ToolDetailsScreen(title, self._detail_sections()))

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

    def compose(self) -> ComposeResult:
        yield Static("", id="tool-header", markup=False)
        yield Static("", id="task-metrics", markup=False)

    async def set_task(
        self, tool: ToolCallView, agents: Mapping[str, AgentView]
    ) -> None:
        self.agents = agents
        await self.set_tool(tool)

    def _render_header(self) -> None:
        tool = self.tool
        child = next(iter(self._children()), None)
        line, running = _task_header(tool, child, self._spinner_index)
        self.query_one("#tool-header", Static).update(f"{self._gutter}{line}" if self._gutter else line)
        self._sync_metrics(child, running)
        self._style_header()

    def _child_activity(self, agent: AgentView) -> str:
        return _task_child_activity(agent)

    def _task_phrase(self, agent: AgentView | None) -> str:
        return _task_phrase(self.tool, agent)

    def _task_result(self) -> str:
        return _task_result(self.tool)

    @staticmethod
    def _short_phrase(value: object) -> str:
        return _task_short_phrase(value)

    def _sync_metrics(self, child: AgentView | None, running: bool) -> None:
        metrics = self.query_one("#task-metrics", Static)
        metrics.update(_task_metrics(self.tool, child, running))
        metrics.styles.display = "block"

    async def set_tool(self, tool: ToolCallView) -> None:
        await super().set_tool(tool)
        self.add_class("task-card")

    def _children(self) -> tuple[AgentView, ...]:
        return _task_children(self.tool, self.agents)

    def _detail_sections(self) -> list[DetailSection]:
        sections = super()._detail_sections()
        rows = _task_child_details(self._children())
        if rows:
            sections.append(DetailSection(
                "Child agents",
                tuple(DetailRow(f"#{i}", row) for i, row in enumerate(rows, 1)),
            ))
        return sections

    async def open_details(self) -> None:
        # A spawned child opens straight on its sub agent page; a call that
        # never spawned one (refused, still starting) shows its details.
        child = next(iter(self._children()), None)
        if child is not None:
            self.post_message(AgentOpenRequested(child.id))
            return
        title = f"{tool_heading(self.tool)} · {tool_status(self.tool)}"
        await self.app.push_screen(ToolDetailsScreen(title, self._detail_sections()))

class AgentActivityLink(Button):
    """Focusable child projection; it holds no activity state of its own."""

    def __init__(self, agent: AgentView, **kwargs: Any) -> None:
        self.agent = agent
        super().__init__(self._label(), **kwargs)

    def _label(self) -> str:
        return _agent_link_label(self.agent)

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
        self._set_turn_lock = asyncio.Lock()
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
        async with self._set_turn_lock:
            if not self.is_attached:
                return
            await self._reconcile_turn(
                turn,
                agents,
                hide_setup_error=hide_setup_error,
                hide_greeting_key=hide_greeting_key,
            )

    async def _reconcile_turn(
        self,
        turn: TurnView,
        agents: Mapping[str, AgentView],
        *,
        hide_setup_error: bool,
        hide_greeting_key: str | None,
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
        batches = tool_batches(turn.tools)
        wanted = {key for _, key, _ in entries}
        for key, widget in tuple(self._items.items()):
            if not self.is_attached:
                return
            if key not in wanted:
                await widget.remove()
                del self._items[key]
        for _, key, value in entries:
            if not self.is_attached:
                return
            widget = self._items.get(key)
            if widget is not None and not widget.is_attached:
                del self._items[key]
                widget = None
            if isinstance(value, tuple):
                if widget is None:
                    widget = ThoughtLine(value[1], classes="timeline-thought")
                    if not await self._mount_item(widget):
                        return
                    self._items[key] = widget
                elif isinstance(widget, ThoughtLine):
                    widget.set_message(value[1])
            elif isinstance(value, MessageView):
                if widget is None:
                    widget = (
                        UserMessage(value, turn.user_ts, classes="timeline-user")
                        if value.role == "user"
                        else AssistantMessage(value, classes="timeline-assistant")
                    )
                    if not await self._mount_item(widget):
                        return
                    self._items[key] = widget
                elif isinstance(widget, (UserMessage, AssistantMessage)):
                    await widget.set_message(value) if isinstance(
                        widget, AssistantMessage
                    ) else widget.set_message(value)
            elif isinstance(value, ToolCallView):
                if widget is not None and isinstance(widget, ToolActivityWidget):
                    widget.set_batch(batches.get(value.call_id))
                if widget is None:
                    widget = (
                        TaskActivityWidget(value, agents, classes="tool-card")
                        if value.name.casefold() in {"task", "subagent"}
                        else ToolActivityWidget(value, classes="tool-card")
                    )
                    widget.set_batch(batches.get(value.call_id))
                    if not await self._mount_item(widget):
                        return
                    self._items[key] = widget
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
                    if not await self._mount_item(widget):
                        return
                    self._items[key] = widget
                elif isinstance(widget, Static):
                    widget.update(f"Error: {_text(value)}")

        # The first visible activity, whether thought, tool or reply, gets a
        # single line after the prompt. Subsequent tool rows stay compact.
        for index, (_, key, _) in enumerate(entries):
            previous = entries[index - 1][2] if index else None
            item = self._items[key]
            item.set_class(
                not self.collapsed
                and isinstance(previous, MessageView)
                and previous.role == "user",
                "timeline-after-user",
            )
            after_tool = not self.collapsed and isinstance(previous, ToolCallView)
            item.set_class(after_tool, "timeline-after-tool")
            item.set_class(
                after_tool and tool_diff_signature(previous) is not None,
                "timeline-after-tool-diff",
            )

        for item in self._items.values():
            if isinstance(item, UserMessage):
                item.set_collapsed(self.collapsed)
        if self.collapsed:
            reply = next((message.text.splitlines()[0] for message in turn.messages if message.role == "assistant" and message.text), "")
            metrics = " · ".join(part for part in (str(turn.usage.total_tokens) + " tokens" if turn.usage.total_tokens else "", _turn_duration(turn) or "") if part)
            collapsed = f"[$nx-muted]{len(turn.tools)} tools · {escape(_text(reply, 120))}[/]  [$nx-quiet]{metrics}[/]"
            if self._collapsed_summary is None:
                summary = Static(collapsed, classes="turn-collapsed")
                if not await self._mount_item(summary):
                    return
                self._collapsed_summary = summary
            else:
                self._collapsed_summary.update(collapsed)
        elif self._collapsed_summary is not None:
            await self._collapsed_summary.remove()
            self._collapsed_summary = None
        if turn.terminal and not self.collapsed:
            summary = _turn_footer(turn)
            if self._summary_widget is None:
                summary_widget = Static(summary, classes="timeline-summary")
                if not await self._mount_item(summary_widget):
                    return
                self._summary_widget = summary_widget
            else:
                self._summary_widget.update(summary)
            self._summary_widget.set_class(
                bool(entries and isinstance(entries[-1][2], MessageView) and entries[-1][2].role == "assistant"),
                "timeline-summary-after-assistant",
            )
            last_value = entries[-1][2] if entries else None
            self._summary_widget.set_class(
                isinstance(last_value, ToolCallView)
                and tool_diff_signature(last_value) is not None,
                "timeline-summary-after-tool-diff",
            )
        elif self._summary_widget is not None:
            await self._summary_widget.remove()
            self._summary_widget = None

    async def _mount_item(self, widget: Widget) -> bool:
        if not self.is_attached:
            return False
        try:
            await self.mount(widget)
        except asyncio.CancelledError:
            if widget.is_attached:
                await widget.remove()
            raise
        if not self.is_attached:
            if widget.is_attached:
                await widget.remove()
            return False
        return True

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
        self._set_view_lock = asyncio.Lock()
        self.agent_colors: dict[str, str] = {}
        self._header = header

    def compose(self) -> ComposeResult:
        if self._header:
            yield ContextHeader(id="context-header")

    @property
    def at_bottom(self) -> bool:
        return self.scroll_offset.y >= self.max_scroll_y - 1

    async def set_view(self, view: ConversationView) -> None:
        async with self._set_view_lock:
            if self.is_attached:
                await self._reconcile_view(view)

    async def _reconcile_view(self, view: ConversationView) -> None:
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
            if not self.is_attached:
                return
            if turn_id not in wanted:
                await widget.remove()
                del self._turns[turn_id]
        for turn in view.turns:
            if not self.is_attached:
                return
            widget = self._turns.get(turn.id)
            if widget is not None and not widget.is_attached:
                del self._turns[turn.id]
                widget = None
            if widget is None:
                widget = TurnWidget(turn, view.agents, classes="turn")
                try:
                    await self.mount(widget)
                except asyncio.CancelledError:
                    if widget.is_attached:
                        await widget.remove()
                    raise
                if not self.is_attached:
                    if widget.is_attached:
                        await widget.remove()
                    return
                self._turns[turn.id] = widget
            if not widget.is_attached:
                del self._turns[turn.id]
                continue
            widget.agent_colors = self.agent_colors
            await widget.set_turn(
                turn,
                view.agents,
                hide_setup_error=turn.id in hidden_errors,
                hide_greeting_key=initial_greeting_key,
            )
            if not self.is_attached:
                return
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False)

__all__ = [
    "ConversationTimeline",
    "TaskActivityWidget",
    "ThoughtLine",
    "ToolActivityWidget",
    "TurnWidget",
    "_agent_metrics",
    "_latest_activity",
    "format_arguments",
    "tool_summary",
]
