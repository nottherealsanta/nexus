"""Skills and MCP sections of the context header (docs/ratatui-parity.md; plans CONTEXT_SECTIONS_PLAN §2-3).

Cards are one selectable item each (``Item.lines`` carries the dim rows); Enter opens the skill
page or the MCP server page, Space/click toggles. Everything is read through host commands
(``SkillInspect``, ``McpServerShow``) and the inspected request; nothing here reads files.
"""
from __future__ import annotations

import textwrap

from ...ui_support.context import _compact_tokens

CARD_WIDTH = 100  # wrap card rows here; the panel never exceeds this much text


def wrapped(lines):
    """Wrap each card row, indenting continuations; nothing is cut."""
    out = []
    for line in lines:
        out += textwrap.wrap(line, CARD_WIDTH, subsequent_indent="  ", break_long_words=True) or [""]
    return out


def _part(preview, *names) -> str:
    for part in getattr(preview, "included_parts", ()) or ():
        if isinstance(part, dict) and part.get("name") in names:
            return str(part.get("text") or "")
    return ""


def _toggle(item, row, category, locked):
    enabled = row.get("enabled") is not False
    item.update(toggle_enabled=enabled, toggle_locked=locked,
                toggle_operation={"kind": "context_toggle", "category": category,
                                  "name": row.get("name") or row.get("id"), "enabled": not enabled})


def _rows(preview, category):
    rows = preview.skills_index if category == "skills" else preview.mcp_servers
    default = "global" if category == "skills" else "project"
    rows = [r for r in rows if isinstance(r, dict) and r.get("config_enabled") is not False]
    return sorted(rows, key=lambda r: (r.get("scope", default) != "project", str(r.get("name") or r.get("id"))))


def skill_card(row) -> list[str]:
    lines = [f"{key}  {value}" for key, value in (row.get("frontmatter") or {}).items()]
    if not lines and row.get("description"):
        lines = [f"description  {row['description']}"]
    now = f"~{row.get('context_tokens', 0)} in context" if row.get("enabled") is not False else "~0 in context (off)"
    full = f" · ~{_compact_tokens(row['skill_tokens'])} full skill" if row.get("skill_tokens") else ""
    count = row.get("resources") or 0
    lines.append(now + full + (f" · {count} resource{'s' if count != 1 else ''}" if count else ""))
    return lines


def mcp_card(row) -> list[str]:
    bits = [row.get("command_label") or row.get("transport") or "", f"{row.get('tool_count', 0)} tools"]
    for key, word in (("resource_count", "resource"), ("prompt_count", "prompt")):
        if row.get(key):
            bits.append(f"{row[key]} {word}{'s' if row[key] != 1 else ''}")
    lines = [" · ".join(b for b in bits if b)]
    if row.get("error"):
        lines.append(f"error: {row['error']}")
    else:
        full = _compact_tokens(row.get("schema_tokens", 0))
        loading = row.get("tool_loading", "all")
        lines.append(f"~{full} full schemas" + (" (loaded via search)" if loading == "search" else " (sent)"))
    return lines


def section(wf, category):
    """The Skills or MCP card list (replaces the generic extension picker)."""
    preview = wf.shell.preview
    locked = bool(getattr(preview, "context_locked", False)) or bool(wf.agent_page_id)
    rows = _rows(preview, category)
    on = [r for r in rows if r.get("enabled") is not False]
    items = []
    for row in rows:
        name = row.get("name") or row.get("id")
        scope = row.get("scope", "global" if category == "skills" else "project")
        status = f"  {row.get('status')} · {row.get('transport') or '?'} · {row.get('tool_loading', 'all')}" if category == "mcp" else ""
        items.append((f"{name}  {scope}{status}", {"kind": "context_extension_details", "category": category, "name": name}))
    index = _part(preview, "skills_index", "skills") if category == "skills" else (getattr(preview, "mcp_index", "") or _part(preview, "mcp_index"))
    from ...ui_support.context_header import estimate_tokens
    items.append((f"Show literal index (~{_compact_tokens(estimate_tokens(index))} tokens)", {"kind": "context_index", "category": category}))
    items.append((f"Edit {'skills' if category == 'skills' else 'MCP servers'}…", {"kind": "settings", "scope": "global", "category": category}))
    now = sum(r.get("context_tokens", 0) for r in on)
    deferred = sum(r.get("schema_tokens", 0) for r in on if r.get("tool_loading") == "search") if category == "mcp" else 0
    title = f"{'Skills' if category == 'skills' else 'MCP'} · {len(on)} of {len(rows)} on · ~{_compact_tokens(now)} in context"
    if deferred:
        title += f" · ~{_compact_tokens(deferred)} deferred"
    note = ["Context locked after first turn" if locked else "Enter opens · Space or click toggles"]
    if not rows:
        note.append("(none)")
    wf.menu(title, items, note, layout="context")
    for item, row in zip(wf.shell.items, rows):
        _toggle(item, row, category, locked)
        item["lines"] = wrapped(skill_card(row) if category == "skills" else mcp_card(row))


