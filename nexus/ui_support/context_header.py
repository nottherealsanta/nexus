"""Pure context-header blocks shared by the Textual and native shells.

``header_blocks`` reduces an inspected request into the labelled blocks that
open every conversation (System prompt, Tools, AGENTS.md, Skills, MCP): label,
one-line/column preview, full detail, token estimate and colour. No UI toolkit
is imported, so both surfaces show the same content.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .context import estimate_tokens, header_system_prompt, tool_entry

import hashlib

AGENT_COLORS = (
    "#a78bfa",  # violet
    "#69b7d5",  # blue
    "#86b97a",  # green
    "#d18a38",  # amber
    "#dc8295",  # rose
    "#55b9a5",  # teal
)


def agent_color(name: str) -> str:
    """Return a stable readable identity color when the host has none yet."""
    digest = hashlib.sha256(name.casefold().encode("utf-8")).digest()
    return AGENT_COLORS[int.from_bytes(digest[:4], "big") % len(AGENT_COLORS)]

#: Colour token of a block with nothing to show.
NEUTRAL = "$nx-label-neutral"


@dataclass(frozen=True)
class HeaderBlock:
    key: str  # system | tools | agents | skills | mcp (the context_show keys)
    label: str
    body: str
    detail: str
    tokens: int | None
    color: str


def one_line_preview(text: str, limit: int = 100) -> str:
    """The header's preview: the first non-empty line, the rest counted."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0].strip()
    if len(first) > limit:
        first = first[:limit - 1].rstrip() + "…"
    rest = len(lines) - 1
    return first + (f"  … +{rest} more line{'s' if rest != 1 else ''}" if rest else "")


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


def scope_counts(rows: list[dict]) -> str:
    project = sum(row.get("scope") == "project" for row in rows)
    return f"Project {project} | Global {len(rows) - project}"


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


def tool_detail_lines(groups: Mapping[str, list[dict]]) -> list[str]:
    """Full Tools detail: each family, tool, and its parameter rows."""
    lines: list[str] = []
    for group, rows in groups.items():
        if len(rows) > 1:
            lines.append(f"▾ {group}  ({len(rows)})")
        for row in rows:
            lines.append(f"{row.get('name', '?')} — {row.get('description', '')}")
            lines.extend("  " + line for line in schema_param_rows(row.get("input_schema") or {}))
            lines.append("")
    return lines


def header_blocks(result: Any, color: str) -> list[HeaderBlock]:
    """The five header blocks of an inspected request, in display order."""
    prompt = header_system_prompt(result)
    groups, mcp_tools = group_tools([t for t in result.tools if not isinstance(t, Mapping) or t.get('enabled') is not False])
    labels = [f"{group}({len(rows)})" if len(rows) > 1 else group for group, rows in groups.items()]
    builtin = [tool for rows in groups.values() for tool in rows]
    agents = next(
        (str(part.get("text") or "") for part in result.included_parts
         if isinstance(part, Mapping) and part.get("name") == "agents_md"), "")
    skills = [row for row in result.skills_index if isinstance(row, dict)]
    skill_names = [str(row.get("name", "")) + (" (off)" if row.get("enabled") is False else "") for row in skills if row.get("name")]
    skill_details = [f"{row.get('name', '?')} · {row.get('scope', '')} · {row.get('origin', '')}\n{row.get('description', '')}" for row in skills]
    skill_tokens = estimate_tokens("\n".join(f"{row.get('name', '')}: {row.get('description', '')}"
                                            for row in skills if row.get("enabled") is not False))
    servers = list(getattr(result, "mcp_servers", ()) or ())
    if servers:
        mcp_labels = [f"{row.get('name')}({row.get('tool_count', 0)})" + (" (off)" if row.get("enabled") is False else "") for row in servers]
        mcp_detail = "\n".join(f"{row.get('name')} · {row.get('status')}\n  " + ", ".join(row.get("tools", ())) for row in servers)
    else:
        mcp_labels = [f"{name}({len(rows)})" for name, rows in mcp_tools.items()]
        mcp_detail = result.mcp_index or "(none)"
    mcp_tokens = sum(tool_entry(tool).tokens for rows in mcp_tools.values() for tool in rows)
    def block(key, label, body, detail, tokens, paint=color):
        return HeaderBlock(key, label, body, detail, tokens, paint if body else NEUTRAL)
    return [
        block("system", "System prompt", one_line_preview(prompt), prompt or "(empty)", estimate_tokens(prompt)),
        block("tools", "Tools", render_columns(labels), "\n".join(tool_detail_lines(groups)) or "(none)",
              sum(tool_entry(tool).tokens for tool in builtin)),
        block("agents", "AGENTS.md", one_line_preview(agents), agents or "(none)", estimate_tokens(agents)),
        block("skills", "Skills", scope_counts(skills) + ("\n" + render_columns(skill_names) if skills else ""),
              "\n\n".join(skill_details) or "(none)", skill_tokens, color if skills else NEUTRAL),
        block("mcp", "MCP", scope_counts(servers) + ("\n" + render_columns(mcp_labels) if mcp_labels else ""), mcp_detail, mcp_tokens,
              color if servers or mcp_labels else NEUTRAL),
    ]
