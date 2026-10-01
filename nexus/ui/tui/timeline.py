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

from ...ui_support.context import _compact_tokens
from ...ui_support.hints import pick_hints
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
    running_output_tail,
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
    """Right-aligned stats for a completed turn: model, elapsed time, tokens
    in/out, cache share, and reasoning the provider did not show. The agent
    labels the reply itself."""
    model = _turn_models(turn).split(", ")[0].rsplit("/", 1)[-1]
    usage = turn.usage
    prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    tokens = f"↑{_compact_tokens(prompt)} ↓{_compact_tokens(usage.output_tokens)}" if prompt or usage.output_tokens else ""
    cached = f"{round(usage.cache_read_tokens / prompt * 100)}% cached" if prompt and usage.cache_read_tokens else ""
    shown = any(block.kind == "thinking" and block.text.strip() for message in turn.messages for block in message.blocks)
    reasoning = (f"{_compact_tokens(usage.reasoning_tokens)} reasoning" + ("" if shown else " (not shown)")
                 if usage.reasoning_tokens else "")
    parts = [
        escape(part)
        for part in (model if model != "unknown" else "", _turn_duration(turn) or "", tokens, cached, reasoning)
        if part
    ]
    return f"[$nx-quiet]{' · '.join(parts)}[/]" if parts else ""


def _agent_label(turn: TurnView, colors: Mapping[str, str]) -> str:
    """``◆ Build`` above the turn's reply, in the agent's color."""
    agent = turn.agent if isinstance(turn.agent, Mapping) else {}
    name = agent.get("name") if isinstance(agent.get("name"), str) else ""
    if not name.strip():
        return ""
    color = colors.get(name.casefold()) or (agent.get("color") if isinstance(agent.get("color"), str) else "") or _FALLBACK_AGENT_COLOR
    label = name.strip()[0].upper() + name.strip()[1:]
    return f"[{color}]◆[/] [bold {color}]{escape(_literal(label, 60))}[/]"


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
    """The prompt block: chevron and literal prompt, the turn number
    right-aligned and highlighted on the first row, and attachments as
    highlighted chips. No other metadata sits inside the body."""

    def __init__(self, message: MessageView, ts: float | None = None, *, number: int = 0, **kwargs: Any) -> None:
        self.message_id = message.id
        self._message = message
        self._ts = message.ts or ts
        self.collapsed = False
        #: 1-based turn number shown at the right of the first row (0 hides it).
        self.number = number
        super().__init__(self._content(message), **kwargs)

    def _content(self, message: MessageView, width: int | None = None) -> str:
        chevron = "▶" if self.collapsed else "▼"
        prompt, attachments = submitted_attachment_summary(message)
        text = _literal(prompt)
        tag = f" #{self.number} " if self.number else ""
        first, _, rest = text.partition("\n")
        if self.collapsed and rest:
            first, rest = first + " …", ""
        if tag and width:
            room = max(8, width - 3 - len(tag) - 1)
            if len(first) > room:
                cut = first.rfind(" ", 0, room + 1)
                cut = cut if cut > room // 2 else room
                first, rest = first[:cut].rstrip(), first[cut:].lstrip() + ("\n" + rest if rest else "")
            first = first.ljust(room)
        body = f"[$nx-border-strong]{chevron}[/]  {escape(first)}"
        if tag:
            body += f" [bold $nx-accent on $nx-element-hi]{tag}[/]"
        if rest:
            body += "\n" + escape(rest)
        if attachments and not self.collapsed:
            chips = "  ".join(f"[bold $nx-blue on $nx-element-hi] ▣ {escape(label)} [/]" for label in attachments)
            body += f"\n\n   {chips}\n   [$nx-quiet]Click to inspect attached context[/]"
        return body

    def _refresh(self) -> None:
        width = self.content_size.width if self.is_mounted and self.content_size.width else None
        self.update(self._content(self._message, width))

    def on_resize(self, _event: object) -> None:
        self._refresh()

    def on_click(self, event: Click) -> None:
        if event.offset.x > 3 and submitted_attachment_summary(self._message)[1]:
            event.stop()
            self.app.push_screen(ContextModal("Attached context", self._message.text))

    def set_message(self, message: MessageView) -> None:
        self._message = message
        self._ts = message.ts or self._ts
        self._refresh()

    def set_number(self, number: int) -> None:
        if number != self.number:
            self.number = number
            self._refresh()

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self._refresh()


