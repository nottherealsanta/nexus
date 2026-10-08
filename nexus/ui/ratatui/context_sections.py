"""Skills and MCP sections of the context header (docs/ratatui-parity.md; plans CONTEXT_SECTIONS_PLAN §2-3).

Skills are cards (``Item.lines`` carries the dim rows); Enter opens the skill page, Space/click
toggles. MCP mirrors the Tools list: one line per server with ``~indexed / ~full`` tokens, a
Restart chip (click it or Ctrl+R on the row) and the on/off toggle, under a Refresh row that
re-reads ``mcp.json`` through ``ExtensionsReload`` and reconnects servers that are not connected.
Enter opens the server page: the server row, then its index entry (what the model sees now), then
the full tool schemas, one line per tool; Enter on a tool opens the tool page. Everything is read
through host commands (``SkillInspect``, ``McpServerShow``, ``McpServerRestart``) and the
inspected request; nothing here reads files.
"""
from __future__ import annotations

import textwrap

from ...ui_support.context import _compact_tokens

CARD_WIDTH = 76  # wrap card rows here: the context dialog is at most 88 columns (render/dialogs.rs)
LIST_WIDTH = 62  # MCP rows sit in the thin list dialog, at most 72 columns with a 3-column indent


def wrapped(lines, width=CARD_WIDTH):
    """Wrap each card row, indenting continuations; nothing is cut."""
    out = []
    for line in lines:
        out += textwrap.wrap(line, width, subsequent_indent="  ", break_long_words=True) or [""]
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
    rows = [r for r in rows if isinstance(r, dict)]
    if category == "mcp":  # every server stays listed (off in mcp.json, invalid) so nothing is hidden; Global first
        return sorted(rows, key=lambda r: (r.get("scope", default) != "global", str(r.get("name") or r.get("id"))))
    rows = [r for r in rows if r.get("config_enabled") is not False]
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


def _mcp_index(preview) -> str:
    return getattr(preview, "mcp_index", "") or _part(preview, "mcp_index")


def server_index(index: str, name: str) -> list[str]:
    """This server's entry in the frozen MCP index: its ``- server:`` line and the indented lines under it."""
    out: list[str] = []
    for line in index.splitlines():
        if line.startswith("- server: "):
            if out:
                break
            if line[len("- server: "):].split(" · ", 1)[0] == name:
                out.append(line)
        elif out:
            if not line.startswith("  "):
                break
            out.append(line)
    return out


def _tokens(row, index) -> tuple[int, int]:
    """(indexed, full): the server's index entry as sent now, and every tool schema it offers."""
    from ...ui_support.context import estimate_tokens
    entry = server_index(index, str(row.get("name") or ""))
    return (estimate_tokens("\n".join(entry)) if entry else 0), int(row.get("schema_tokens") or 0)


def _pair(indexed, full) -> str:
    return f"~{_compact_tokens(indexed)} / ~{_compact_tokens(full)}"


def _broken(row) -> bool:
    """A server entry that failed to parse, or a whole-file problem: nothing to toggle or restart."""
    return bool(row.get("invalid") or row.get("file_error"))


def _mcp_lines(row) -> list[str]:
    if _broken(row):
        return wrapped([str(row.get("error") or "invalid entry")], LIST_WIDTH)
    target = row.get("url") or row.get("command_label")
    summary = " · ".join(str(b) for b in (row.get("transport"), target) if b)
    summary = " · ".join(b for b in (summary, f"{row.get('tool_count', 0)} tools") if b)
    error = [f"error: {row['error']}"] if row.get("error") else []
    return wrapped([summary, *error], LIST_WIDTH)


def _mcp_group(scope, rows) -> str:
    path = next((str(r["source_path"]) for r in rows if r.get("scope") == scope and r.get("source_path")), "")
    return f"{scope.title()} · {path or ('~/.nexus/mcp.json' if scope == 'global' else '.agents/mcp.json')}"


