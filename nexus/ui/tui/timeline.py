"""Reducer-backed conversation timeline and inline tool activity cards."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Click, Key
from textual.widget import Widget
from textual.widgets import Button, Markdown, Static

from ...view import AgentView, ConversationView, MessageView, ToolCallView, TurnView
from ..cli.render import escape_controls, sanitize
from .messages import AgentOpenRequested

_DETAIL_LIMIT = 1_600
_ARG_LIMIT = 180
_HUNK_HEADER = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@")


def _text(value: object, limit: int = _DETAIL_LIMIT) -> str:
    return sanitize(value, limit)


def _literal(value: object, limit: int = _DETAIL_LIMIT) -> str:
    """Bound terminal data without interpreting it as Markdown or Rich markup."""
    return escape_controls(str(value))[:limit]


def _output(tool: ToolCallView) -> str:
    if tool.display:
        return _literal(tool.display)
    if tool.context_note:
        return _literal(tool.context_note)
    if not tool.result:
        return ""
    parts: list[str] = []
    for block in tool.result[:8]:
        if isinstance(block, Mapping):
            value = block.get("text", block.get("content", block))
        else:
            value = block
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=True, default=str)
        if value:
            parts.append(_literal(value, 400))
    return "\n".join(parts)[:_DETAIL_LIMIT]


def _first_line(text: str, limit: int = _ARG_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_arguments(tool: ToolCallView) -> str:
    """Return useful, bounded arguments without exposing write payloads."""
    args = tool.input if isinstance(tool.input, dict) else {}
    name = tool.name.casefold()
    path = args.get("path") or args.get("file_path") or ""
    if name == "read":
        extras = [f"{key}={args[key]}" for key in ("offset", "limit") if key in args]
        return _first_line(f"{path}  ({', '.join(extras)})" if extras else str(path))
    if name == "write":
        content = args.get("content")
        lines = content.count("\n") + 1 if isinstance(content, str) and content else 0
        return _first_line(f"{path} ({lines} lines)" if lines else str(path))
    if name in {"edit", "multiedit"}:
        return _first_line(str(path))
    if name in {"bash", "bashoutput", "killshell"}:
        return _first_line(_literal(args.get("command") or args.get("id") or ""))
    if name == "task":
        return _first_line(str(args.get("description") or args.get("task") or args.get("prompt") or ""))
    pairs = []
    for key, value in args.items():
        if key in {"content", "old_string", "new_string"}:
            continue
        pairs.append(f"{key}={_first_line(_literal(value), 48)}")
    return _first_line(", ".join(pairs))


def tool_summary(tool: ToolCallView) -> str:
    output = _output(tool)
    if tool.name.casefold() == "read":
        return "read" if not output else _first_line(output)
    if tool.name.casefold() == "write":
        return "written" if tool.status == "completed" else tool.status
    if tool.name.casefold() == "bash":
        return _first_line(output) if output else tool.status
    if tool.progress:
        return _first_line(_literal(tool.progress[-1]))
    if tool.error:
        return _first_line(_literal(tool.error))
    return _first_line(output) if output else tool.status


def _diff_text(diff: Mapping[str, Any]) -> tuple[str, str] | None:
    """Reconstruct bounded before/after text from Nexus's durable unified hunk."""
    hunk = diff.get("hunk")
    if not isinstance(hunk, str) or not hunk:
        return None
    before: list[str] = []
    after: list[str] = []
    seen_hunk = False
    for line in hunk.splitlines():
        if _HUNK_HEADER.match(line):
            seen_hunk = True
            continue
        if not seen_hunk:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            after.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            before.append(line[1:])
        elif line.startswith(" "):
            before.append(line[1:])
            after.append(line[1:])
    if not before and not after:
        return None
    return "\n".join(before), "\n".join(after)


