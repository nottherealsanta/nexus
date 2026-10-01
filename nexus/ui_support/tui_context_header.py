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
from .context import _compact_tokens, header_system_prompt, tool_groups
from .text import escape_controls
from .tui_widgets import agent_color, context_group_widgets


def prompt_preview(text: str, lines: int = 5) -> str:
    parts = text.splitlines()
    marker = "--- Selected agent instructions ---"
    if marker in parts:
        index = parts.index(marker)
        agent_lines = [line for line in parts[index + 1:] if line.strip()][:2]
        return "\n".join([*parts[:2], marker, *agent_lines, "… open for full prompt"])
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
        self.title_text, self.body_text = title, escape_controls(body)
        self.category = category
        #: Explicit Markdown bodies opt in; context text remains literal and safe.
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "context-modal-edit":
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "context-modal-edit":
            self.dismiss()
            self.app.call_after_refresh(lambda: self.app.action_open_settings(category="tools"))


def scope_counts(rows: list[dict]) -> str:
    project = sum(row.get("scope") == "project" for row in rows)
    return f"Project {project} | Global {len(rows) - project}"


class ExtensionsModal(Screen):
    """Session choices backed by durable host commands (plan §1)."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "dismiss", "Close")]

    def __init__(self, category: str, result: ContextInspectResult, *, locked: bool = False) -> None:
        super().__init__()
        self.category, self.result = category, result
        self.locked = result.context_locked or locked
        self.rows = result.skills_index if category == "skills" else result.mcp_servers
        self.pending = False

    def compose(self) -> ComposeResult:
        with Vertical(id="context-modal"):
            yield Static(f"{self.category.title()} · {scope_counts(self.rows)}", id="context-modal-title", markup=False)
            yield Static(
                "Locked after the first turn to preserve the prompt cache. Start a new session to change skills, MCP or agents."
                if self.locked else "Choose what this session can use before its first turn.",
                id="extension-note", markup=False,
            )
            with VerticalScroll(id="context-modal-scroll"):
                if not self.rows:
                    yield Static("(none)", markup=False)
                for index, row in enumerate(self.rows):
                    enabled = row.get("enabled", True)
                    yield Button(
                        escape(f"{'On' if enabled else 'Off'} · {row.get('name', '?')} · {row.get('scope', 'global')}"),
                        id=f"extension-{index}", disabled=self.locked or row.get("config_enabled") is False,
                    )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.pending or not event.button.id or not event.button.id.startswith("extension-"):
            return
        index = int(event.button.id.split("-")[1])
        row = self.rows[index]
        self.pending = True
        try:
            controller = self.app.controller
            if controller.session != self.result.session:
                self.dismiss()
                return
            result = await controller.client.select_context_extension(
                self.result.session, self.category, row["name"], not row.get("enabled", True))
            if controller.session != self.result.session or not self.is_mounted:
                return
            self.result = result
            self.rows = result.skills_index if self.category == "skills" else result.mcp_servers
            updated = self.rows[index]
            event.button.label = escape(f"{'On' if updated.get('enabled', True) else 'Off'} · {updated['name']} · {updated.get('scope', 'global')}")
            self.app._refresh_context_after_selection(result.session)
        except Exception as exc:
            if not self.is_mounted:
                return
            self.query_one("#extension-note", Static).update(str(exc))
            if "locked" in str(exc).lower():
                self.locked = True
                for button in self.query(Button):
                    button.disabled = True
        finally:
            self.pending = False


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
        if self.label in {"Skills", "MCP"} and self.result is not None and hasattr(self.app, "controller") and self.result.session == self.app.controller.session:
            self.app.push_screen(ExtensionsModal("skills" if self.label == "Skills" else "mcp", self.result, locked=bool(self.app.controller.view.turns)))
            return
        category = {"Tools": "tools", "Skills": "skills", "MCP": "mcp"}.get(self.label, "")
        self.app.push_screen(ContextModal(
            self.label, self.detail, category=category))


class ContextHeader(Vertical):
    """The request-context blocks at the start of a conversation.

    A sub agent page also shows its Task (what the root agent asked of it)
    right after the System prompt; the root conversation has no Task block.
    """

    def compose(self) -> ComposeResult:
        for label, slug in (("System prompt", "prompt"), ("Task", "task"), ("Tools", "tools"),
                            ("AGENTS.md", "agents"), ("Skills", "skills"), ("MCP", "mcp")):
            classes = "context-block -task" if slug == "task" else "context-block"
            yield ContextBlock(label, id=f"context-{slug}", classes=classes)

    def set_task(self, task: str) -> None:
        """Show the root agent's task for this sub agent; empty hides the block."""
        block = self.query_one("#context-task", ContextBlock)
        block.display = bool(task)
        block.set_data(task, task or "(none)")

    def set_unavailable(self) -> None:
        for block in self.query(ContextBlock):
            block.result = None
            block.set_data("Context unavailable", "Context unavailable")

    def set_sent_request(self, context: Mapping, *, session: str, note: str) -> None:
        """Fill from a subagent's recorded request (``AgentTranscript.context``).

        Unknown fields are ignored; with no request yet, ``note`` stands in the
        System prompt block and the other blocks stay empty.
        """
        if not context:
            for block in self.query(ContextBlock):
                if block.id != "context-task":
                    block.result = None
                    block.set_data(note if block.id == "context-prompt" else "", note)
            return
        fields = set(ContextInspectResult.__struct_fields__) - {"session"}
        result = ContextInspectResult(session=session, **{k: v for k, v in context.items() if k in fields})
        color = result.agent.get("color")
        self.set_data(result, color=color if isinstance(color, str) and color else None)

    def set_data(self, result: ContextInspectResult, *, color: str | None = None) -> None:
        """Fill the blocks; section chips take the agent's color (host color first)."""
        name = str(result.agent.get("name") or "build")
        host_color = result.agent.get("color")
        color = color or (host_color if isinstance(host_color, str) and host_color else agent_color(name))
        prompt = header_system_prompt(result)
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
        agents = next(
            (str(part.get("text") or "") for part in result.included_parts
             if isinstance(part, Mapping) and part.get("name") == "agents_md"), "")
        self.query_one("#context-agents", ContextBlock).set_data(
            prompt_preview(agents) if agents.strip() else "", agents or "(none)", color=color)
        skills = [row for row in result.skills_index if isinstance(row, dict)]
        skill_names = [str(row.get("name", "")) + (" (off)" if row.get("enabled") is False else "") for row in skills if row.get("name")]
        skill_details = [f"{row.get('name', '?')} · {row.get('scope', '')} · {row.get('origin', '')}\n{row.get('description', '')}" for row in skills]
        self.query_one("#context-skills", ContextBlock).set_data(
            scope_counts(skills) + ("\n" + render_columns(skill_names) if skills else ""), "\n\n".join(skill_details) or "(none)", color=color)
        self.query_one("#context-skills", ContextBlock).result = result
        self.query_one("#context-mcp", ContextBlock).result = result
        if not skills:
            block = self.query_one("#context-skills", ContextBlock)
            block.set_data(scope_counts(skills), block.detail, color="$nx-label-neutral")
            block.add_class("-empty")
        servers = list(getattr(result, "mcp_servers", ()) or ())
        if servers:
            mcp_labels = [f"{row.get('name')}({row.get('tool_count', 0)})" + (" (off)" if row.get("enabled") is False else "") for row in servers]
            mcp_detail = "\n".join(f"{row.get('name')} · {row.get('status')}\n  " + ", ".join(row.get("tools", ())) for row in servers)
        else:
            mcp_labels = [f"{name}({len(rows)})" for name, rows in mcp_tools.items()]
            mcp_detail = result.mcp_index or "(none)"
        self.query_one("#context-mcp", ContextBlock).set_data(
            scope_counts(servers) + ("\n" + render_columns(mcp_labels) if mcp_labels else ""), mcp_detail, color=color if servers or mcp_labels else "$nx-label-neutral")
        if not servers and not mcp_labels:
            self.query_one("#context-mcp", ContextBlock).add_class("-empty")