def mcp_list(wf):
    """Thin list like Tools: settings and refresh first, then Global and Project servers with their state."""
    preview = wf.shell.preview
    locked = bool(getattr(preview, "context_locked", False)) or bool(wf.agent_page_id)
    rows, index = _rows(preview, "mcp"), _mcp_index(preview)
    valid = [r for r in rows if not _broken(r)]
    on = [r for r in valid if r.get("enabled") is not False]
    open_settings = {"kind": "settings", "scope": "global", "category": "mcp"}
    items = [("Open MCP settings…", open_settings), ("↻ Refresh all · re-read mcp.json, reconnect servers", {"kind": "mcp_refresh"})]
    for row in rows:
        name, status = row.get("name") or row.get("id"), row.get("status") or "?"
        if _broken(row):
            label, operation = f"{name} · invalid", open_settings
        else:
            label = str(name) if status == "connected" else f"{name} · {status}"
            if row.get("config_enabled") is False:
                label += " · off in mcp.json"
            operation = {"kind": "context_extension_details", "category": "mcp", "name": name}
        items.append((label, operation))
    indexed = sum(_tokens(r, index)[0] for r in on)
    full = sum(r.get("schema_tokens", 0) for r in on)
    invalid = len(rows) - len(valid)
    title = (f"MCP · {len(on)} of {len(valid)} on" + (f" · {invalid} invalid" if invalid else "")
             + f" · ~{_compact_tokens(indexed)} indexed / ~{_compact_tokens(full)} full")
    note = ["Context locked after first turn" if locked else "Enter details · Space or click toggle · Ctrl+R restart"]
    wf.menu(title, items, note + ([] if rows else ["(none)"]), layout="list")
    shell_items = wf.shell.items
    shell_items[0].update(group="Configure")
    shell_items[1].update(group="Configure")
    for item, row in zip(shell_items[2:], rows):
        item.update(group=_mcp_group(row.get("scope") or "project", rows), lines=_mcp_lines(row))
        if _broken(row):
            continue
        if row.get("config_enabled") is False:
            _toggle(item, row, "mcp", True)  # a session switch cannot turn on what mcp.json turns off
            item.update(toggle_enabled=False)
        else:
            _toggle(item, row, "mcp", locked)
        item.update(trailing=_pair(*_tokens(row, index)),
                    action_label="Restart", action_operation={"kind": "mcp_restart", "name": row.get("name") or row.get("id")})


def section(wf, category):
    """The Skills card list or the MCP server list (replaces the generic extension picker)."""
    if category == "mcp":
        return mcp_list(wf)
    preview = wf.shell.preview
    locked = bool(getattr(preview, "context_locked", False)) or bool(wf.agent_page_id)
    rows = _rows(preview, category)
    on = [r for r in rows if r.get("enabled") is not False]
    items = []
    for row in rows:
        name = row.get("name") or row.get("id")
        items.append((f"{name}  {row.get('scope', 'global')}", {"kind": "context_extension_details", "category": category, "name": name}))
    index = _part(preview, "skills_index", "skills")
    from ...ui_support.context_header import estimate_tokens
    items.append((f"Show literal index (~{_compact_tokens(estimate_tokens(index))} tokens)", {"kind": "context_index", "category": category}))
    items.append(("Edit skills…", {"kind": "settings", "scope": "global", "category": category}))
    now = sum(r.get("context_tokens", 0) for r in on)
    title = f"Skills · {len(on)} of {len(rows)} on · ~{_compact_tokens(now)} in context"
    note = ["Context locked after first turn" if locked else "Enter opens · Space or click toggles"]
    if not rows:
        note.append("(none)")
    wf.menu(title, items, note, layout="context")
    for item, row in zip(wf.shell.items, rows):
        _toggle(item, row, category, locked)
        item["lines"] = wrapped(skill_card(row))


async def refresh(wf):
    """Re-read mcp.json, reconnect every enabled server that is not connected, redraw in place.

    ``ExtensionsReload`` adds, removes and reconfigures servers but skips a failed server until its
    backoff expires, so the not-connected ones get a ``McpServerRestart`` each (at most 64 rows).
    """
    import asyncio
    report = await wf.client.reload_extensions(trigger="mcp_refresh")
    failures = [f"{row.get('name', 'mcp')}: {row.get('error', '')}" for row in getattr(report, "failed", ()) or ()
                if isinstance(row, dict) and row.get("kind") == "mcp"]
    session = wf.shell.controller.session
    wf.shell.preview = await wf.client.inspect_context(session)
    stale = [str(r.get("name")) for r in _rows(wf.shell.preview, "mcp")
             if r.get("enabled") is not False and r.get("config_enabled") is not False and not _broken(r)
             and r.get("status") != "connected"][:64]
    results = await asyncio.gather(*(wf.client.mcp_server_restart(session, name) for name in stale), return_exceptions=True)
    for name, result in zip(stale, results):
        error = str(result) if isinstance(result, BaseException) else getattr(result, "error", "")
        if error:
            failures.append(f"{name}: {error}")
    wf.shell.mcp_due = 0.0  # the details sidebar re-reads server health too
    wf.shell.panel_title = ""  # same dialog, new rows: replace it rather than stacking
    await wf.context_extensions("mcp")
    servers = len([r for r in _rows(wf.shell.preview, "mcp") if not _broken(r)])
    summary = f"MCP refreshed · {servers} server{'s' if servers != 1 else ''}" + (f" · {len(stale)} reconnected" if stale else "")
    if failures:
        wf.shell.flash(f"{summary} · {len(failures)} problem(s): {failures[0]}", "warning")
    else:
        wf.shell.flash(summary, "success")


async def restart(wf, name, page=False):
    """Restart one server, then redraw whichever MCP view asked."""
    result = await wf.client.mcp_server_restart(wf.shell.controller.session, name)
    wf.shell.mcp_due = 0.0
    wf.shell.panel_title = ""
    if page:
        wf.shell.preview = await wf.client.inspect_context(wf.shell.controller.session)
        await server_page(wf, name)
    else:
        await wf.context_extensions("mcp")
    if result.error:
        wf.shell.flash(f"{name} · restart failed: {result.error}", "error")
    else:
        wf.shell.flash(f"{name} restarted · {result.status}", "success")