def literal_index(wf, category):
    preview = wf.shell.preview
    text = _part(preview, "skills_index", "skills") if category == "skills" else (getattr(preview, "mcp_index", "") or _part(preview, "mcp_index"))
    label = "Skills index" if category == "skills" else "MCP index (untrusted, from servers)"
    wf.stack.append((wf.shell.panel_title, wf.shell.panel_lines, wf.shell.items, wf.form, wf.form_target,
                     wf.shell.panel_layout, wf.shell.panel_format, wf.shell.panel_tones))
    wf.shell.show(f"{label} · literal", text or "(empty)", layout="detail")


def _table(rows):
    out = ["| Field | Value |", "| --- | --- |"]
    out += [f"| {k} | {str(v).replace('|', chr(92) + '|').replace(chr(10), ' ')} |" for k, v in rows if v not in (None, "", [])]
    return "\n".join(out)


async def skill_page(wf, name):
    preview = wf.shell.preview
    row = next((r for r in preview.skills_index if r.get("name") == name), {})
    result = await wf.client.skill_inspect(wf.shell.controller.session, name)
    if result.status == "error":
        wf.shell.notice = f"Skill · {name}: {result.error}"
        return
    meta = result.metadata
    fields = [("name", result.name), ("description", meta.get("description")),
              ("allowed-tools", ", ".join(meta.get("allowed_tools") or [])), ("bundles", ", ".join(meta.get("bundles") or [])),
              ("model", meta.get("model")), ("version", meta.get("version"))]
    head = (f"{'● on' if result.enabled else '○ off'} · ~{row.get('context_tokens', 0)} tokens in context · "
            f"~{_compact_tokens(row.get('skill_tokens', 0))} tokens full skill · {result.body_bytes:,} bytes")
    parts = [head, _table(fields), result.body or "(empty body)"]
    if result.truncated:
        parts.append(f"*Content truncated by the host ({len(result.body.encode()):,} of {result.body_bytes:,} bytes shown).*")
    if row.get("resources"):
        parts.append(f"Resources: {row['resources']} bundled file(s)")
    wf.stack.append((wf.shell.panel_title, wf.shell.panel_lines, wf.shell.items, wf.form, wf.form_target,
                     wf.shell.panel_layout, wf.shell.panel_format, wf.shell.panel_tones))
    wf.shell.show(f"Skill · {result.name} · {result.scope} · {result.origin}", "\n\n".join(parts), layout="detail", format="markdown")


def _full_name(preview, server, tool):
    if tool.startswith("mcp__"):
        return tool  # the host already reports the name the model sees
    names = [str(t.get("name")) for t in preview.tools if isinstance(t, dict)]
    key = server.replace("-", "_")
    matches = [n for n in names if n.startswith("mcp__") and n.endswith("__" + tool)]
    return next((n for n in matches if n.split("__", 2)[1] == key), matches[0] if matches else f"mcp__{key}__{tool}")


async def server_page(wf, name):
    preview = wf.shell.preview
    row = next((r for r in preview.mcp_servers if r.get("name") == name), {})
    result = await wf.client.mcp_server_show(wf.shell.controller.session, name)
    locked = bool(getattr(preview, "context_locked", False)) or bool(wf.agent_page_id)
    state = {str(t.get("name")): t.get("enabled") is not False for t in preview.tools if isinstance(t, dict)}
    info = result.server_info or {}
    lines = [f"{result.status} · {result.transport or '?'} · {result.scope} · loading {result.tool_loading} ({result.tool_loading_source})"]
    if info.get("name"):
        lines.append(f"server {info.get('name')} {info.get('version') or ''}".strip())
    lines.append(f"~{_compact_tokens(row.get('schema_tokens', 0))} full schemas · {len(result.tools)} tools")
    if result.error:
        lines.append(f"error: {result.error}")
    elif not result.tools and result.status != "connected":
        lines.append("not connected yet: no tools listed")
    if result.instructions:
        lines.append("Instructions (untrusted, from the server):")
        lines += [f"  {line}" for line in result.instructions.splitlines()[:12]]
    items, tools = [], list(result.tools)
    for tool in tools:
        full = _full_name(preview, name, str(tool.get("name")))
        items.append((str(tool.get("name")), {"kind": "tool_show", "name": full, "fallback": {**tool, "name": full}}))
    for res in result.resources:
        items.append((f"resource · {res.get('name') or res.get('uri')}", {"kind": "mcp_detail", "title": "Resource", "value": res}))
    for prompt in result.prompts:
        items.append((f"prompt · {prompt.get('name')}", {"kind": "mcp_detail", "title": "Prompt", "value": prompt}))
    wf.menu(f"MCP · {name} · {result.status}", items, lines, layout="list")
    for item, tool in zip(wf.shell.items, tools):
        full = item["operation"]["name"]
        item["trailing"] = f"~{_compact_tokens(int(tool.get('tokens') or 0))}"
        if full in state:
            on = state[full]
            item.update(toggle_enabled=on, toggle_locked=locked,
                        toggle_operation={"kind": "context_toggle", "category": "tools", "name": full, "enabled": not on})
