"""Scrollable request-context header and read-only detail dialogs (plan §1)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import Button, Markdown, Static

from ..host.protocol import ContextInspectResult
from .context import _compact_tokens, tool_groups
from .tui_widgets import agent_color, context_group_widgets


def prompt_preview(text: str, lines: int = 5) -> str:
    parts = text.splitlines()
    shown = parts[:lines]
    if len(parts) > lines:
        shown.append(f"… +{len(parts) - lines} more lines")
    return "\n".join(shown) or "(empty)"


def render_columns(labels: list[str], columns: int = 3) -> str:
    if not labels:
        return ""
    width = max(len(label) for label in labels) + 2
    return "\n".join(
        "".join(label.ljust(width) for label in labels[start:start + columns]).rstrip()
        for start in range(0, len(labels), columns)
    )


def group_tools(tools: list[dict]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    mcp: dict[str, list[dict]] = defaultdict(list)
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name", ""))
        group = str(tool.get("group") or name)
        if name.startswith("mcp__"):
            mcp[name.split("__", 2)[1]].append(tool)
        elif group.startswith("mcp:"):
            mcp[group[4:]].append(tool)
        else:
            groups[group].append(tool)
    return dict(sorted(groups.items())), dict(sorted(mcp.items()))


def format_schema_type(prop: Mapping) -> str:
    kind = prop.get("type", "any")
    if isinstance(kind, list):
        return " | ".join(map(str, kind))
    if kind == "array":
        return f"array<{format_schema_type(prop.get('items', {}))}>"
    if "enum" in prop and isinstance(prop["enum"], list):
        return " | ".join(map(str, prop["enum"][:8]))
    return str(kind)


def schema_param_rows(schema: Mapping) -> list[str]:
    properties = schema.get("properties", {})
    required = set(schema.get("required", ()))
    if not isinstance(properties, Mapping):
        return []
    rows = []
    for name, prop in list(properties.items())[:64]:
        if not isinstance(prop, Mapping):
            continue
        marker = "*" if name in required else " "
        description = str(prop.get("description", ""))[:300]
        default = f" (default: {prop['default']})" if "default" in prop else ""
        rows.append(f"{marker} {name}  {format_schema_type(prop)}  — {description}{default}")
    return rows


class ContextModal(Screen):
    """Shared modal frame for a single part of the assembled request."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "dismiss", "Close")]

    def __init__(self, title: str, body: str, *, category: str = "", markdown: bool = False) -> None:
        super().__init__()
        self.title_text, self.body_text = title, body
        self.category = category
        #: The system prompt is Markdown; diffs and indexes stay plain text.
        self.markdown = markdown

    def compose(self) -> ComposeResult:
        with Vertical(id="context-modal"):
            yield Static(self.title_text, id="context-modal-title", markup=False)
            with VerticalScroll(id="context-modal-scroll"):
                if self.markdown:
                    yield Markdown(self.body_text, id="context-modal-body")
                else:
                    yield Static(self.body_text, id="context-modal-body", markup=False)
            if self.category:
                yield Button(f"Edit {self.category}…", id="context-modal-edit")
            yield Button("Close", id="context-modal-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "context-modal-close":
            self.dismiss()
        elif event.button.id == "context-modal-edit":
            category = self.category
            self.dismiss()
            self.app.call_after_refresh(lambda: self.app.action_open_settings(category=category))


class ToolsModal(Screen):
    """Every tool definition the next request carries, by family, one row per tool."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "dismiss", "Close")]

    def __init__(self, tools: list[dict], *, supported: bool | None = None) -> None:
        super().__init__()
        self.groups = tool_groups(tools)
        self.supported = supported

    def compose(self) -> ComposeResult:
        count = sum(len(group.entries) for group in self.groups)
        tokens = sum(group.tokens for group in self.groups)
        with Vertical(id="context-modal", classes="tools-modal"):
            yield Static(
                f"Tools · {count} definition{'s' if count != 1 else ''} · ~{_compact_tokens(tokens)} tokens",
                id="context-modal-title", markup=False,
            )
            if self.supported is False:
                yield Static("The selected model does not support tools; none are sent.", id="tools-modal-note", markup=False)
            with VerticalScroll(id="context-modal-scroll"):
                if not self.groups:
                    yield Static("(none)", id="context-modal-body", markup=False)
                yield from context_group_widgets(
                    self.groups, expanded=tuple(group.key for group in self.groups), flatten_single=True)
            with Horizontal(id="context-modal-actions"):
                yield Button("Edit tools…", id="context-modal-edit")
                yield Button("Close", id="context-modal-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "context-modal-close":
            self.dismiss()
        elif event.button.id == "context-modal-edit":
            self.dismiss()
            self.app.call_after_refresh(lambda: self.app.action_open_settings(category="tools"))


class ContextBlock(Static):
    """A compact clickable summary of one request part."""

    def __init__(self, label: str, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.label = label
        self.body = ""
        self.detail = ""
        self.label_color = "$nx-label-neutral"
        #: The inspected request, so the Tools block can open the Tools dialog.
        self.result: ContextInspectResult | None = None

    def set_data(self, body: str, detail: str, *, color: str = "$nx-label-neutral") -> None:
        """Render the label chip and an indented body; an empty body greys the chip."""
        color = color if body else "$nx-label-neutral"
        self.body, self.detail, self.label_color = body, detail, color
        lines = "".join(f"\n  {escape(line)}" for line in body.splitlines())
        self.update(f"[bold $nx-bg on {color}] {escape(self.label)} [/]{lines}")
        self.set_class(not body, "-empty")

    def on_click(self, event: Click) -> None:
        event.stop()
        if self.label == "Tools" and self.result is not None:
            self.app.push_screen(ToolsModal(list(self.result.tools), supported=self.result.tools_supported))
            return
        category = {"Tools": "tools", "Skills": "skills", "MCP": "mcp"}.get(self.label, "")
        self.app.push_screen(ContextModal(
            self.label, self.detail, category=category, markdown=self.label == "System prompt" and bool(self.body)))


class ContextHeader(Vertical):
    """The four request-context blocks at the start of a conversation."""

    def compose(self) -> ComposeResult:
        for label, slug in (("System prompt", "prompt"), ("Tools", "tools"),
                            ("Skills", "skills"), ("MCP", "mcp")):
            yield ContextBlock(label, id=f"context-{slug}", classes="context-block")

    def set_unavailable(self) -> None:
        for block in self.query(ContextBlock):
            block.result = None
            block.set_data("Context unavailable", "Context unavailable")

    def set_data(self, result: ContextInspectResult, *, color: str | None = None) -> None:
        """Fill the blocks; section chips take the agent's color (host color first)."""
        name = str(result.agent.get("name") or "build")
        host_color = result.agent.get("color")
        color = color or (host_color if isinstance(host_color, str) and host_color else agent_color(name))
        prompt = result.system_text or ""
        self.query_one("#context-prompt", ContextBlock).set_data(
            prompt_preview(prompt) if prompt else "", prompt or "(empty)", color=color)
        groups, mcp_tools = group_tools(result.tools)
        labels = [f"{group}({len(rows)})" if len(rows) > 1 else group for group, rows in groups.items()]
        tool_details = []
        for group, rows in groups.items():
            if len(rows) > 1:
                tool_details.append(f"▾ {group}  ({len(rows)})")
            for row in rows:
                tool_details.append(f"{row.get('name', '?')} — {row.get('description', '')}")
                tool_details.extend("  " + line for line in schema_param_rows(row.get("input_schema") or {}))
                tool_details.append("")
        tools_block = self.query_one("#context-tools", ContextBlock)
        tools_block.result = result
        tools_block.set_data(render_columns(labels), "\n".join(tool_details) or "(none)", color=color)
        skills = [row for row in result.skills_index if isinstance(row, dict) and row.get("included", True)]
        skill_names = [str(row.get("name", "")) for row in skills if row.get("name")]
        skill_details = [f"{row.get('name', '?')} · {row.get('scope', '')} · {row.get('origin', '')}\n{row.get('description', '')}" for row in skills]
        self.query_one("#context-skills", ContextBlock).set_data(
            render_columns(skill_names), "\n\n".join(skill_details) or "(none)", color=color)
        servers = list(getattr(result, "mcp_servers", ()) or ())
        if servers:
            mcp_labels = [f"{row.get('name')}({row.get('tool_count', 0)})" for row in servers]
            mcp_detail = "\n".join(f"{row.get('name')} · {row.get('status')}\n  " + ", ".join(row.get("tools", ())) for row in servers)
        else:
            mcp_labels = [f"{name}({len(rows)})" for name, rows in mcp_tools.items()]
            mcp_detail = result.mcp_index or "(none)"
        self.query_one("#context-mcp", ContextBlock).set_data(
            render_columns(mcp_labels), mcp_detail, color=color)
