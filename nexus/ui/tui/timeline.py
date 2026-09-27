"""Reducer-backed conversation timeline and inline tool activity cards."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Click, Key
from textual.widget import Widget
from textual.widgets import Button, Markdown, Static

from ...view import AgentView, ConversationView, MessageView, ToolCallView, TurnView
from ...ui_support.timeline import (
    _DETAIL_LIMIT,
    _agent_metrics,
    _diff_text,
    _has_message_content,
    _latest_activity,
    _literal,
    _message_markdown,
    _output,
    _setup_failure,
    _stale_greeting,
    _text,
    _turn_setup_failure,
    _turn_summary,
    format_arguments,
    tool_status,
    tool_summary,
)
from ..cli.render import escape_controls, sanitize
from .messages import AgentOpenRequested

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
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
    def __init__(self, message: MessageView, **kwargs: Any) -> None:
        self.message_id = message.id
        super().__init__(_literal(message.text), markup=False, **kwargs)

    def set_message(self, message: MessageView) -> None:
        self.update(_literal(message.text))


class ToolActivityWidget(Widget):
    """Focusable compact card for a stable ToolCallView call id."""

    can_focus = True

    def __init__(self, tool: ToolCallView, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.call_id = tool.call_id
        self.tool = tool
        self.expanded = False
        self._diff_signature: tuple[object, ...] | None = None
        self._diff_widget: Widget | None = None
        self._spinner_index = 0
        self._spinner = None

    def compose(self) -> ComposeResult:
        yield Static("", id="tool-header", markup=False)
        yield Static("", id="tool-detail", markup=False)
        yield Static("", id="tool-expanded", markup=False)

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
        self.query_one("#tool-detail", Static).update(tool_summary(tool))
        self.query_one("#tool-expanded", Static).update(
            self._expanded_text() if self.expanded else ""
        )
        await self._sync_diff()

    def _render_header(self) -> None:
        tool = self.tool
        name = _text(tool.name or "tool", 80)
        args = format_arguments(tool)
        duration = f" {tool.duration_ms}ms" if isinstance(tool.duration_ms, int) else ""
        marker = tool_status(tool)
        active = marker == "running"
        indicator = f"{_SPINNER[self._spinner_index]} " if active else ""
        self.query_one("#tool-header", Static).update(
            f"{indicator}{name}"
            + (f"  {args}" if args else "")
            + f"  [{marker}{duration}]"
        )

    def _spin(self) -> None:
        if tool_status(self.tool) != "running":
            return
        self._spinner_index = (self._spinner_index + 1) % len(_SPINNER)
        self._render_header()

    def on_unmount(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None

    def _expanded_text(self) -> str:
        tool = self.tool
        lines: list[str] = []
        if isinstance(tool.diff, Mapping):
            path = _text(tool.diff.get("path") or "edit", 160)
            added = tool.diff.get("added_lines", 0)
            removed = tool.diff.get("removed_lines", 0)
            suffix = " (preview truncated)" if tool.diff.get("truncated") else ""
            lines.append(f"{path}: +{added} -{removed}{suffix}")
            hunk = tool.diff.get("hunk")
            if isinstance(hunk, str):
                lines.append(_literal(hunk, _DETAIL_LIMIT))
        if tool.progress:
            lines.extend(_literal(item, 300) for item in tool.progress[-6:])
        # Write content is intentionally never replayed into a transcript. Its
        # canonical output is already the compact status shown on the card.
        value = "" if tool.name.casefold() == "write" else _output(tool)
        if value:
            lines.append(value)
        if tool.error:
            lines.append("error: " + _text(tool.error, 600))
        if tool.context_note:
            lines.append("context: " + _literal(tool.context_note, 400))
        return "\n".join(lines)[:_DETAIL_LIMIT]

    async def _sync_diff(self) -> None:
        """Mount the optional dependency only for valid, wide Edit artifacts."""
        diff = (
            self.tool.diff
            if self.tool.name.casefold() in {"edit", "multiedit"}
            else None
        )
        if not isinstance(diff, Mapping) or self.size.width < 72:
            await self._clear_diff()
            return
        text = _diff_text(diff)
        signature = (diff.get("path"), diff.get("hunk"), diff.get("truncated"))
        if text is None or signature == self._diff_signature:
            return
        self._diff_signature = signature
        try:
            from textual_diff_view import DiffView

            path = _text(diff.get("path") or "edit", 160)
            if self._diff_widget is not None:
                await self._diff_widget.remove()
            view = DiffView(
                path, path, text[0], text[1], split=True, annotations=True, wrap=True
            )
            view.add_class("tool-diff-view")
            await view.prepare()
            await self.mount(view)
            self._diff_widget = view
        except Exception:
            # The durable unified hunk remains visible in the expanded fallback.
            self.query_one("#tool-expanded", Static).update(self._expanded_text())

    async def _clear_diff(self) -> None:
        """Return to the durable text fallback when a diff preview cannot fit."""
        if self._diff_widget is not None:
            await self._diff_widget.remove()
            self._diff_widget = None
        self._diff_signature = None

    async def toggle(self) -> None:
        self.expanded = not self.expanded
        await self.set_tool(self.tool)

    async def on_click(self, event: Click) -> None:
        event.stop()
        await self.toggle()

    async def on_key(self, event: Key) -> None:
        if event.key in {"enter", "space"}:
            event.stop()
            await self.toggle()


class TaskActivityWidget(ToolActivityWidget):
    """A Task card projected solely from its call and linked AgentView children."""

    def __init__(
        self, tool: ToolCallView, agents: Mapping[str, AgentView], **kwargs: Any
    ) -> None:
        super().__init__(tool, **kwargs)
        self.agents = agents
        self._child_links: dict[str, AgentActivityLink] = {}

    async def set_task(
        self, tool: ToolCallView, agents: Mapping[str, AgentView]
    ) -> None:
        self.agents = agents
        await self.set_tool(tool)
        wanted = {agent.id for agent in self._children()}
        for agent_id, link in tuple(self._child_links.items()):
            if agent_id not in wanted:
                await link.remove()
                del self._child_links[agent_id]
        for agent in self._children():
            link = self._child_links.get(agent.id)
            if link is None:
                link = AgentActivityLink(agent, classes="task-child")
                self._child_links[agent.id] = link
                await self.mount(link)
            else:
                link.set_agent(agent)

    def _children(self) -> Iterable[AgentView]:
        return (
            self.agents[agent_id]
            for agent_id in self.tool.child_agent_ids
            if agent_id in self.agents
        )

    def _expanded_text(self) -> str:
        rows = []
        for agent in self._children():
            latest = _latest_activity(agent)
            rows.append(
                f"{_text(agent.type or agent.id, 48)} · {_text(agent.task or agent.description, 96)}"
                f" · {_text(agent.status, 24)} · {_agent_metrics(agent)}\n  {latest}"
            )
        return "\n".join(rows) or super()._expanded_text()

    async def on_click(self, event: Click) -> None:
        event.stop()
        children = list(self._children())
        if len(children) == 1:
            self.post_message(AgentOpenRequested(children[0].id))
            return
        await self.toggle()

    async def on_key(self, event: Key) -> None:
        if event.key == "enter":
            event.stop()
            children = list(self._children())
            if children:
                self.post_message(AgentOpenRequested(children[0].id))
            else:
                await self.toggle()
        elif event.key == "space":
            event.stop()
            await self.toggle()


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
        entries: list[tuple[int, str, object]] = []
        entries.extend(
            (message.event_seq, f"message:{message.id or index}", message)
            for index, message in enumerate(turn.messages)
            if _has_message_content(message)
            and f"{self.turn_id}:message:{message.id or index}" != hide_greeting_key
        )
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
        wanted = {key for _, key, _ in entries}
        for key, widget in tuple(self._items.items()):
            if key not in wanted:
                await widget.remove()
                del self._items[key]
        for _, key, value in entries:
            widget = self._items.get(key)
            if isinstance(value, MessageView):
                if widget is None:
                    widget = (
                        UserMessage(value, classes="timeline-user")
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
                        _text(value), markup=False, classes="timeline-error"
                    )
                    self._items[key] = widget
                    await self.mount(widget)
                elif isinstance(widget, Static):
                    widget.update(_text(value))

        if turn.phase == "completed":
            summary = _turn_summary(turn)
            if self._summary_widget is None:
                self._summary_widget = Static(
                    summary, markup=False, classes="timeline-summary"
                )
                await self.mount(self._summary_widget)
            else:
                self._summary_widget.update(summary)
        elif self._summary_widget is not None:
            await self._summary_widget.remove()
            self._summary_widget = None


class ConversationTimeline(VerticalScroll):
    """Single-column reducer projection with tail-follow only when pinned."""

    can_focus = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._turns: dict[str, TurnWidget] = {}

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
            await widget.set_turn(
                turn,
                view.agents,
                hide_setup_error=turn.id in hidden_errors,
                hide_greeting_key=initial_greeting_key,
            )
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False)

    def on_resize(self, _event: object) -> None:
        for turn in self._turns.values():
            for widget in turn._items.values():
                if isinstance(widget, ToolActivityWidget):
                    self.run_worker(
                        widget.set_tool(widget.tool),
                        group="timeline-resize",
                        exclusive=False,
                    )


__all__ = [
    "ConversationTimeline",
    "TaskActivityWidget",
    "ToolActivityWidget",
    "TurnWidget",
    "format_arguments",
]