class AssistantMessage(Markdown):
    """One stable streamed Markdown message, updating only its appended suffix."""

    def __init__(self, message: MessageView, **kwargs: Any) -> None:
        self.message_id = message.id
        self._rendered = escape_controls(message.text)
        self._stream = None
        super().__init__(self._rendered, open_links=False, **kwargs)

    async def set_message(self, message: MessageView) -> None:
        text = escape_controls(message.text)
        if text == self._rendered:
            return
        if self.is_mounted and text.startswith(self._rendered) and self._stream is not None:
            await self._stream.write(text[len(self._rendered) :])
        else:
            await self.update(text)
            self._stream = None
        self._rendered = text
        if message.done and self._stream is not None:
            await self._stream.stop()
            self._stream = None

    def on_mount(self) -> None:
        # Markdown's stream parser keeps delta rendering cheap while preserving
        # regular Markdown semantics for the finished message.
        self._stream = Markdown.get_stream(self)

    async def on_unmount(self) -> None:
        if self._stream is not None:
            await self._stream.stop()


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

    def compose(self) -> ComposeResult:
        yield Static("", id="tool-header", markup=False)
        yield Static("", id="tool-detail", markup=False)
        yield Static("", id="tool-expanded", markup=False)

    async def on_mount(self) -> None:
        await self.set_tool(self.tool)

    async def set_tool(self, tool: ToolCallView) -> None:
        self.tool = tool
        if not self.is_mounted:
            return
        name = _text(tool.name or "tool", 80)
        args = format_arguments(tool)
        duration = f" {tool.duration_ms}ms" if isinstance(tool.duration_ms, int) else ""
        marker = "failed" if tool.status == "failed" or tool.is_error else tool.status
        self.query_one("#tool-header", Static).update(
            f"{name}" + (f"  {args}" if args else "") + f"  [{marker}{duration}]"
        )
        self.query_one("#tool-detail", Static).update(tool_summary(tool))
        self.query_one("#tool-expanded", Static).update(self._expanded_text() if self.expanded else "")
        await self._sync_diff()

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
            lines.append("error: " + _literal(tool.error, 600))
        if tool.context_note:
            lines.append("context: " + _literal(tool.context_note, 400))
        return "\n".join(lines)[:_DETAIL_LIMIT]

    async def _sync_diff(self) -> None:
        """Mount the optional dependency only for valid, wide Edit artifacts."""
        diff = self.tool.diff if self.tool.name.casefold() in {"edit", "multiedit"} else None
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
            view = DiffView(path, path, text[0], text[1], split=True, annotations=True, wrap=True)
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

    def __init__(self, tool: ToolCallView, agents: Mapping[str, AgentView], **kwargs: Any) -> None:
        super().__init__(tool, **kwargs)
        self.agents = agents
        self._child_links: dict[str, AgentActivityLink] = {}

    async def set_task(self, tool: ToolCallView, agents: Mapping[str, AgentView]) -> None:
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
        return (self.agents[agent_id] for agent_id in self.tool.child_agent_ids if agent_id in self.agents)

    def _expanded_text(self) -> str:
        rows = []
        for agent in self._children():
            latest = _latest_activity(agent)
            rows.append(
                f"{_text(agent.type or agent.id, 48)} · {_text(agent.task or agent.description, 96)}"
                f" · {_text(agent.status, 24)}\n  {latest}"
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


def _latest_activity(agent: AgentView) -> str:
    for turn in reversed(agent.body.turns):
        if turn.tools:
            tool = turn.tools[-1]
            return _text(f"{tool.name or 'tool'}: {tool_summary(tool)}", 140)
        for message in reversed(turn.messages):
            if message.text:
                return _text(message.text, 140)
    return "waiting for activity"


class AgentActivityLink(Button):
    """Focusable child projection; it holds no activity state of its own."""

    def __init__(self, agent: AgentView, **kwargs: Any) -> None:
        self.agent = agent
        super().__init__(self._label(), **kwargs)

    def _label(self) -> str:
        return (
            f"{_text(self.agent.type or self.agent.id, 36)} · "
            f"{_text(self.agent.status, 18)} · {_latest_activity(self.agent)}"
        )

    def set_agent(self, agent: AgentView) -> None:
        self.agent = agent
        self.label = self._label()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button is self:
            self.post_message(AgentOpenRequested(self.agent.id))


class TurnWidget(Widget):
    """One turn reconciled by durable message and call identifiers."""

    def __init__(self, turn: TurnView, agents: Mapping[str, AgentView], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.turn_id = turn.id
        self._items: dict[str, Widget] = {}
        self.turn = turn
        self.agents = agents

    async def on_mount(self) -> None:
        await self.set_turn(self.turn, self.agents)

    async def set_turn(self, turn: TurnView, agents: Mapping[str, AgentView]) -> None:
        self.turn = turn
        self.agents = agents
        entries: list[tuple[int, str, object]] = []
        entries.extend((message.event_seq, f"message:{message.id or index}", message) for index, message in enumerate(turn.messages) if message.text)
        entries.extend((tool.event_seq, f"tool:{tool.call_id}", tool) for tool in turn.tools)
        entries.sort(key=lambda entry: (entry[0], 0 if entry[1].startswith("message") else 1, entry[1]))
        wanted = {key for _, key, _ in entries}
        for key, widget in tuple(self._items.items()):
            if key not in wanted:
                await widget.remove()
                del self._items[key]
        for _, key, value in entries:
            widget = self._items.get(key)
            if isinstance(value, MessageView):
                if widget is None:
                    widget = UserMessage(value, classes="timeline-user") if value.role == "user" else AssistantMessage(value, classes="timeline-assistant")
                    self._items[key] = widget
                    await self.mount(widget)
                elif isinstance(widget, (UserMessage, AssistantMessage)):
                    await widget.set_message(value) if isinstance(widget, AssistantMessage) else widget.set_message(value)
            elif isinstance(value, ToolCallView):
                if widget is None:
                    widget = TaskActivityWidget(value, agents, classes="tool-card") if value.name.casefold() == "task" else ToolActivityWidget(value, classes="tool-card")
                    self._items[key] = widget
                    await self.mount(widget)
                    if isinstance(widget, TaskActivityWidget):
                        await widget.set_task(value, agents)
                elif isinstance(widget, TaskActivityWidget):
                    await widget.set_task(value, agents)
                elif isinstance(widget, ToolActivityWidget):
                    await widget.set_tool(value)


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
            else:
                await widget.set_turn(turn, view.agents)
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False)

    def on_resize(self, _event: object) -> None:
        for turn in self._turns.values():
            for widget in turn._items.values():
                if isinstance(widget, ToolActivityWidget):
                    self.run_worker(widget.set_tool(widget.tool), group="timeline-resize", exclusive=False)


__all__ = ["ConversationTimeline", "TaskActivityWidget", "ToolActivityWidget", "TurnWidget", "format_arguments"]