class ThoughtLine(Static):
    """Provider thinking as one headline row: ``◇ first sentence ▸``.

    Enter or click shows the full text. A provider that reasons without
    sharing the text (an empty, signed thinking block) gets a labelled row
    saying so rather than nothing.
    """

    can_focus = True

    def __init__(self, message: MessageView, **kwargs: Any) -> None:
        self.message = message
        self.expanded = False
        super().__init__("", **kwargs)
        self.set_message(message)

    def set_message(self, message: MessageView) -> None:
        self.message = message
        text = message.thinking
        if not text.strip():
            self.set_class(True, "-hidden")
            self.update("[$nx-purple]◇[/] [i]Thought[/]  [$nx-quiet]· not shared by the provider[/]")
            return
        self.set_class(False, "-hidden")
        lines = len([line for line in text.splitlines() if line.strip()])
        more = "▾" if self.expanded else "▸"
        head = f"[$nx-purple]◇[/] [i]{escape(thought_title(text))}[/]  [$nx-quiet]{more}[/]"
        if not self.expanded and lines > 1:
            head = head[:-len(f"[$nx-quiet]{more}[/]")] + f"[$nx-quiet]{lines} lines {more}[/]"
        if self.expanded:
            head += f"\n\n[$nx-muted]{escape(_literal(text))}[/]"
        self.update(head)

    def on_click(self, event: Click) -> None:
        event.stop()
        if not self.message.thinking.strip():
            return
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
        live = running_output_tail(tool) if marker == "running" else None
        if live is not None:
            # A running shell shows its latest output under the call (⎿), with
            # the lines above it counted, until it completes.
            tail, hidden = live
            text += f"\n{self._gutter}  ⎿  " + (tail[0] if tail else "running…")
            text += "".join(f"\n{self._gutter}     {line}" for line in tail[1:])
            if hidden:
                text += f"\n{self._gutter}     … {hidden} earlier line{'s' if hidden != 1 else ''} · enter for full output"
            rows = [""] * (1 + max(1, len(tail)) + (1 if hidden else 0))
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
        #: 1-based position in the conversation, shown on the prompt.
        self.number = 0

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
            if message.role == "assistant" and any(block.kind == "thinking" for block in message.blocks):
                entries.append((message.event_seq, f"message-thought:{message.id or index}", ("thought", message)))
            if message.text if message.role == "assistant" else _has_message_content(message):
                entries.append((message.event_seq, f"message:{message.id or index}", message))
        first_reply = next((entry for entry in entries if isinstance(entry[2], MessageView) and entry[2].role == "assistant"), None)
        label = _agent_label(turn, self.agent_colors)
        if first_reply is not None and label:
            entries.append((first_reply[0], "message-agent-label", ("label", label)))
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
        # Same event: thought, then the agent label, then the reply, then tools.
        rank = {"message-thought": 0, "message-agent-label": 1}
        entries.sort(
            key=lambda entry: (
                entry[0],
                rank.get(entry[1].split(":")[0], 2) if entry[1].startswith("message") else 3,
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
            if isinstance(value, tuple) and value[0] == "label":
                if widget is None:
                    widget = Static(value[1], classes="timeline-agent")
                    if not await self._mount_item(widget):
                        return
                    self._items[key] = widget
                elif isinstance(widget, Static):
                    widget.update(value[1])
            elif isinstance(value, tuple):
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
                        UserMessage(value, turn.user_ts, number=self.number, classes="timeline-user")
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
                item.set_number(self.number)
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


class EmptyHints(Static):
    """Grey tips in the middle of an empty session; gone once you type."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.seed: object = None
        self.empty = True
        self.typing = False

    def set_state(self, *, seed: object = None, empty: bool | None = None, typing: bool | None = None) -> None:
        if seed is not None and seed != self.seed:
            self.seed = seed
            rows = pick_hints(seed)
            width = max(len(keys) for keys, _ in rows)
            # Equal-width lines keep the two columns aligned once centred.
            tail = max(len(text) for _, text in rows)
            self.update("\n".join(f"[bold $nx-muted]{escape(keys.rjust(width))}[/]  {escape(text.ljust(tail))}" for keys, text in rows))
        if empty is not None:
            self.empty = empty
        if typing is not None:
            self.typing = typing
        # Typing only hides the text (no layout jump); turns remove it.
        self.display = self.empty
        self.visible = not self.typing


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
            yield EmptyHints(id="empty-hints")

    def set_typing(self, typing: bool) -> None:
        for hints in self.query(EmptyHints):
            hints.set_state(typing=typing)

    @property
    def at_bottom(self) -> bool:
        return self.scroll_offset.y >= self.max_scroll_y - 1

    async def set_view(self, view: ConversationView) -> None:
        async with self._set_view_lock:
            if self.is_attached:
                await self._reconcile_view(view)

    async def _reconcile_view(self, view: ConversationView) -> None:
        follow = self.at_bottom
        for hints in self.query(EmptyHints):
            hints.set_state(seed=view.session_id or "session", empty=not view.turns)
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
        for number, turn in enumerate(view.turns, 1):
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
            widget.number = number
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
    "EmptyHints",
    "TaskActivityWidget",
    "ThoughtLine",
    "ToolActivityWidget",
    "TurnWidget",
    "_agent_metrics",
    "_latest_activity",
    "format_arguments",
    "tool_summary",
]
