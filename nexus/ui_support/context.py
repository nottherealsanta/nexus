"""Pure display projections for context preview and session usage."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from ..host import protocol as p
from ..view import ConversationView
from .text import escape_controls, redact

MAX_TEXT = 1_000_000
MAX_ROWS = 512


def _plain(value: object, limit: int | None = MAX_TEXT) -> str:
    if isinstance(value, (dict, list, tuple)):
        try:
            value = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        except (TypeError, ValueError):
            value = str(value)
    text = redact(escape_controls(str(value)).replace("\t", "\\t"))
    return text if limit is None or len(text) <= limit else text[:limit] + "…"


def _list_rows(rows, render) -> list[str]:
    result = [render(row) for row in list(rows or ())[:MAX_ROWS]]
    count = len(rows or ())
    if count > MAX_ROWS:
        result.append(f"… {count - MAX_ROWS} more entries omitted from display")
    return result


def render_context_details(
    inspection: p.ContextInspectResult | None,
    usage: str,
    *,
    error: str | None = None,
    session: str = "",
) -> str:
    """Render the read-only request in provider structure and wire order."""
    lines = [f"# CURRENT REQUEST · {_plain(session, 100)}", f"Observed usage: {usage}", ""]
    if error:
        lines.extend(("Preview unavailable · context inspection failed", _plain(error), "Try again after the active turn finishes."))
        return "\n".join(lines)
    result = inspection
    if result is None:
        lines.extend(("Preview unavailable", "The host returned no context inspection."))
        return "\n".join(lines)

    lines.extend((
        f"Provider/model: {_plain(result.provider or '?', 120)}/{_plain(result.model or '?', 180)}",
        f"Agent: {_plain(result.agent.get('name') or 'not selected', 120)} · {_plain(result.agent.get('source') or 'unknown', 80)}",
        f"Manifest generation: {result.manifest_generation if result.manifest_generation is not None else 'not reported'}",
        "Provider call: No · read-only assembly · no session write · unsent draft excluded.",
    ))

    lines.extend(("", "## SYSTEM PROMPT · request.system", ""))
    lines.append(_plain(result.system_text or "(empty)", MAX_TEXT))

    lines.extend(("", f"## TOOLS · structured request.tools · {len(result.tools)} definition(s)", ""))
    if result.tools_supported is False:
        lines.append("The selected model does not support tools.")
    elif not result.tools:
        lines.append("(none)")
    for index, tool in enumerate(result.tools, 1):
        if not isinstance(tool, Mapping):
            continue
        schema = {
            "name": tool.get("name", "tool"),
            "description": tool.get("description", ""),
            "input_schema": tool.get("input_schema", {}),
        }
        lines.append(
            f"[{index}] {_plain(schema['name'], 120)} · "
            f"~{max(1, len(_plain(schema, None)) // 4):,} tokens"
        )
        lines.extend(("Input schema:", "```json", _plain(schema, None), "```"))

    lines.extend(("", f"## MESSAGES · ordered request.messages · {len(result.messages)}", ""))
    lines.append(
        "Persisted conversation history "
        + ("is included." if result.history_included else "is empty.")
    )
    if not result.messages:
        lines.append("(none)")
    for index, message in enumerate(result.messages, 1):
        if not isinstance(message, Mapping):
            continue
        blocks = message.get("blocks", ())
        lines.append(
            f"[{index}] {_plain(message.get('role', 'unknown'), 40)} · "
            f"~{_estimate_message_tokens(message):,} tokens"
        )
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            kind = block.get("type", "content")
            if kind in {"text", "thinking"}:
                lines.append(_plain(block.get("text") or "", MAX_TEXT))
            elif kind == "tool_use":
                lines.append(
                    f"Tool call · {_plain(block.get('name', 'tool'), 120)} · "
                    f"id {_plain(block.get('id', 'not reported'), 120)}"
                )
                lines.extend(("```json", _plain(block.get("input", {}), None), "```"))
            elif kind == "tool_result":
                lines.append(
                    f"Tool result · for call {_plain(block.get('tool_use_id', 'not reported'), 120)}"
                    f"{' · error' if block.get('is_error') else ''}"
                )
                for part in block.get("content", ()):
                    if isinstance(part, Mapping):
                        lines.append(_plain(part.get("text") or "[image omitted]", MAX_TEXT))
            else:
                lines.append(_plain(block.get("text") or f"[{kind} omitted]", MAX_TEXT))

    context_data = result.request_context
    lines.extend(("", "## ACCOUNTING · assembler request context", ""))
    if context_data:
        for key, value in context_data.items():
            lines.append(f"{_plain(key, 80)}: {_plain(value, 500)}")
    else:
        lines.append("Token accounting unavailable.")
    if result.params:
        lines.append("Model parameters: " + _plain(result.params, None))

    lines.extend(("", "## INCLUDED SYSTEM PARTS · contribution breakdown", ""))
    if not result.included_parts:
        lines.append("(none reported)")
    for part in result.included_parts:
        if isinstance(part, Mapping):
            lines.extend((f"### {_plain(part.get('name', 'part'), 100)}", "", _plain(part.get("text", ""), MAX_TEXT), ""))

    lines.extend(("", "## SYSTEM FILE STATUS · SOUL.md / AGENTS.md / MEMORY.md"))
    for key, label in (("soul", "SOUL.md"), ("agents", "AGENTS.md"), ("memory", "MEMORY.md")):
        item = result.system_files.get(key, {})
        if not isinstance(item, dict):
            item = {}
        flags = ", ".join(
            f"{name}={'yes' if item.get(name) else 'no'}"
            for name in ("configured", "loaded", "included", "included_nonempty", "truncated")
        )
        source = f" · source {_plain(item['source'], 120)}" if item.get("source") else ""
        lines.append(f"{label}: {flags or 'status not reported'}{source}")

    lines.extend(("", "## SKILLS INDEX"))
    skills = result.skills_index
    lines.append("(none reported)" if not skills else "")
    lines.extend(_list_rows(
        skills,
        lambda row: (
            f"{_plain(row.get('name', '?'), 120)} · {'included' if row.get('included') else 'available, not included'}\n"
            f"  {_plain(row.get('description', ''), 2048)}"
        ) if isinstance(row, dict) else _plain(row, 500),
    ))
    lines.extend(("", "## MCP INDEX · system-prompt contribution", "", _plain(result.mcp_index or "(none reported)", 4000)))
    if result.budget:
        lines.extend(("", "## PREVIEW BUDGET", "", "```json", _plain(result.budget, None), "```"))
    if result.omitted:
        lines.extend(("", "## DISPLAY LIMITATIONS"))
        lines.extend(f"· {_plain(row, 500)}" for row in result.omitted)
    return "\n".join(lines)


def render_context_summary(
    inspection: p.ContextInspectResult | None,
    *,
    loading: bool = False,
    error: str | None = None,
    width: int = 80,
) -> Text:
    """Render a bounded current-request preview for the main pane.

    Every request component is clipped independently so one large prompt,
    schema, or history message cannot hide the other sections. The full,
    display-safe request remains available in the context modal.
    """
    if loading:
        return Text("CURRENT REQUEST CONTEXT\nLoading assembled request…")
    if error:
        return Text(f"CURRENT REQUEST CONTEXT\nContext unavailable · {_plain(error, 1000)}")
    if inspection is None:
        return Text("CURRENT REQUEST CONTEXT\nNo context inspection was reported by the host.")

    agent = inspection.agent if isinstance(inspection.agent, dict) else {}
    width = max(1, width)
    output = Text()
    output.append("CURRENT REQUEST CONTEXT · read-only\n", style="bold #d18a38")
    output.append(
        f"Agent: {_plain(agent.get('name') or 'not selected', 120)} · provider/model: "
        f"{_plain(inspection.provider or '?', 80)}/{_plain(inspection.model or '?', 120)}\n"
        f"Accounting: {inspection.request_context.get('used_tokens', 'not reported')} / "
        f"{inspection.request_context.get('input_budget', 'not reported')} tokens · "
        f"{len(inspection.tools)} tool definitions · {len(inspection.messages)} ordered messages\n"
        "Click a preview or ... for full context · each part shows up to 10 rendered lines\n",
        style="dim",
    )

    def add_section(title: str, content: object) -> None:
        source = (_plain(content, None) or "(empty)").strip("\n")
        # Preserve request-part line boundaries while rendering Markdown.
        source = source.replace("\n", "  \n")
        console = Console(width=width, force_terminal=True, color_system="truecolor")
        rows = console.render_lines(
            Markdown(source), console.options.update(width=width, overflow="fold"), pad=False
        ) or [[]]
        output.append(f"\n{title}\n", style="bold #d18a38")
        truncated = len(rows) > 10
        visible_rows = rows[:9] if truncated else rows[:10]
        for index, row in enumerate(visible_rows):
            for segment in row:
                output.append(segment.text, style=segment.style)
            if index < len(visible_rows) - 1 or not truncated:
                output.append("\n")
        if truncated:
            output.append("\n")
            output.append("...", style="dim #d18a38")

    add_section("SYSTEM PROMPT · request.system", inspection.system_text or "(empty)")
    output.append(f"\nTOOLS · structured request.tools · {len(inspection.tools)}\n", style="bold #d18a38")
    if inspection.tools_supported is False:
        output.append("(unsupported by model)\n")
    elif not inspection.tools:
        output.append("(none)\n")
    for index, tool in enumerate(inspection.tools, 1):
        if isinstance(tool, dict):
            add_section(
                f"[{index}] {_plain(tool.get('name', 'tool'), 100)} · request.tools",
                {
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema", {}),
                },
            )
    output.append(f"\nMESSAGES · ordered request.messages · {len(inspection.messages)}\n", style="bold #d18a38")
    if not inspection.messages:
        output.append("(none)\n")
    for index, message in enumerate(inspection.messages, 1):
        if isinstance(message, dict):
            add_section(
                f"[{index}] {message.get('role', 'unknown')} · ~{_estimate_message_tokens(message):,} tokens",
                context_message_text(message),
            )
    for part in inspection.included_parts:
        if isinstance(part, dict):
            add_section(
                f"SYSTEM PART · {_plain(part.get('name', 'part'), 100)}",
                part.get("text", ""),
            )
    if inspection.skills_index:
        add_section("SKILLS INDEX", inspection.skills_index)
    if inspection.mcp_index:
        add_section("MCP INDEX · also included in system text", inspection.mcp_index)
    add_section(
        "ACCOUNTING · request context",
        inspection.request_context or inspection.budget or "(not reported)",
    )
    if inspection.budget:
        add_section("ASSEMBLY BUDGET · per-part allocation", inspection.budget)
    if inspection.params:
        add_section("MODEL PARAMETERS", inspection.params)
    if inspection.omitted:
        add_section("DISPLAY LIMITATIONS", inspection.omitted)
    return output


def context_details_renderable(
    inspection, usage: str, *, error=None, session="", width=100
) -> Text:
    """Render full context Markdown as styled, queryable terminal text."""
    source = render_context_details(inspection, usage, error=error, session=session)
    width = max(1, width)
    console = Console(width=width, force_terminal=True, color_system="truecolor")
    rows = console.render_lines(
        Markdown(source), console.options.update(width=width, overflow="fold"), pad=False
    )
    rendered = Text()
    for index, row in enumerate(rows):
        for segment in row:
            rendered.append(segment.text, style=segment.style)
        if index + 1 < len(rows):
            rendered.append("\n")
    return rendered


def context_message_text(message: Mapping[str, object]) -> str:
    """Render projected provider blocks without hiding structured call data."""
    rendered = []
    for block in message.get("blocks", ()):
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type", "content")
        if kind in {"text", "thinking"}:
            rendered.append(_plain(block.get("text") or "", MAX_TEXT))
        elif kind == "tool_use":
            rendered.append(
                f"Tool call · {block.get('name', 'tool')} · id {block.get('id', 'not reported')}\n"
                f"{_plain(block.get('input', {}), None)}"
            )
        elif kind == "tool_result":
            rendered.append(
                f"Tool result · for call {block.get('tool_use_id', 'not reported')}\n" + "\n".join(
                _plain(row.get("text") or "[image omitted]", MAX_TEXT)
                for row in block.get("content", ()) if isinstance(row, Mapping)
                )
            )
        else:
            rendered.append(_plain(block.get("text") or f"[{kind} omitted]", MAX_TEXT))
    return "\n\n".join(rendered) or "(empty)"


def _compact_tokens(value: int) -> str:
    if abs(value) >= 1_000_000:
        compact = f"{value / 1_000_000:.1f}M"
    elif abs(value) >= 1_000:
        compact = f"{value / 1_000:.1f}K"
    else:
        compact = str(value)
    return compact.replace(".0K", "K").replace(".0M", "M")


def _estimate_message_tokens(message: Mapping[str, object]) -> int:
    return max(1, (len(str(message.get("role", ""))) + len(context_message_text(message))) // 4 + 1)


def estimate_tokens(text: str) -> int:
    """The display estimate used by both context dialogs (~4 characters a token)."""
    return (len(text) + 3) // 4


@dataclass(frozen=True)
class ContextEntry:
    """One expandable row in a context dialog group."""

    title: str
    body: str
    tokens: int = 0
    detail: str = ""
    error: bool = False


@dataclass(frozen=True)
class ContextGroup:
    """A section of the assembled request, in wire order."""

    key: str
    title: str
    entries: tuple[ContextEntry, ...]
    detail: str = ""
    #: Set when entries overlap (system parts are also in the full prompt).
    total: int | None = None

    @property
    def tokens(self) -> int:
        return self.total if self.total is not None else sum(entry.tokens for entry in self.entries)


def _schema_rows(schema: object) -> list[str]:
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(properties, Mapping):
        return []
    required = set(schema.get("required") or ())
    rows = []
    for name, prop in list(properties.items())[:64]:
        prop = prop if isinstance(prop, Mapping) else {}
        kind = prop.get("type", "any")
        kind = " | ".join(map(str, kind)) if isinstance(kind, list) else str(kind)
        if kind == "array" and isinstance(prop.get("items"), Mapping):
            kind = f"array<{prop['items'].get('type', 'any')}>"
        if isinstance(prop.get("enum"), list):
            kind = " | ".join(map(str, prop["enum"][:8]))
        flag = "required" if name in required else "optional"
        description = _plain(prop.get("description", ""), 300)
        rows.append(f"- `{_plain(name, 80)}` {kind} · {flag}" + (f" — {description}" if description else ""))
    return rows


def tool_entry(tool: Mapping[str, object]) -> ContextEntry:
    """A tool definition: name, one-line summary, then description, parameters and schema."""
    description = _plain(tool.get("description") or "", MAX_TEXT)
    schema = tool.get("input_schema") or {}
    rows = _schema_rows(schema)
    raw = _plain({"name": tool.get("name", "tool"), "description": tool.get("description", ""), "input_schema": schema}, None)
    body = "\n\n".join(part for part in (
        description or "(no description)",
        "**Parameters**\n" + ("\n".join(rows) if rows else "(none)"),
        f"**Schema**\n```json\n{_plain(schema, None)}\n```",
    ) if part)
    first = description.strip().splitlines()[0] if description.strip() else ""
    return ContextEntry(
        title=_plain(tool.get("name", "tool"), 120),
        body=body,
        tokens=estimate_tokens(raw),
        detail=f"{len(rows)} param{'s' if len(rows) != 1 else ''}" + (f" · {first[:90]}" if first else ""),
    )


def tool_groups(tools: list[object]) -> list[ContextGroup]:
    """Tools by family (``group``), then one group per MCP server."""
    families: dict[str, list[ContextEntry]] = {}
    servers: dict[str, list[ContextEntry]] = {}
    for tool in tools[:MAX_ROWS]:
        if not isinstance(tool, Mapping):
            continue
        name, group = str(tool.get("name", "")), str(tool.get("group") or "")
        if name.startswith("mcp__"):
            servers.setdefault(name.split("__", 2)[1], []).append(tool_entry(tool))
        elif group.startswith("mcp:"):
            servers.setdefault(group[4:], []).append(tool_entry(tool))
        else:
            families.setdefault(group or "other", []).append(tool_entry(tool))
    groups = [ContextGroup(f"tools:{key}", _plain(key, 60), tuple(rows)) for key, rows in sorted(families.items())]
    groups += [ContextGroup(f"mcp:{key}", f"MCP · {_plain(key, 60)}", tuple(rows)) for key, rows in sorted(servers.items())]
    return groups


def _message_turns(messages: list[object]) -> list[list[ContextEntry]]:
    """Split messages into turns: each user prompt starts one; a tool call is shown with its result."""
    turns: list[list[ContextEntry]] = []
    calls: dict[str, tuple[int, int]] = {}
    for message in messages[:MAX_ROWS]:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role", "unknown"))
        blocks = [block for block in message.get("blocks", ()) if isinstance(block, Mapping)]
        if role == "user" and any(block.get("type") != "tool_result" for block in blocks) or not turns:
            turns.append([])
        entries = turns[-1]
        text = "\n\n".join(_plain(block.get("text") or "", MAX_TEXT) for block in blocks if block.get("type") == "text")
        if text:
            title = "User message" if role == "user" else "Assistant" if role == "assistant" else role.title()
            entries.append(ContextEntry(title, text, estimate_tokens(text)))
        for block in blocks:
            kind = block.get("type")
            if kind == "thinking" and block.get("text"):
                thought = _plain(block.get("text"), MAX_TEXT)
                entries.append(ContextEntry("Thinking", thought, estimate_tokens(thought)))
            elif kind == "tool_use":
                name = _plain(block.get("name", "tool"), 120)
                arguments = _plain(block.get("input", {}), None)
                calls[str(block.get("id", ""))] = (len(turns) - 1, len(entries))
                entries.append(ContextEntry(
                    f"Tool · {name}", f"**Input**\n```json\n{arguments}\n```",
                    estimate_tokens(arguments), detail=_plain(" ".join(arguments.split()), 90),
                ))
            elif kind == "tool_result":
                output = "\n".join(
                    _plain(row.get("text") or "[image omitted]", MAX_TEXT)
                    for row in block.get("content", ()) if isinstance(row, Mapping)
                ) or "(empty)"
                label = "**Error**" if block.get("is_error") else "**Result**"
                where = calls.pop(str(block.get("tool_use_id", "")), None)
                if where is not None and where[0] < len(turns):
                    call = turns[where[0]][where[1]]
                    turns[where[0]][where[1]] = ContextEntry(
                        call.title, f"{call.body}\n\n{label}\n```\n{output}\n```",
                        call.tokens + estimate_tokens(output), call.detail, bool(block.get("is_error")),
                    )
                else:
                    entries.append(ContextEntry("Tool result", output, estimate_tokens(output), error=bool(block.get("is_error"))))
            elif kind not in {"text", "thinking"}:
                entries.append(ContextEntry(_plain(str(kind), 40).title(), _plain(block.get("text") or f"[{kind} omitted]", MAX_TEXT)))
    return turns


def context_groups(result: p.ContextInspectResult) -> list[ContextGroup]:
    """The next request in wire order: system prompt, tools, each turn, then accounting."""
    system = result.system_text or ""
    parts = [
        ContextEntry(_plain(part.get("name") or "part", 100), _plain(part.get("text") or "", MAX_TEXT), estimate_tokens(str(part.get("text") or "")))
        for part in result.included_parts if isinstance(part, Mapping)
    ]
    names = {entry.title.casefold() for entry in parts}
    skills = [row for row in result.skills_index if isinstance(row, Mapping)]
    if skills and not names & {"skills", "skills_index", "skills index"}:
        lines = [
            f"- **{_plain(row.get('name', '?'), 120)}** · {'included' if row.get('included', True) else 'available, not included'}"
            + (f" — {_plain(row.get('description'), 500)}" if row.get("description") else "")
            for row in skills[:MAX_ROWS]
        ]
        parts.append(ContextEntry("Skills index", "\n".join(lines), detail=f"{len(skills)} skill(s)"))
    if result.mcp_index and not names & {"mcp", "mcp_index", "mcp index"}:
        parts.append(ContextEntry("MCP index", _plain(result.mcp_index, 4000)))
    parts.append(ContextEntry("Full system prompt", _plain(system or "(empty)", MAX_TEXT), estimate_tokens(system), "as sent"))
    detail = f"{len(parts) - 1} part(s)" if len(parts) > 1 else ""
    groups = [ContextGroup("system", "System prompt", tuple(parts), detail, total=estimate_tokens(system))]
    tools = [tool_entry(tool) for tool in result.tools[:MAX_ROWS] if isinstance(tool, Mapping)]
    unsupported = "not supported by this model" if result.tools_supported is False else ""
    groups.append(ContextGroup("tools", "Tools", tuple(tools), unsupported or f"{len(tools)} definition(s)"))
    for index, entries in enumerate(_message_turns(list(result.messages)), 1):
        calls = sum(1 for entry in entries if entry.title.startswith("Tool · "))
        groups.append(ContextGroup(f"turn:{index}", f"Turn {index}", tuple(entries), f"{calls} tool call(s)" if calls else ""))
    request = [ContextEntry("Accounting", _plain(result.request_context or result.budget or "(not reported)", None))]
    if result.params:
        request.append(ContextEntry("Model parameters", _plain(result.params, None)))
    files = []
    for key, label in (("soul", "SOUL.md"), ("agents", "AGENTS.md"), ("memory", "MEMORY.md")):
        item = result.system_files.get(key) if isinstance(result.system_files.get(key), Mapping) else {}
        state = "included" if item.get("included_nonempty") else "loaded, empty" if item.get("loaded") else "not loaded"
        files.append(f"{label}: {state}" + (f" · {_plain(item['source'], 120)}" if item.get("source") else ""))
    request.append(ContextEntry("System files", "\n".join(files)))
    if result.omitted:
        request.append(ContextEntry("Display limitations", "\n".join(f"- {_plain(row, 500)}" for row in result.omitted)))
    groups.append(ContextGroup("request", "Request details", tuple(request), "not counted"))
    return groups


def context_summary(result: p.ContextInspectResult | None, usage: str, *, error: str | None = None) -> str:
    """The dialog header: route, agent, usage and a per-group token breakdown."""
    if error or result is None:
        reason = _plain(error, 1000) if error else "The host returned no context inspection."
        return f"Preview unavailable · {reason}\nTry again after the active turn finishes." if error else f"Preview unavailable\n{reason}"
    groups = context_groups(result)
    conversation = sum(group.tokens for group in groups if group.key.startswith("turn:"))
    breakdown = " · ".join(
        f"{label} ~{_compact_tokens(tokens)}" for label, tokens in (
            ("system", groups[0].tokens), ("tools", groups[1].tokens), ("conversation", conversation),
        )
    )
    agent = result.agent if isinstance(result.agent, Mapping) else {}
    return "\n".join((
        f"{_plain(agent.get('name') or 'default', 80)} · {_plain(result.provider or '?', 80)}/{_plain(result.model or '?', 120)}",
        usage,
        f"Next request (estimated): {breakdown} · {sum(1 for group in groups if group.key.startswith('turn:'))} turn(s)",
        "Provider call: No · read-only assembly · the unsent draft is excluded",
    ))


def context_measure(view: ConversationView) -> tuple[int | None, int | None, bool]:
    """``(used, window, measured)`` for the latest request, never invented.

    ``used`` is the provider-measured prompt when one was reported (see
    ``view.reduce``), otherwise the assembler's estimate. ``window`` is the
    model's whole context window (older logs only carry the input budget).
    """
    data = view.context if isinstance(view.context, dict) else {}
    data = data.get("context", data)

    def number(key: str) -> int | None:
        value = data.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    measured = number("measured_tokens")
    used = measured if measured is not None else number("used_tokens")
    window = number("context_window") or number("input_budget")
    return used, window, measured is not None


def context_usage(view: ConversationView) -> str:
    """Format a compact context entry, never inventing missing measurements."""
    used, window, _measured = context_measure(view)
    if used is None:
        return "0 (0%)"
    total = str(used) if used < 1000 else f"{round(used / 1000)}k"
    if window:
        return f"{total} ({min(999, round(used / window * 100))}%)"
    return f"{total} (0%)"


def context_detail_usage(view: ConversationView) -> str:
    """Explain projected context and observed session usage in the detail view."""
    used, window, measured = context_measure(view)
    source = "reported by the provider" if measured else "estimated"
    if used is None:
        context = "not available in the session view"
    elif window:
        context = f"{min(999, round(used / window * 100))}% · {used:,} of {window:,} tokens ({source})"
    else:
        context = f"{used:,} context tokens used ({source})"
    usage = view.usage
    return (
        f"Context: {context}\n"
        f"Recorded usage: {usage.input_tokens:,} input · {usage.output_tokens:,} output · "
        f"{usage.cache_read_tokens:,} cache read · {usage.cache_write_tokens:,} cache write · "
        f"{usage.reasoning_tokens:,} reasoning tokens"
    )


__all__ = [
    "ContextEntry",
    "ContextGroup",
    "context_detail_usage",
    "context_details_renderable",
    "context_groups",
    "context_measure",
    "context_summary",
    "context_usage",
    "estimate_tokens",
    "render_context_details",
    "render_context_summary",
    "tool_entry",
    "tool_groups",
]