def literal_index(wf, category):
    preview = wf.shell.preview
    text = _part(preview, "skills_index", "skills") if category == "skills" else _mcp_index(preview)
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
    """One server: its row (Restart, on/off), its index entry on top, then each full tool schema on one line."""
    preview = wf.shell.preview
    row = next((r for r in preview.mcp_servers if r.get("name") == name), {"name": name})
    result = await wf.client.mcp_server_show(wf.shell.controller.session, name)
    locked = bool(getattr(preview, "context_locked", False)) or bool(wf.agent_page_id)
    state = {str(t.get("name")): t.get("enabled") is not False for t in preview.tools if isinstance(t, dict)}
    info = result.server_info or {}
    about = " ".join(str(info.get(k) or "") for k in ("name", "version")).strip()
    entry = server_index(_mcp_index(preview), name)
    indexed, full = _tokens(row, _mcp_index(preview))
    loading = "deferred to McpSearch" if result.tool_loading == "search" else "sent every request"
    ignored = ", ".join(str(k) for k in row.get("ignored_keys") or ())
    filters = " · ".join(f"{label} {', '.join(map(str, row[key]))}" for key, label in (("include_tools", "include"), ("exclude_tools", "exclude")) if row.get(key))
    server = {"name": name, "status": result.status, "transport": result.transport, "command": row.get("command_label"),
              "url": row.get("url"), "defined in": row.get("source_path"), "tool filters": filters,
              "ignored keys (accepted from another client's format, not acted on)": ignored,
              "scope": result.scope, "tool loading": f"{result.tool_loading} ({result.tool_loading_source})",
              "server": about, "error": result.error,
              "instructions (untrusted, from the server)": result.instructions}
    lines = [f"{result.status} · {result.transport or '?'} · {result.scope} · loading {result.tool_loading}"]
    if result.error:
        lines.append(f"error: {result.error}")
    head_label = " · ".join(b for b in (result.status, result.transport or "?", result.scope, about) if b)
    items = [(head_label, {"kind": "mcp_detail", "title": "Server", "value": server})]
    if entry:
        items.append((entry[0], {"kind": "context_index", "category": "mcp"}))
    else:
        items.append((f"not in the index: only connected servers are listed ({result.status})", {"kind": "context_index", "category": "mcp"}))
    tools = list(result.tools)
    for tool in tools:
        full_name = _full_name(preview, name, str(tool.get("name")))
        items.append((str(tool.get("name")), {"kind": "tool_show", "name": full_name, "fallback": {**tool, "name": full_name}, "server": name}))
    for res in result.resources:
        items.append((str(res.get("name") or res.get("uri")), {"kind": "mcp_detail", "title": "Resource", "value": res}))
    for prompt in result.prompts:
        items.append((str(prompt.get("name")), {"kind": "mcp_detail", "title": "Prompt", "value": prompt}))
    if not tools:
        items.append(("no tools listed · Restart reconnects", {"kind": "mcp_restart", "name": name, "page": True}))
    items.append(("Open MCP settings…", {"kind": "settings", "scope": "global", "category": "mcp"}))
    wf.menu(f"MCP · {name} · ~{_compact_tokens(indexed)} indexed / ~{_compact_tokens(full)} full", items, lines, layout="list")
    head, index_row, *rest = wf.shell.items
    _toggle(head, row, "mcp", locked or row.get("config_enabled") is False)  # off in mcp.json: the session switch cannot enable it
    head["toggle_operation"]["page"] = True
    head.update(group="Server", action_label="Restart", action_operation={"kind": "mcp_restart", "name": name, "page": True},
                lines=wrapped([f"error: {result.error}"], LIST_WIDTH) if result.error else [])
    index_row.update(group=f"Indexed · ~{_compact_tokens(indexed)} tokens in context now", lines=wrapped(entry[1:], LIST_WIDTH))
    full_group = f"Full · ~{_compact_tokens(full)} tokens · {len(tools)} tools · {loading}"
    for item, tool in zip(rest, tools):
        full_name = item["operation"]["name"]
        item.update(group=full_group, trailing=f"~{_compact_tokens(int(tool.get('tokens') or 0))}")
        if full_name in state:
            enabled = state[full_name]
            item.update(toggle_enabled=enabled, toggle_locked=locked,
                        toggle_operation={"kind": "context_toggle", "category": "tools", "name": full_name,
                                          "enabled": not enabled, "server": name})
    for item in rest[len(tools):]:
        kind = item["operation"].get("title")
        item["group"] = {"Resource": "Resources", "Prompt": "Prompts"}.get(kind, full_group)
    rest[-1]["group"] = "Configure"  # the Open MCP settings… row closes the page
