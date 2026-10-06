"""Pure context-header blocks for the native shell.

``header_blocks`` reduces an inspected request into the labelled blocks that
open every conversation (System prompt, Environment, AGENTS.md, MEMORY.md, Skills, Tools, MCP): label,
one-line/column preview, full detail, token estimate and colour. No UI toolkit
is imported, so both surfaces show the same content.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .context import estimate_tokens, header_prompt_sections, tool_entry

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
    # Primary composer identities are semantic; other agents retain stable hues.
    if name.casefold() == "build":
        return "#5C9CF5"
    if name.casefold() == "orchestrator":
        return "#d18a38"
    digest = hashlib.sha256(name.casefold().encode("utf-8")).digest()
    return AGENT_COLORS[int.from_bytes(digest[:4], "big") % len(AGENT_COLORS)]

#: Colour token of a block with nothing to show.
NEUTRAL = "$nx-label-neutral"


@dataclass(frozen=True)
class HeaderBlock:
    key: str  # system | environment | tools | agents | memory | skills | mcp (the context_show keys)
    label: str
    body: str
    detail: str
    tokens: int | None
    color: str
    inventory: tuple[str, ...] = ()
    inventory_note: str = ""


def one_line_preview(text: str, limit: int = 100) -> str:
    """The header's preview: the first non-empty line, clipped."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0].strip()
    if len(first) > limit:
        first = first[:limit - 1].rstrip() + "…"
    return first


def render_columns(labels: list[str], columns: int = 3) -> str:
    if not labels:
        return ""
    width = max(len(label) for label in labels) + 2
    return "\n".join(
        "".join(label.ljust(width) for label in labels[start:start + columns]).rstrip()
        for start in range(0, len(labels), columns)
    )


def inventory_preview(labels: list[str], columns: int, rows: int = 5) -> str:
    """Bounded fallback preview; native layout chooses columns at viewport width."""
    shown = labels[:columns * rows]
    return "\n".join(filter(None, (render_columns(shown, columns),
        f"{len(labels)} total · {len(labels) - len(shown)} omitted")))


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
    """The named header blocks of an inspected request, in display order."""
    sections, split = header_prompt_sections(result)
    prompt = sections["system"]
    groups, mcp_tools = group_tools([t for t in result.tools if not isinstance(t, Mapping) or t.get('enabled') is not False])
    labels = [f"{group}({len(rows)})" if len(rows) > 1 else group for group, rows in groups.items()]
    builtin = [tool for rows in groups.values() for tool in rows]
    agents = sections["agents"]
    environment = sections["environment"]
    memory = sections["memory"]
    skills = [row for row in result.skills_index if isinstance(row, dict)]
    skill_details = [f"{row.get('name', '?')} · {row.get('scope', '')} · {row.get('origin', '')}\n{row.get('description', '')}" for row in skills]
    skill_part = next((part for part in result.included_parts if isinstance(part, Mapping)
                       and part.get("name") in {"skills", "skills_index", "skills index"}), None)
    # Estimate the actual included prompt contribution, never the available catalogue.
    skill_tokens = estimate_tokens(str(skill_part.get("text") or "")) if skill_part is not None else None
    index_lines = str(skill_part.get("text") or "").splitlines() if skill_part else []
    skill_names = []
    for row in skills:
        name = str(row.get("name") or "")
        if not name:
            continue
        entry = next((line for line in index_lines if line.startswith(name + ":")), None)
        token_label = f"~{estimate_tokens(entry)} tokens" if entry is not None else "tokens unknown"
        skill_names.append(f"{name} · {token_label}" + (" (off)" if row.get("enabled") is False else ""))
    servers = list(getattr(result, "mcp_servers", ()) or ())
    if servers:
        mcp_labels = [f"{row.get('name')}({row.get('tool_count', 0)} · {row.get('tool_loading', 'all')})" + (" (off)" if row.get("enabled") is False else "") + f" · {row.get('status') or 'unknown'}" for row in servers]
        mcp_detail = "\n".join(f"{row.get('name')} · {row.get('status')} · {row.get('tool_loading', 'all')}\n  " + ", ".join(row.get("tools", ())) for row in servers)
    else:
        mcp_labels = [f"{name}({len(rows)}) · unknown" for name, rows in mcp_tools.items()]
        mcp_detail = result.mcp_index or "(none)"
    deferred = sum(row.get("schema_tokens", 0) for row in servers
                   if row.get("enabled") is not False and row.get("tool_loading") == "search")
    if deferred:
        mcp_detail += f"\n~{deferred} tokens deferred"
    mcp_tokens = sum(tool_entry(tool).tokens for rows in mcp_tools.values() for tool in rows)
    def block(key, label, body, detail, tokens, paint=color, inventory=(), note=""):
        return HeaderBlock(key, label, body, detail, tokens, paint if body else NEUTRAL, tuple(inventory), note)
    return [
        block("system", "System prompt", one_line_preview(prompt), prompt or "(empty)", estimate_tokens(prompt)),
        block("environment", "Environment", one_line_preview(environment), environment or "(none)",
              estimate_tokens(environment) if split else None, color),
        block("agents", "AGENTS.md", one_line_preview(agents), agents or "(none)", estimate_tokens(agents) if split else None),
        block("memory", "MEMORY.md", one_line_preview(memory), memory or "(none)",
              estimate_tokens(memory) if split else None),
        block("skills", "Skills", scope_counts(skills) + ("\n" + inventory_preview(skill_names, 2) if skills else ""),
              "\n\n".join(skill_details) or "(none)", skill_tokens if split else None, color if skills else NEUTRAL, inventory=skill_names),
        block("tools", "Tools", inventory_preview(labels, 3), "\n".join(tool_detail_lines(groups)) or "(none)",
              sum(tool_entry(tool).tokens for tool in builtin), inventory=labels),
        block("mcp", "MCP", scope_counts(servers) + ("\n" + inventory_preview(mcp_labels, 1) if mcp_labels else "")
              + (f"\n~{deferred} tokens deferred" if deferred else ""), mcp_detail, mcp_tokens,
              color if servers or mcp_labels else NEUTRAL, inventory=mcp_labels,
              note=f"~{deferred} tokens deferred" if deferred else ""),
    ]
