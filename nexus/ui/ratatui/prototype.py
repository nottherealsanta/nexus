"""Python reducer/host adapter for the native prototype (feasibility §4).

Private newline-delimited snapshots go to Rust stdin; typed actions return on
stdout. Rust renders exclusively on stderr. Closing the UI never cancels a turn.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

from ...ui.cli import open_client
from ...ui_support.prompts import approval_choices, pending_questions
from .actions import ShellActions, labelled
from ...ui_support.context import context_usage, context_measure, price_tier_thresholds
from ...ui_support.completion import root_agents
from ...ui_support.prompt_history import load_history
from .controller import NativeController as TuiController
from ...ui_support.text import escape_controls, sanitize
from ...ui_support.tool_details import sections_to_text


_OUTPUT_SECTIONS = frozenset({"Result", "Summary", "Error", "Progress"})
_EXPAND_AFTER_LINES = 12  # outputs longer than this expand in place on click


def _display_breadcrumb(breadcrumb: str, home: Path | None = None) -> str:
    """Abbreviate only the actual home component; keep the source path intact."""
    home_text = str(Path.home() if home is None else home).rstrip("/")
    path, separator, branch = breadcrumb.partition(" › ")
    # Root is not a useful home abbreviation, nor is a relative home path.
    if home_text and Path(home_text).is_absolute():
        if path == home_text:
            path = "~"
        elif path.startswith(home_text + "/"):
            path = "~" + path[len(home_text):]
    return path + separator + branch


#: Read-only lookups that collapse into one `Explored: 1 search, 1 read` row.
_EXPLORE_KINDS = {"grep": ("search", "searches"), "glob": ("search", "searches"),
                  "read": ("read", "reads"), "ls": ("list", "lists")}


def operation_allowed(shell, allowed: set, operation) -> bool:
    """May the client run ``operation``? Only operations this snapshot offered: list rows, transcript
    blocks, and the controls of the open Settings page (an offered operation the client completed with
    a valid ``value``, ``action`` or ``index``)."""
    import json
    from ...ui_support import settings_page as sp
    if not operation:
        return False
    if json.dumps(operation, sort_keys=True) in allowed:
        return True
    workflows = shell.workflows
    page = workflows.settings_page if shell.panel_title and shell.panel_title == workflows.page_title else None
    return sp.accepts(page, operation)


def _block_operations(blocks):
    """Allow only actions present in the current projected transcript, including chips."""
    for block in blocks:
        for key in ("operation", "output_operation", "chip_operation"):
            if operation := block.get(key):
                yield operation
        yield from _block_operations(block.get("members", []))


def _explored_summary(tools) -> str:
    """`1 search, 2 reads` when every call is a lookup, else empty."""
    kinds = [_EXPLORE_KINDS.get(tool.name.casefold()) for tool in tools]
    if not kinds or not all(kinds):
        return ""
    counts = {}
    for one, many in kinds:
        counts[(one, many)] = counts.get((one, many), 0) + 1
    return ", ".join(f"{n} {one if n == 1 else many}" for (one, many), n in counts.items())


def _thought_took(ms: int) -> str:
    """`671ms`, `4.2s`, `1m 5s`."""
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms // 60_000}m {(ms // 1000) % 60}s"

#: Projected-turn cache bound, shared by every open tab (keys are namespaced by session).
TURN_CACHE_BYTES = 64 * 1024 * 1024


def _revision(block):
    import hashlib
    return hashlib.blake2s(json.dumps({key: value for key, value in block.items() if key != "rev"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(), digest_size=8).hexdigest()


def _safe_blocks(blocks):
    result = []
    for block in blocks:
        safe = {**block, **{key: escape_controls(block[key]) for key in ("title", "text", "path", "detail", "color", "local_detail", "local_preview", "fold_summary", "heading") if key in block}}
        if block.get("chips"):
            safe["chips"] = [escape_controls(chip) for chip in block["chips"]]
        if "members" in block:
            safe["members"] = _safe_blocks(block["members"])
        safe["rev"] = _revision(safe)
        result.append(safe)
    return result


def _fold_summary(turn) -> str:
    """The muted line under a folded turn's prompt: tool calls, tokens, model."""
    from ...ui_support.context import _compact_tokens
    from ...ui_support.timeline import _turn_models
    count = len(turn.tools)
    parts = [f"{count} tool{'s' if count != 1 else ''}"]
    if turn.usage.total_tokens:
        parts.append(f"{_compact_tokens(turn.usage.total_tokens)} tokens")
    model = _turn_models(turn).split(", ")[0].rsplit("/", 1)[-1]
    if model != "unknown":
        parts.append(model)
    return " · ".join(parts)


def _project_turn(turn, shell, agents=None, literal=True):
    """Blocks of one turn in canonical transcript order and spacing.

    Each block carries ``gap`` (blank rows before it, after margin collapsing),
    so Rust only draws. Reply/tool/thought order follows the durable event seq.
    """
    from ...ui_support.timeline import (
        BATCH_GLYPHS, _has_message_content, _literal, submitted_attachment_summary,
        thought_title, tool_batches, tool_status, turn_footer_text,
        _turn_duration, _text, turn_agent_label)
    local_ui = bool(shell and getattr(shell, "local_transcript", False))
    collapsed = bool(not local_ui and shell and turn.id in shell.collapsed_turns)
    lines = [f"\nTurn {turn.index} · {turn.phase}"] if literal else []
    emit = lines.append if literal else (lambda _line: None)
    entries = []  # (seq, rank, block, top_margin, bottom_margin)
    for retry in turn.retries:
        if retry.reason == "provider_overloaded":
            entries.append((retry.event_seq, 1, {"id": f"{turn.id}:retry:{retry.event_seq}", "kind": "literal",
                "title": "Provider overloaded", "text": f"Retry {retry.attempt}/3 after {retry.delay_seconds}s"}, 1, 1, "retry"))
    for index, message in enumerate(turn.messages):
        if message.role == "user":
            if not _has_message_content(message):
                continue
            prompt, chips = submitted_attachment_summary(message)
            text = _literal(prompt)
            first, _, rest = text.partition("\n")
            emit(f"user · text\n{message.text}")
            if collapsed and rest:
                first, rest = first + " …", ""
            block = {"id": message.id + ":user", "kind": "user", "title": first, "text": rest,
                     "color": turn_agent_label(turn)[1] or _agent_color(shell, getattr(getattr(shell, "controller", None), "agent_name", "build")),
                     "number": turn.index + 1, "collapsed": collapsed,
                     "chips": [] if collapsed else chips,
                     "operation": {"kind": "turn_toggle", "id": turn.id}}
            if chips and not collapsed:
                block["chip_operation"] = {"kind": "message_page", "id": message.id}
            entries.append((message.event_seq, 2, block, 0, 0, "user"))
            continue
        if message.role == "assistant" and any(block.kind == "thinking" for block in message.blocks):
            thinking = message.thinking
            if thinking.strip():
                expanded = bool(not local_ui and shell and message.id + "thinking" in shell.expanded)
                count = len([line for line in thinking.splitlines() if line.strip()])
                # `Thought: 671ms`; the reasoning opens beneath it. Collapsed, the first
                # sentence and the line count announce what is hidden.
                took = message.thinking_ms
                title = "Thought" if took is None else f"Thought: {_thought_took(took)}"
                suffix = "" if expanded else (f"{thought_title(thinking)} · {count} lines ▸" if count > 1 else f"{thought_title(thinking)} ▸")
                block = {"id": message.id + "thinking", "kind": "thought", "title": title,
                         "text": suffix, "detail": _literal(thinking) if expanded and not local_ui else "",
                         **({"local_ui": True, "local_detail": _literal(thinking), "local_open": False} if local_ui else {}),
                         "operation": {"kind": "block_toggle", "id": message.id + "thinking"}}
                entries.append((message.event_seq, 0, block, 1, 1, "thought"))
                emit(f"assistant · thinking\n{thinking}")
        if message.text if message.role == "assistant" else _has_message_content(message):
            block = {"id": message.id + "text", "kind": "markdown", "text": message.text}
            entries.append((message.event_seq, 2, block, 0, 1, "assistant"))
            emit(f"assistant · text\n{message.text}")
    batches = tool_batches(turn.tools)
    for tool in turn.tools:
        # Rows open nothing by default; only a long output expands in place (`out_id`),
        # and only a spawned subagent opens a page of its own.
        from .transcript_data import tool_presentation
        sections, cached_row, cached_heading, cached_summary = tool_presentation(tool, shell)
        out_lines = sum(len(row.value.splitlines()) or 1 for section in sections
                        if section.title in _OUTPUT_SECTIONS for row in section.rows)
        out_id = tool.call_id + ":output"
        expandable = out_lines > _EXPAND_AFTER_LINES
        expanded = bool(not local_ui and shell and (shell.verbose or out_id in shell.expanded))
        detail_open = bool(not local_ui and shell and (shell.verbose or tool.call_id + ":detail" in shell.expanded))
        body = sections_to_text(sections) if local_ui or literal or detail_open else ""
        full_body = body
        output_end = 0
        if local_ui and out_lines > _EXPAND_AFTER_LINES:
            rows = body.splitlines()
            output_start = next((i for i, row in enumerate(rows) if row.strip().rstrip(":").title() in _OUTPUT_SECTIONS), len(rows))
            output_end = min(len(rows), output_start + _EXPAND_AFTER_LINES + 1)
        if detail_open and not expanded and out_lines > _EXPAND_AFTER_LINES:
            body_rows = body.splitlines()
            output_start = next((i for i, row in enumerate(body_rows) if row.strip().rstrip(":").title() in _OUTPUT_SECTIONS), len(body_rows))
            end = output_start + _EXPAND_AFTER_LINES + 1
            if end < len(body_rows):
                body = "\n".join(body_rows[:end] + [f"… {len(body_rows) - end} more lines · Enter for all"])

        emit(body)
        glyph = BATCH_GLYPHS.get(batches.get(tool.call_id, ""))
        operation = {"kind": "block_toggle", "id": tool.call_id + ":detail"}
        if tool.name.casefold() in {"task", "subagent"}:
            from ...ui_support.timeline import _task_children, _task_header
            child = next(iter(_task_children(tool, agents or {})), None)
            # One row: `<spinner|✓> Explore Subagent — phrase · model (effort) · time`;
            # the tick takes the muted tool tone, never green.
            head, running = _task_header(tool, child, 0)
            name, _, phrase = head.partition(" ")[2].partition(" · ")
            phrase = " ".join(phrase.split())[:64]
            text = f"{SPINNER_SLOT if running else '✓'} {name} Subagent — {phrase}"
            if child is not None and child.model:
                effort = next((t.reasoning_effort for t in child.body.turns if t.reasoning_effort), None)
                text += f" · {child.model.rsplit('/', 1)[-1]}" + (f" ({effort})" if effort else "")
            if not running and child is not None and child.spawned_ts is not None:
                end = child.completed_ts
                if end is not None:
                    elapsed_ms = max(0, int((end - child.spawned_ts) * 1000))
                    text += f" · {_thought_took(elapsed_ms)}"
            operation = {"kind": "agent_page", "id": child.id} if child is not None else None
        else:
            text = cached_row
            if expandable:
                first, _, rest = text.partition("\n")
                text = f"{first} · {out_lines} lines {'▾' if expanded else '▸'}" + (f"\n{rest}" if rest else "")
        if tool_status(tool) == "running":  # the native client animates this slot (docs/ratatui-parity.md)
            text = text.replace(SPINNER_FRAMES[0], SPINNER_SLOT, 1)
        block = {"id": tool.call_id, "kind": "tool", "status": tool_status(tool), "text": text,
                 "batch_glyph": glyph or "",
                 "detail": body if detail_open else "", "operation": operation,
                 "output_operation": {"kind": "block_toggle", "id": out_id} if (detail_open or local_ui) and expandable else None}
        if local_ui:
            block.update(local_ui=True, local_detail=full_body, fold_lines=output_end, local_open=False)
        # A subagent row stands apart: one blank row above and below.
        entries.append((tool.event_seq, 3, block, 0, 0, "tool"))
        if tool.diff and (local_ui or literal or detail_open):
            from ...ui_support.timeline import diff_sections, diff_split_rows, split_diff_files
            hunks = dict(split_diff_files(tool.diff))
            for diff in diff_sections(tool.diff):
                rows = [[old_no, escape_controls(old_text), new_no, escape_controls(new_text), kind]
                        for old_no, old_text, new_no, new_text, kind in diff_split_rows(hunks.get(diff.path, ""))]
                entries.append((tool.event_seq, 3, {"id": tool.call_id + diff.path, "kind": "diff", "title": diff.path,
                    "path": diff.path, "added": diff.added, "removed": diff.removed, "diff_rows": rows,
                    "operation": None}, 1, 1, "diff"))
    if turn.terminal and turn.error:
        entries.append((max((entry[0] for entry in entries), default=0) + 1, 4,
                        {"id": turn.id + ":error", "kind": "error", "text": "Error: " + _text(turn.error)}, 1, 0, "error"))
        emit(f"Error: {turn.error}")
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    # Activity runs are delimited by visible replies, not by tool name or reasoning.
    # Identity follows the first durable member, so streaming additions do not
    # reset local disclosure. Diffs stay attached to their tool's full detail.
    tools_by_id = {tool.call_id: tool for tool in turn.tools}
    diff_members = {tool.call_id: [row[2] for row in entries
        if row[5] == "diff" and row[0] == tool.event_seq
        and row[2]["id"].startswith(tool.call_id)] for tool in turn.tools}
    grouped, pending = [], []

    def flush_activity():
        if not pending:
            return
        seq, rank, first, _, _, _ = pending[0]
        group_id = turn.id + ":activity:" + first["id"]
        opened = bool(not local_ui and shell and (shell.verbose or group_id in shell.expanded))
        members, counts = [], {}
        for _, _, block, _, _, role in pending:
            member = block
            if role == "thought":
                category = ("Thought", "time", "times")
            else:
                tool = tools_by_id[block["id"]]
                name = tool.name.casefold()
                category = {
                    "bash": ("Ran", "command", "commands"),
                    "run": ("Ran", "command", "commands"),
                    "read": ("Read", "file", "files"),
                    "edit": ("Edited", "file", "files"),
                    "write": ("Wrote", "file", "files"),
                    "apply_patch": ("Applied", "patch", "patches"),
                    "grep": ("Searched", "time", "times"),
                    "glob": ("Searched", "time", "times"),
                }.get(name, (tool.name.title(), "call", "calls"))
                _, _, heading_full, summary = tool_presentation(tool, shell)
                heading = heading_full.split(" ", 1)[-1]
                if tool.diff:
                    from ...ui_support.timeline import diff_sections
                    diffs = diff_sections(tool.diff)
                    summary = f"+{sum(d.added for d in diffs)} −{sum(d.removed for d in diffs)}"
                if name not in {"task", "subagent"}:
                    member = {**block, "heading": heading_full + (f" · {summary}" if summary else ""),
                        "title": tool.name.title(), "text": heading.removeprefix(tool.name.title()).strip() + (f" · {summary}" if summary else ""),
                        "batch_glyph": "∥" if block.get("batch_glyph") else ""}
                member = {**member, "members": diff_members[tool.call_id]}
            counts[category] = counts.get(category, 0) + 1
            members.append(member)
        text = " · ".join(f"{verb} {count} {one if count == 1 else many}"
                          for (verb, one, many), count in counts.items())
        running = any(member.get("status") == "running" for member in members)
        grouped.append((seq, rank, {"id": group_id, "kind": "tool_group", "text": text,
            "color": _agent_color(shell, getattr(getattr(shell, "controller", None), "agent_name", "build")),
            "status": "running" if running else "completed", "count": len(members), "collapsed": not opened,
            "members": members if opened or local_ui else [],
            **({"local_ui": True, "local_open": False} if local_ui else {}),
            "operation": {"kind": "block_toggle", "id": group_id}}, 1, 1, "tool"))
        pending.clear()

    for entry in entries:
        if entry[5] == "diff":
            continue
        if entry[5] in {"tool", "thought"} and not (
            entry[5] == "tool" and tools_by_id[entry[2]["id"]].name.casefold() in {"task", "subagent"}
        ):
            pending.append(entry)
        else:
            flush_activity()
            grouped.append(entry)
    flush_activity()
    # Only the trailing activity of an active turn previews automatically. Replies
    # close the previous run; explicit local expansion still reveals every item.
    if grouped and grouped[-1][2]["kind"] == "tool_group" and not turn.terminal:
        grouped[-1][2]["preview_limit"] = 5
    entries = grouped
    if collapsed:
        entries = [entry for entry in entries if entry[5] == "user"]
    blocks, previous = [], None
    for _, _, block, top, bottom, role in entries:
        if previous is not None:
            if previous[0] == "user":
                top = max(top, 1)
            elif (previous[0] == "tool" and role in {"assistant", "agent"}) or (previous[0] == "assistant" and role == "tool"):
                top = max(top, 1)
            block = {**block, "gap": max(previous[1], top)}
        blocks.append(block)
        previous = (role, 0 if role == "assistant" else bottom)
    if collapsed:
        reply = next((message.text.splitlines()[0] for message in turn.messages if message.role == "assistant" and message.text), "")
        metrics = " · ".join(part for part in (str(turn.usage.total_tokens) + " tokens" if turn.usage.total_tokens else "", _turn_duration(turn) or "") if part)
        blocks.append({"id": turn.id + ":collapsed", "kind": "collapsed", "gap": 0,
                       "title": f"{len(turn.tools)} tools · {_text(reply, 120)}", "text": metrics})
    elif turn.terminal:
        footer = turn_footer_text(turn)
        if footer:
            last, bottom = previous if previous else ("", 0)
            top = 0 if last in {"assistant", "diff"} else 1
            blocks.append({"id": turn.id + ":summary", "kind": "summary", "text": footer, "gap": max(top, bottom)})
    if local_ui:
        summary = _fold_summary(turn)
        for block in blocks:
            block["turn_id"] = turn.id
            if block["kind"] == "user":
                block.update(local_ui=True, fold_summary=summary)
    return [escape_controls(line) for line in lines], _safe_blocks(blocks)


def _agent_color(shell, name):
    from ...ui_support.context_header import agent_color
    identity = getattr(getattr(shell, "preview", None), "agent", None)
    identity = identity if isinstance(identity, dict) and identity.get("name") == name else getattr(shell, "agent_definitions", {}).get(name, {})
    return identity.get("color") or agent_color(name)


def _context_blocks(shell, view):
    """The request-context header that opens every conversation."""
    from ...ui_support.context_header import NEUTRAL, HeaderBlock, header_blocks
    from ...ui_support.context import _compact_tokens
    preview = shell.preview
    if preview is not None and hasattr(preview, "tools"):
        color = _agent_color(shell, getattr(getattr(shell, "controller", None), "agent_name", (getattr(preview, "agent", {}) or {}).get("name", "build")))
        found = header_blocks(preview, color)
    else:
        found = [HeaderBlock(key, label, "", "", None, NEUTRAL) for key, label in (
            ("system", "System prompt"), ("tools", "Tools"), ("agents", "AGENTS.md"), ("skills", "Skills"), ("mcp", "MCP"))]
    def token_status(block):
        if block.key == "skills":
            return (f"Included index · ~{_compact_tokens(block.tokens)} tokens"
                    if block.tokens is not None else "Included index · tokens unknown")
        return f"~{_compact_tokens(block.tokens)} tokens" if block.tokens is not None else "tokens unknown"

    return [{"id": "context:" + block.key, "kind": "context", "title": block.label,
             "text": "\n".join(block.inventory) if block.key in {"tools", "skills", "mcp"} else block.body,
             "local_preview": block.inventory_note,
             "status": token_status(block), "color": _agent_color(shell, getattr(getattr(shell, "controller", None), "agent_name", (getattr(preview, "agent", {}) or {}).get("name", "build"))),
             "gap": 1, "operation": {"kind": "context_show", "key": block.key}}
            for block in found]


def _compact_header(shell, view):
    """Compact chip strip with a right-aligned estimated-token footer."""
    from nexus.ui_support.context_header import header_blocks

    chips = _context_blocks(shell, view)
    preview = shell.preview

    def scoped(rows, default):
        rows = [r for r in rows if isinstance(r, dict) and r.get("enabled") is not False and r.get("config_enabled") is not False]
        project = sum(r.get("scope", default) == "project" for r in rows)
        return [project, len(rows) - project]

    counts = {"context:tools": [sum(1 for t in getattr(preview, "tools", []) if not isinstance(t, dict) or t.get("enabled") is not False)],
              "context:skills": [sum(scoped(getattr(preview, "skills_index", []), "global"))],
              "context:mcp": [sum(scoped(getattr(preview, "mcp_servers", []), "project"))]}
    # Every block shows what it holds: inventories for tools/skills/MCP, a one-line
    # preview for the system prompt and AGENTS.md ("None included" once a preview exists).
    def shown(chip):
        if chip["id"] in {"context:system", "context:agents"} and preview is not None and not chip["text"]:
            return "None included"
        return chip["text"]

    chips = [{**chip, "text": shown(chip), "counts": counts.get(chip["id"], []) if preview else [], "gap": 0} for chip in chips]
    total = "Context total · tokens unavailable"
    if preview is not None and hasattr(preview, "tools"):
        total = f"Context total · ~{sum(block.tokens or 0 for block in header_blocks(preview, chips[0]["color"])):,} tokens"
    return [{"id": "context:header", "kind": "context_header", "gap": 1, "color": chips[0]["color"],
             "members": chips, "operation": {"kind": "context_menu"}},
            {"id": "context:total", "kind": "summary", "text": total}]


def _modified_files_memo(shell, view, modified_files):
    """``modified_files`` walks every tool of the session; reuse it until a turn changes.

    Turns are replaced, never mutated, so the identity of each turn (the root's and
    one level of subagents') is the change key.
    """
    if shell is None:
        return modified_files(view)
    turns = (*view.turns, *(turn for agent in view.agents.values() for turn in agent.body.turns))
    memo = getattr(shell, "files_memo", None)
    if memo and len(memo[0]) == len(turns) and all(a is b for a, b in zip(memo[0], turns)):
        return memo[1]
    files = modified_files(view)
    shell.files_memo = (turns, files)
    return files


def _details_panel(controller, view, shell):
    """The terminal details sidebar (SESSION, MODIFIED FILES, MCP SERVERS) as data."""
    from ...ui_support.details import diff_preview_lines, mcp_rows, modified_files, session_rows
    rows = session_rows(view, phase=view.phase, agent=getattr(controller, "agent_name", "build"),
                        model=getattr(controller, "model", None) or "default",
                        effort=getattr(controller, "reasoning_effort", None))
    files = _modified_files_memo(shell, view, modified_files)
    added, removed = sum(f.added for f in files), sum(f.removed for f in files)
    report = getattr(shell, "mcp_report", None) or {}
    daemon = report.get("daemon") or {}
    # Doctor reports configuration; context inspection owns the session mode.
    preview = getattr(shell, "preview", None)
    session_modes = {row["name"]: row for row in getattr(preview, "mcp_servers", ())}
    mcp_report = {**report, "mcp": {**(report.get("mcp") or {}), "servers": [
        {**row, **({"tool_loading": session_modes[row["name"]]["tool_loading"]}
                  if row.get("name") in session_modes else {})}
        for row in (report.get("mcp") or {}).get("servers", ())]}}
    from importlib.metadata import version
    from datetime import datetime
    def stamp(value):
        try:
            return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S") if value is not None else "unavailable"
        except (TypeError, ValueError, OverflowError, OSError):
            return "unavailable"
    selected = next((row for row in getattr(shell, "sessions", []) if row["id"] == getattr(controller, "session", view.session_id)), {})
    rows = [("ID", str(getattr(controller, "session", view.session_id))),
            ("Title", selected.get("title") or getattr(controller, "title", None) or "Untitled"), *rows,
            ("Provider", getattr(controller, "provider", None) or "unavailable"),
            ("Cost", str(getattr(view.usage, "cost", None) or "unavailable")),
            ("Created", stamp(view.turns[0].started_ts) if view.turns else "unavailable"),
            ("Updated", stamp(view.turns[-1].updated_ts) if view.turns else "unavailable"),
            ("Workspace", str(getattr(shell, "workspace", "") or "unavailable")),
            ("Branch", str((report.get("git") or {}).get("branch") or "unavailable"))]
    return {
        "tab": shell.preferences.values["details_tab"] if shell else "Session",
        "logs_header": [["Session", str(getattr(controller, "session", view.session_id))], ["Daemon pid", str(daemon.get("pid") or "unavailable")],
                        ["Socket", escape_controls(str(daemon.get("socket") or "unavailable"))],
                        ["Client", str(getattr(getattr(getattr(controller, "client", None), "health", None), "version", "unavailable"))],
                        ["Bridge", "3 (schema 1/2 compatible)" if shell and getattr(shell, "local_transcript", False) else "Desktop schema 3"], ["Nexus", version("nexus-harness")],
                        *[["Timing", line] for line in (getattr(getattr(shell, "logs", None), "trace", []) + getattr(getattr(shell, "logs", None), "python_trace", []))]],
        "session": [[label, escape_controls(value)] for label, value in rows],
        "files": [{"path": escape_controls(f.path), "added": f.added, "removed": f.removed, "created": f.created,
                   "open": (is_open := bool(shell and f.path in shell.open_files)),
                   "diff": [escape_controls(line) for line in diff_preview_lines(f.hunks)] if is_open or bool(shell and getattr(shell, "local_transcript", False)) else []} for f in files],
        "files_summary": f"+{added} -{removed} across {len(files)} file{'s' if len(files) != 1 else ''}" if files else "",
        "mcp": [[tone, escape_controls(text), escape_controls(note)]
                for tone, text, note in mcp_rows(mcp_report, error=getattr(shell, "mcp_error", None))],
    }


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_SLOT = "\ue000"  # private-use placeholder; Rust draws the current frame here


def _settings_nav(shell):
    """The Settings area list (left pane) while a Settings page is open; ``None`` otherwise."""
    from ...ui_support.settings_help import SETTINGS_SECTIONS
    if not shell:
        return None
    if not shell.panel_title:
        shell.settings_nav = None  # the panel closed: the list goes with it
    if shell.settings_nav is None:
        return None
    selected = next((i for i, (key, _) in enumerate(SETTINGS_SECTIONS) if key == shell.settings_nav), -1)
    return {"items": [[label, key or "", key is None] for key, label in SETTINGS_SECTIONS], "selected": selected}


def _context_display_view(view, shell):
    """Fill missing durable accounting from the host's current request preview."""
    preview = getattr(shell, "preview", None)
    accounting = dict(getattr(preview, "request_context", {}) or {})
    durable = view.context if isinstance(view.context, dict) else {}
    durable = durable.get("context", durable)
    if isinstance(durable, dict):
        accounting.update({key: value for key, value in durable.items() if value is not None})
    return SimpleNamespace(context=accounting)


def _context_label(view):
    """Used tokens and occupancy of each reported price tier/window."""
    used, _window, _measured = context_measure(view)
    def compact(value):
        if value is None:
            return "unavailable"
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
        return str(value)
    limits = _context_tiers(view)
    return " · ".join([compact(used), *[f"{used / limit * 100:.1f}%" for limit in limits if used is not None]])


def _context_tiers(view):
    _, window, _ = context_measure(view)
    boundaries = price_tier_thresholds(view)
    return ([boundaries[0]] if boundaries and boundaries[0] != window else []) + ([window] if window else [])


def _queue_lines(view):
    """Messages waiting for the running turn,."""
    from ...ui_support.text import sanitize
    queue = list(view.input_queue)
    rows = [f"{'Queued' if item.mode == 'queue' else item.mode.title()} · " + sanitize("".join(
        block.get("text", "") for block in item.content if isinstance(block, dict)), 160) for item in queue[:3]]
    if len(queue) > 3:
        rows.append(f"+{len(queue) - 3} more queued")
    return [escape_controls(row) for row in rows]


def _tab_rows(controller, shell):
    """Tabs with the current one marked; the current tab reads "working" while its turn runs."""
    rows = []
    for tab in shell.tabs:
        current = tab["id"] == controller.session and tab.get("workspace") == shell.workspace
        status = tab.get("status", "")
        if current and getattr(controller, "running", False):
            status = "working"
        elif current:
            status = status if status == "input" else ""
        rows.append({**tab, "active": current, "status": status})
    return rows


def _guarded(failures: list[str], label: str, build, fallback):
    """Run one projection section; a failure becomes a labelled notice, not a dead UI."""
    try:
        return build()
    except Exception as exc:
        failures.append(f"{label} could not be shown: {type(exc).__name__}: {exc}")
        return fallback


def _child_turn_blocks(shell, agent, turn, flags):
    """Control-safe, hashed blocks of one subagent turn, reused until the turn changes.

    Shares the root's bounded ``turn_cache`` (keys are namespaced by agent id), so a
    streamed child token re-projects one turn rather than the whole child page.
    """
    body = agent.body
    deps = tuple((tool.call_id, tuple(id(body.agents.get(child)) for child in tool.child_agent_ids))
                 for tool in turn.tools if tool.child_agent_ids)
    key, turn_flags = f"{getattr(shell.controller, 'session', '')}\x00{agent.id}\x00{turn.id}", (*flags, deps)
    cached = shell.turn_cache.get(key)
    if cached and cached[0] is turn and cached[1] == turn_flags:
        return cached[3]
    _, projected = _project_turn(turn, shell, body.agents, literal=False)
    blocks = _safe_blocks([b for b in projected if b.get("kind") != "user"])
    if cached:
        shell.turn_cache_bytes -= cached[4]
    size = len(json.dumps(blocks, ensure_ascii=False).encode())
    shell.turn_cache[key] = (turn, turn_flags, [], blocks, size)
    shell.turn_cache.move_to_end(key)
    shell.turn_cache_bytes += size
    while len(shell.turn_cache) > 4096 or shell.turn_cache_bytes > TURN_CACHE_BYTES:
        _, removed = shell.turn_cache.popitem(last=False)
        shell.turn_cache_bytes -= removed[4]
    return blocks


def project(controller: TuiController, revision: int, error: str = "", shell=None, literal: bool = True) -> dict:
    """Project labelled, control-safe context and transcript from canonical state.

    Each section is built independently: one failing section is replaced by a
    labelled notice and the others still render.
    """
    view = controller.view
    failures: list[str] = []
    lines = []
    blocks = []
    context_lines = []
    # An open subagent page replaces every root block and the details panel below,
    # so the live (non-literal) projection does not build them.
    on_agent_page = bool(not literal and shell and shell.workflows.agent_page_id and shell.workflows.selected_agent is not None)
    if shell and shell.preferences.values["context_preview"] and not on_agent_page:
        blocks.extend(_guarded(failures, "Context header", lambda: _safe_blocks(_compact_header(shell, view)), []))
    flags = (_agent_color(shell, getattr(controller, "agent_name", "build")), bool(shell and shell.verbose and not getattr(shell, "local_transcript", False)), frozenset(shell.expanded) if shell and not getattr(shell, "local_transcript", False) else frozenset(), frozenset(shell.collapsed_turns) if shell and not getattr(shell, "local_transcript", False) else frozenset(), bool(shell and getattr(shell, "local_transcript", False)), literal)
    dependencies = {}
    for agent in view.agents.values():
        dependencies.setdefault(agent.parent_call_id, []).append(id(agent))
    for turn in () if on_agent_page else view.turns:
        turn_flags = (*flags, tuple((tool.call_id, tuple(dependencies.get(tool.call_id, ())),
                                    tuple(id(view.agents.get(child)) for child in tool.child_agent_ids))
                                   for tool in turn.tools if tool.child_agent_ids or tool.call_id in dependencies))
        cache_key = f"{getattr(controller, 'session', view.session_id)}\x00{turn.id}"
        cached = shell.turn_cache.get(cache_key) if shell else None
        if cached and cached[0] is turn and cached[1] == turn_flags:
            turn_lines, turn_blocks = cached[2], cached[3]
        else:
            turn_lines, turn_blocks = _guarded(failures, f"Turn {turn.id}", lambda: _project_turn(turn, shell, view.agents, literal),
                                               ([], [{"id": turn.id, "title": "Turn", "text": "This turn could not be rendered", "kind": "literal"}]))
            if blocks and turn_blocks and turn_blocks[0].get("gap", 0) < 1:
                first = {**turn_blocks[0], "gap": 1}
                first["rev"] = _revision(first)
                turn_blocks = [first, *turn_blocks[1:]]
            if shell and not any(f.startswith(f"Turn {turn.id} ") for f in failures):
                if cached:
                    shell.turn_cache_bytes -= cached[4]
                size = len(json.dumps([turn_lines, turn_blocks], ensure_ascii=False).encode())
                shell.turn_cache[cache_key] = (turn, turn_flags, turn_lines, turn_blocks, size)
                shell.turn_cache.move_to_end(cache_key)
                shell.turn_cache_bytes += size
                while len(shell.turn_cache) > 4096 or shell.turn_cache_bytes > TURN_CACHE_BYTES:
                    _, removed = shell.turn_cache.popitem(last=False)
                    shell.turn_cache_bytes -= removed[4]
        lines.extend(turn_lines)
        if blocks and turn_blocks and turn_blocks[0].get("gap", 0) < 1:  # One blank row between turns
            first = {**turn_blocks[0], "gap": max(1, turn_blocks[0].get("gap", 0))}
            first["rev"] = _revision(first)
            turn_blocks = [first, *turn_blocks[1:]]
        blocks.extend(turn_blocks)
    linked = {agent_id for turn in view.turns for tool in turn.tools for agent_id in tool.child_agent_ids}
    for agent in () if on_agent_page else view.agents.values():
        if agent.id in linked:  # shown by its Task card
            continue
        blocks.append({"id": agent.id, "title": escape_controls(f"Agent {agent.id} · {agent.status}"), "text": escape_controls(agent.description),
                       "kind": "literal", "operation": {"kind": "agent_page", "id": agent.id}})
    prompt = None
    def permissions(body, depth=0):
        pending = list(body.pending_permissions)
        if depth < 8:
            for agent in body.agents.values():
                pending.extend(permissions(agent.body, depth+1))
        return pending
    pending_permissions = _guarded(failures, "Permission prompt", lambda: permissions(view), [])
    if pending_permissions:
        permission = pending_permissions[0]
        prompt = {"kind": "permission", "id": permission.id,
                  "lines": labelled(permission.to_dict()),
                  "choices": [{"label": choice.label, "value": choice.value,
                               "key": choice.key, "disabled": choice.disabled}
                              for choice in approval_choices(permission.to_dict())]}
    elif questions := pending_questions(view):
        question = questions[0]
        prompt = {"kind": "question", "id": question.call_id,
                  "lines": [question.prompt],
                  "choices": [{"label": choice.label, "value": choice.value,
                               "key": choice.key, "disabled": False}
                              for choice in question.choices()]}
    if prompt:
        prompt["lines"] = [escape_controls(line) for line in prompt["lines"]]
        for choice in prompt["choices"]:
            choice["label"] = escape_controls(choice["label"])
    details_panel = {} if on_agent_page else _guarded(failures, "Details sidebar", lambda: _details_panel(controller, view, shell), {})
    # Projection failures stay in the transcript (durable context); shell notices are toasts.
    notice = "\n".join(part for part in [error, *failures] if part)
    if notice:
        lines.append(escape_controls(f"Error: {notice}"))
        blocks.append({"id": "notice", "title": "Notice", "text": escape_controls(notice), "kind": "literal"})
    display_view = _context_display_view(view, shell)
    used, window, _measured = context_measure(display_view)
    snapshot = {"schema": 1, "revision": revision, "title": f"Nexus · {view.session_id}",
            "status": view.phase,
            "panel_layout": shell.panel_layout if shell else "modal",
            "panel_format": shell.panel_format if shell else "plain",
            "preview_image": base64.b64encode(shell.preview_image).decode("ascii") if shell and shell.preview_image else "",
            "preview_image_media": shell.preview_image_media if shell else "",
            "panel_loading": bool(shell and shell.panel_loading),
            "blocks": blocks,
            "transcript_verbose": bool(shell and shell.verbose),
            "local_ui_enabled": bool(shell and getattr(shell, "local_transcript", False)),
            "ui_ack": getattr(shell, "ui_ack", 0) if shell else 0,
            "commands": getattr(shell, "static_commands", []) if shell else [],
            "context_lines": [escape_controls(line) for line in context_lines],
            "context_used": used,
            "context_window": window,
            "context_marks": [],
            "context_tiers": _context_tiers(display_view),
            "context_usage": context_usage(view),
            "context_note": "",
            "context_label": _context_label(display_view),
            "queue_lines": _queue_lines(view),
            "composer_key": json.dumps([shell.workspace if shell else "", getattr(controller, "session", view.session_id)]),
            "nav": _settings_nav(shell),
            "update_notice": escape_controls(shell.update_notice) if shell else "",
            "toasts": list(shell.toasts) if shell else [],
            "disconnected": bool(
                shell and str(getattr(shell, "notice", "")).startswith("Disconnected")
            ),
            "provider": getattr(controller, "provider", None) or "",
            "effort": getattr(controller, "reasoning_effort", None) or "default",
            "theme": shell.preferences.values["theme"] if shell else "nexus-dark",
            "sessions_sidebar": shell.preferences.values["sessions_sidebar"] if shell else True,
            "details_sidebar": shell.preferences.values["details_sidebar"] if shell else True,
            "context_preview": shell.preferences.values["context_preview"] if shell else True,
            "sessions": shell.sessions if shell else [],
            "archived_label": shell.archived_label if shell else "",
            "sessions_truncated": bool(shell and shell.sessions_truncated),
            "sessions_request": shell.sessions_request if shell else 0,
            "settings_page": (shell.workflows.settings_page if shell.panel_title and shell.panel_title == shell.workflows.page_title else None) if shell else None,
            "tabs": _tab_rows(controller, shell) if shell else [],
            "breadcrumb": escape_controls(_display_breadcrumb(shell.breadcrumb)) if shell else "",
            "details_panel": details_panel,
            "logs": shell.logs.lines() if shell else [],
            **({"logs_all": shell.logs.lines(show_all=True), "logs_folded": shell.logs.lines(show_all=False),
                "logs_show_all": shell.logs.show_all} if shell and getattr(shell, "local_transcript", False) else {}),
            "attachment_lines": [f"{shell.attachment_label(i)}: {escape_controls(item.name)}" for i,item in enumerate(shell.attachments)] if shell else [],
            "form": {**shell.workflows.form, "can_delete": bool(shell.workflows.form_target and shell.workflows.form_target.get("kind") == "settings_read"), "status": escape_controls(shell.workflows.form["status"])} if shell and shell.workflows.form else None,
            "history": getattr(shell, "history", []) if shell else [],
            "insert": shell.composer_insert if shell else "",
            "auto_send_insert": shell.composer_auto_send if shell else False,
            "insert_kind": shell.composer_insert_kind if shell else "",
            "voice_phase": shell.voice.phase if shell else "idle",
            "voice_preview": escape_controls(shell.voice.preview) if shell else "",
            "voice_level": shell.voice.level if shell else 0,
            "completion_query": getattr(shell, "completion_query", "") if shell else "",
            "completion_prefix": getattr(shell, "completion_prefix", "") if shell else "",
            "completions": getattr(shell, "completions", []) if shell else [],
            "generation": shell.generation if shell else 0,
            "panel_title": escape_controls(shell.panel_title) if shell else "",
            "panel_hint": ("← areas · → page · ↑↓ select · Enter open · Alt+↑↓ reorder · Delete remove · Esc back" if shell and shell.settings_nav is not None else escape_controls(getattr(shell, "panel_hint", "")) if shell else ""),
            "panel_lines": [escape_controls(line) for line in shell.panel_lines] if shell else [],
            "panel_tones": list(shell.panel_tones) if shell else [],
            "items": [{**item, "label": escape_controls(item["label"]),
                       **({"detail": escape_controls(item["detail"])} if "detail" in item else {})} for item in shell.items] if shell else [],
            "prompt": prompt,
            "restore": shell.composer_restore if shell else "",
            "agent": getattr(controller, "agent_name", "build"),
            "model": escape_controls(
                (shell.model_names.get((getattr(controller, "provider", None) or "", getattr(controller, "model", None) or "")) if shell else None)
                or getattr(controller, "model", None) or "default"),
            "attachments": len(shell.attachments) if shell else 0,
            "lines": lines}
    if shell and shell.workflows.agent_page_id:
        agent = shell.workflows.selected_agent
        if agent is not None:
            from ...host.protocol import ContextInspectResult
            fields = set(ContextInspectResult.__struct_fields__) - {"session"}
            context = shell.workflows.agent_context
            preview = ContextInspectResult(session=agent.id, **{k: v for k, v in context.items() if k in fields})
            child_shell = SimpleNamespace(preview=preview)
            child_blocks = _compact_header(child_shell, agent.body)
            prompt_text = next((str(block.get("text") or "") for message in preview.messages[:1]
                               for block in message.get("blocks", []) if block.get("text")), "")
            if not prompt_text:
                prompt_text = next((str(tool.input.get("prompt") or "") for turn in view.turns for tool in turn.tools
                                    if tool.call_id == agent.parent_call_id), agent.task or agent.description)
            from ...ui_support.timeline import _literal
            rail = child_blocks[0]["color"]
            # A subagent page opens with the task as a prompt card in the agent's
            # colour (no header strip), then its thoughts, tool rows and reply.
            first, _, rest = prompt_text.partition("\n")
            child_blocks = [{"id": agent.id + ":task", "kind": "user", "title": _literal(first),
                             "text": _literal(rest), "color": rail, "gap": 0}]
            if not context and agent.status == "spawned":
                child_blocks.append({"id": agent.id + ":wait", "kind": "literal", "text": "Waiting for the first request…", "gap": 1})
            child_blocks = _safe_blocks(child_blocks)
            for turn in agent.body.turns:
                child_blocks.extend(_child_turn_blocks(shell, agent, turn, flags))
            from dataclasses import replace
            agent_status = "failed" if agent.is_error or agent.ok is False else agent.status
            snapshot.update(blocks=child_blocks, agent_page=agent.id,
                status="running" if agent.status == "spawned" else "done",
                sessions_sidebar=False, sessions=[], tabs=[], prompt=None, queue_lines=[],
                completions=[], voice_phase="idle", voice_preview="",
                agent=agent.type or "subagent", model=agent.model or preview.model or "default",
                title=f"{agent.type or 'Subagent'} · {agent.description or agent.task}",
                details_panel=_details_panel(SimpleNamespace(agent_name=agent.type, model=agent.model), replace(agent.body, phase=agent_status), shell))
    if shell and getattr(shell, "inline_images_enabled", False):
        from ...ui_support.native_images import inline_images
        snapshot["inline_images"] = inline_images(view, shell)
    snapshot["agent_color"] = _agent_color(shell, snapshot["agent"])
    if snapshot.get("agent_page"):
        snapshot["agent_color"] = next((block["color"] for block in snapshot["blocks"]
                                        if block.get("id", "").endswith(":task")), snapshot["agent_color"])
    for block in snapshot["blocks"]:
        if not block.get("rev"):
            block["rev"] = _revision(block)
    return snapshot


async def run(workspace: Path, session: str, binary: Path, client=None, reconnect=None, *, desktop=False) -> int:
    client = client or await open_client(workspace)
    controller = TuiController(client, session)
    shell = ShellActions(controller)
    from ..cli.commands import SPECS
    shell.static_commands = sorted((spec.name, list(spec.aliases)) for spec in SPECS if not spec.hidden)
    shell.local_transcript = not desktop
    shell.ui_ack = 0
    shell.inline_images_enabled = desktop
    shell.workspace = str(workspace)
    shell.tabs = [{"id": session, "title": session, "workspace": str(workspace), "state": "idle"}]
    shell.reconnect = reconnect or (lambda: open_client(workspace))
    controller.reconnect = lambda: shell.reconnect()
    shell.history = load_history()
    process = None
    write_lock = asyncio.Lock()
    revision = 0
    from .trace import BridgeTrace
    trace = BridgeTrace()
    from .stream_projection import StreamProjection, STREAM_EVENTS
    stream_projection = StreamProjection()
    stream_turn = None
    event_sent_at = 0.0
    event_kind = ""
    allowed_operations = set()
    displayed_prompt = None
    from .wire import TerminalWire
    terminal_wire = TerminalWire()
    desktop_wire = None
    update_coalescer = None
    async def update(event=False, *, immediate=True, _scheduled=False):
        nonlocal revision, desktop_wire, event_sent_at, event_kind, allowed_operations, displayed_prompt, stream_turn
        if event is not False and event is not None:
            event_kind = event.type
            stream_turn = event.turn if event.type in STREAM_EVENTS and (stream_turn in {None, event.turn}) else False
        elif not _scheduled:
            stream_turn = False
        if event is not False and event is not None and not desktop:
            immediate = event.type in {"permission.requested", "permission.resolved", "question.requested", "question.answered", "question.resolved", "tool.requested", "turn.completed", "turn.failed"}
        if not immediate and event is not None:
            if event is not False and event is not None:
                controller.ingest(event)
                shell.mark_current_seen()
                event_sent_at = event.ts if isinstance(getattr(event, "ts", None), (int, float)) and abs(time.time() - event.ts) < 60 else time.time()
            update_coalescer.schedule()
            return
        if update_coalescer is not None and not _scheduled:
            await update_coalescer.flush()
        if event is not False and event is not None:
            controller.ingest(event)
            shell.mark_current_seen()
            shell.notice = ""
            event_sent_at = event.ts if isinstance(getattr(event, "ts", None), (int, float)) and abs(time.time() - event.ts) < 60 else time.time()
            event = False
        # An explicit immediate flush incorporates all events already ingested.
        if event is None:
            shell.notice = "Disconnected: use /reconnect to replay and reattach"
        elif event is False and shell.notice.startswith("Disconnected"):
            shell.notice = "Reconnected · durable state replayed"
        elif event is not False:
            shell.notice = ""
            controller.ingest(event)
            shell.mark_current_seen()
        revision += 1
        async with write_lock:
            # The literal `lines` projection is a test seam; the native typed blocks already
            # carry the transcript and the full-detail actions, so it is not built here.
            if desktop:
                from ...ui_support.native_images import refresh_draft_images
                await refresh_draft_images(shell)
            started = time.perf_counter()
            snapshot = stream_projection.project(controller, shell, revision, stream_turn, _project_turn) if not desktop and stream_turn else None
            full_projection = snapshot is None
            if full_projection:
                snapshot = project(controller, revision, shell=shell, literal=False)
                stream_projection.remember(snapshot, controller, shell)
            stream_turn = None
            trace.elapsed("project", started)
            snapshot["event_sent_at"] = event_sent_at if trace.enabled else 0
            snapshot["event_kind"] = event_kind if trace.enabled else ""
            displayed_prompt = snapshot["prompt"]
            if full_projection:
                allowed_operations = {json.dumps(op, sort_keys=True) for op in [
                *[item["operation"] for item in snapshot.get("inline_images", [])],
                *[item.get("operation") for item in shell.items],
                *[item.get("toggle_operation") for item in shell.items if not item.get("toggle_locked")],
                *[item.get(key) for item in shell.items for key in ("move_up", "move_down", "remove")],
                *_block_operations(snapshot["blocks"])] if op}
            snapshot["completion_bell"] = controller.completion_bell
            shell.notify_completion()
            # Poll ticks that change nothing are not resent; one-shot composer
            # fields (restore/insert) always are, since they repeat legitimately.
            started = time.perf_counter()
            if desktop:
                if desktop_wire is None:
                    from ..desktop.wire import DesktopWire
                    desktop_wire = DesktopWire()
                wire = desktop_wire.encode(snapshot)
                if wire is None:
                    return
            else:
                wire = terminal_wire.encode(snapshot)
                if wire is None:
                    return
            trace.elapsed("fingerprint", started)
            started = time.perf_counter()
            encoded = json.dumps(wire, ensure_ascii=False, separators=(",", ":"))
            trace.elapsed("encode", started)
            trace.record("snapshot_bytes", len(encoded.encode()))
            started = time.perf_counter()
            process.stdin.write((encoded + "\n").encode())
            event_sent_at = 0.0
            event_kind = ""
            # Reset one-shot fields only after their snapshot has reached the
            # pipe. Event flushes may coalesce; they must not consume them.
            await process.stdin.drain()
            trace.elapsed("write+drain", started)
            trace.publish(shell.logs)
            if shell.composer_restore == snapshot.get("restore", ""):
                shell.composer_restore = ""
            sent_insert = snapshot.get("insert", "")
            if sent_insert and shell.composer_insert.startswith(sent_insert):
                shell.composer_insert = shell.composer_insert[len(sent_insert):]
            if not shell.composer_insert:
                shell.composer_auto_send = False
                shell.composer_insert_kind = ""
    async def emit_scheduled_update():
        try:
            await update(False, _scheduled=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            shell.notice = f"Display update failed: {type(exc).__name__}: {exc}"
    from ...ui_support.native_schedule import UpdateCoalescer
    update_coalescer = UpdateCoalescer(emit_scheduled_update, delay=0.016)
    from .background import BackgroundActions
    background = BackgroundActions(shell, update)
    shell.on_update = update
    poll_task = None
    async def poll():
        preview_cursor = -1
        while True:
            await asyncio.sleep(.2 if shell.voice.phase != "idle" else 1 if shell.logs.open else 3)
            failures = []
            async def section(label, work):
                try:
                    await work()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # keep polling; name the failing section
                    failures.append(f"{label} refresh failed: {type(exc).__name__}: {exc}")
            async def sessions():
                if shell.voice.phase != "idle":
                    return
                result = await shell.client.project_sessions()
                from .workflows import session_rows
                shell.refresh_session_tabs(session_rows(result, controller.session, shell.seen_seq))
                shell.sessions_truncated = bool(result.truncated)
                archived = await shell.client.list_archived_sessions("", limit=200)
                count = len(archived.sessions)
                shell.archived_label = f"Archived · {count}{'+' if archived.has_more else ''}" if count else ""
                if shell.workflows.agent_page_id and not shell.workflows.agent_context:
                    agent_id, generation, session = shell.workflows.agent_page_id, shell.generation, controller.session
                    result = await shell.client.agent_transcript(session, agent_id)
                    if (agent_id, generation, session) == (shell.workflows.agent_page_id, shell.generation, controller.session):
                        shell.workflows.agent_context = result.get("context") or {}
            async def dictation():
                if shell.panel_title == "Local dictation":
                    await shell.voice.open(prepare=False)
            async def health():
                if time.monotonic() < shell.mcp_due:
                    return
                shell.mcp_due = time.monotonic() + 30
                try:
                    result = await shell.client.doctor()
                    shell.mcp_report, shell.mcp_error = dict(getattr(result, "report", {}) or {}), None
                except Exception as exc:  # health is advisory; the sidebar names the failure
                    shell.mcp_error = str(exc)
                try:
                    update = await shell.client.update_status()
                    shell.update_notice = f"{sanitize(str(update.available), 80)} available: {sanitize(str(update.command), 120)}" if update.available else ""
                except Exception:  # noqa: BLE001 - the notice is advisory
                    shell.update_notice = ""
            async def context():
                nonlocal preview_cursor
                if controller.cursor == preview_cursor:
                    return
                generation, session_id, cursor = shell.generation, controller.session, controller.cursor
                if controller.running:  # the host has no preview while a turn runs; retry when idle
                    return
                preview = None
                try:
                    preview = await shell.client.inspect_context(session_id)
                except Exception as exc:  # noqa: BLE001
                    if "while the session is active" not in str(exc):
                        raise
                if preview is not None and generation == shell.generation:
                    from datetime import datetime
                    shell.preview = preview
                    shell.preview_at = datetime.now().astimezone()
                    preview_cursor = cursor
            async def logs():
                if shell.logs.open and shell.logs.visible:
                    await shell.logs.poll()
            for label, work in (("Sessions", sessions), ("Dictation", dictation), ("Health", health), ("Context", context), ("Logs", logs)):
                await section(label, work)
            if failures:
                shell.notice = "; ".join(failures)
            try:
                await update(immediate=False)
            except Exception as exc:
                shell.notice = f"Display update failed: {type(exc).__name__}: {exc}"
    try:
        await controller.bootstrap()
        await shell.refresh_preview(session)
        try:
            doctor = await client.doctor()
            git = doctor.report.get("git", {})
            shell.breadcrumb = str(workspace) + (" › " + str(git.get("branch")) if git.get("branch") else "")
        except Exception:
            shell.breadcrumb = str(workspace)
        try:  # full catalogue names for the composer controls; ids remain the fallback
            shell.model_names = {(str(row.get("provider") or ""), str(row.get("id") or "")): str(row["name"])
                                 for row in await client.list_models() if row.get("name")}
        except Exception:  # noqa: BLE001 - display names are advisory
            shell.model_names = {}
        process = await asyncio.create_subprocess_exec(
            str(binary), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024)
        try:
            setup = await shell.client.setup_status()
            if setup.required:  # first run: open Settings on Providers (its page offers "Choose default model")
                await shell.workflows.open_page("providers")
        except Exception as exc:
            shell.flash(str(exc), "error")
        await update()
        controller.resume(update)
        poll_task = asyncio.create_task(poll())
        while line := await process.stdout.readline():
            action = json.loads(line)
            if action["type"] == "quit":
                break
            try:
                if "generation" in action and action["generation"] != shell.generation:
                    if action["type"] == "submit":
                        shell.composer_restore = action["text"]
                        shell.notice = "Session changed: the old draft was returned and was not sent"
                        await update()
                    continue
                if action["type"] in background.KINDS:
                    if not background.start(action):
                        await update()
                    continue
                if action["type"] == "submit":
                    if not await shell.submit(action["text"], action.get("mode", "steer")):
                        process.stdin.close()
                        break
                elif action["type"] == "draft_changed":
                    shell.reconcile_attachment_markers(action["text"])
                elif action["type"] == "cancel":
                    if not await shell.workflows.speak_stop():
                        await shell.cancel()
                elif action["type"] == "dismiss":
                    if shell.panel_loading:
                        continue
                    if await shell.workflows.speak_stop():
                        continue
                    form = shell.workflows.form
                    if form and not form["secret"] and form["status"] and not form.get("saved", False):
                        shell.workflows.menu("Discard unsaved changes?", [("Keep editing", {"kind": "back"}),
                            ("Discard draft", {"kind": "discard_form"})], [form["status"], form["body"]])
                    else:
                        shell.workflows.back()
                        await shell.workflows.refresh_page()
                elif action["type"] == "command":
                    await shell.submit(action["text"])
                elif action["type"] == "pick":
                    if action.get("generation") == shell.generation:
                        item = next((item for item in shell.items if item["command"] == action["text"]), None)
                        if item:
                            shell.panel_title = ""
                            shell.items = []
                            await shell.submit(item["command"])
                elif action["type"] == "answer":
                    current = displayed_prompt
                    if current and current["id"] == action["text"]:
                        value = action.get("value", "")
                        allowed = [choice["value"] for choice in current["choices"] if not choice["disabled"]]
                        if current["kind"] == "permission":
                            if value not in allowed:
                                raise ValueError("Unavailable permission choice")
                            resolved = await shell.client.resolve_permission(controller.session, current["id"], value)
                            if not resolved:
                                shell.notice = "Permission already resolved by another client"
                        else:
                            resolved, error = await shell.client.answer_question(controller.session, current["id"], value)
                            if error:
                                raise ValueError(error)
                            if not resolved:
                                shell.notice = "Question already answered by another client"
                elif action["type"] == "operation":
                    operation = action.get("operation")
                    if operation_allowed(shell, allowed_operations, operation) and not shell.panel_loading:
                        if operation["kind"] in {"block_toggle", "turn_toggle"} and shell.local_transcript:
                            continue  # disclosure belongs exclusively to Rust
                        if operation["kind"] == "block_toggle":
                            if operation["id"] in shell.expanded: shell.expanded.remove(operation["id"])
                            elif len(shell.expanded) < 4096: shell.expanded.add(operation["id"])
                        else:
                            if await shell.workflows.operate(operation) is False:
                                process.stdin.close()
                                break
                elif action["type"] == "form_draft":
                    form = shell.workflows.form
                    if form and form["id"] == action["form"] and not form["secret"] and form["body"] != action["body"]:
                        form.update(body=action["body"], revision=action["revision"], saved=False, status="Unsaved draft")
                elif action["type"] == "save":
                    await shell.workflows.save(action["form"], action["body"], action["revision"])
                elif action["type"] == "form_delete":
                    target = shell.workflows.form_target
                    if target and target["kind"] == "settings_read":
                        await shell.workflows.operate({"kind": "confirm", "label": "Delete this file or restore its built-in default?", "next": {**target, "kind": "settings_delete"}})
                elif action["type"] == "voice_stop":
                    async def finish_voice(discard, send):
                        try:
                            await shell.voice.stop(discard, send=send)
                        except Exception as exc:
                            shell.flash(str(exc), "error")
                        await update()
                    if not shell.voice.finish_task or shell.voice.finish_task.done():
                        shell.voice.finish_task = asyncio.create_task(finish_voice(action.get("discard", False), action.get("send", False)))
                elif action["type"] == "voice_discard":
                    await shell.voice.discard()
                elif action["type"] == "context_header":
                    key = action.get("key")
                    if key in {"system", "agents", "tools", "skills", "mcp"}:
                        await shell.workflows.operate({"kind": "context_show", "key": key})
                elif action["type"] == "toggle":
                    key = action.get("key")
                    if key in {"sessions_sidebar", "details_sidebar", "context_preview"}:
                        shell.preferences.set(key, bool(action.get("value", not shell.preferences.values[key])))
                        shell.logs.open = shell.preferences.values["details_sidebar"] and shell.preferences.values["details_tab"] == "Logs"
                elif action["type"] == "file_toggle":
                    path = action["text"]
                    if not action.get("value", path not in shell.open_files):
                        shell.open_files.discard(path)
                    elif len(shell.open_files) < 256:
                        shell.open_files.add(path)
                elif action["type"] == "details_tab":
                    tab = action.get("text")
                    if tab in {"Session", "Files", "MCP", "Logs"}:
                        shell.preferences.set("details_tab", tab)
                        shell.logs.open = tab == "Logs" and shell.preferences.values["details_sidebar"]
                        if shell.logs.open and shell.logs.visible:
                            await shell.logs.poll()
                elif action["type"] == "details_visible":
                    shell.logs.visible = bool(action.get("open"))
                elif action["type"] == "ui_trace":
                    shell.logs.trace = [sanitize(line, 256) for line in action.get("lines", [])[:16]]
                elif action["type"] == "update_help":
                    shell.show("Nexus update", (shell.update_notice or "Run nexus update to check and install the current release.") + "\nRun this command in your terminal. Esc closes this help.")
                elif action["type"] == "context_popover":
                    shell.context_popover()
                elif action["type"] == "logs":
                    shell.preferences.set("details_sidebar", bool(action.get("open", True)))
                    shell.preferences.set("details_tab", "Logs")
                    shell.logs.open = bool(action.get("open", True))
                    if shell.logs.open and shell.logs.visible:
                        await shell.logs.poll()
                elif action["type"] == "logs_fold":
                    shell.logs.show_all = bool(action.get("value", not shell.logs.show_all))
                elif action["type"] == "nav_select":
                    from ...ui_support.settings_help import SETTINGS_SECTIONS
                    index = int(action["text"])
                    if not shell.panel_loading and 0 <= index < len(SETTINGS_SECTIONS) and SETTINGS_SECTIONS[index][0]:
                        key = SETTINGS_SECTIONS[index][0]
                        await shell.workflows.settings_area(key)
                elif action["type"] == "model_sort":
                    shell.model_sort = "name" if shell.model_sort == "updated" else "updated"
                    await shell.command("/model", ())
                elif action["type"] == "favorite":
                    items = [item for item in shell.items if action["filter"].lower() in item["label"].lower()]
                    if action["selection"] < len(items):
                        item = items[action["selection"]]
                        ref = item.get("operation", {}).get("ref") or item["command"].removeprefix("/model ")
                        if not ref or not ref.strip():
                            continue
                        favorites = list(shell.preferences.values["model_favorites"])
                        if ref in favorites: favorites.remove(ref)
                        else: favorites.append(ref)
                        shell.preferences.set("model_favorites", favorites)
                        await shell.command("/model", ())
                elif action["type"] == "cycle_effort":
                    levels = [None, *controller.supported_levels]
                    index = levels.index(controller.reasoning_effort) if controller.reasoning_effort in levels else 0
                    await shell.client.select_reasoning_effort(controller.session, levels[(index+1)%len(levels)])
                    await controller.refresh_agent_metadata()
                elif action["type"] == "cycle_agent":
                    agents = root_agents(await shell.client.list_agents())
                    names = [row["name"] for row in agents]
                    if names:
                        index = names.index(controller.agent_name) if controller.agent_name in names else -1
                        await shell.select_agent(names[(index+1)%len(names)])
                elif action["type"] == "session_actions":
                    row = next((row for row in shell.sessions if row["id"] == action["text"] and row["workspace"] == action["workspace"]), None)
                    if row:
                        await shell.switch_project(row["workspace"], row["id"])
                        await shell.workflows.operate({"kind": "session_actions", **row})
                elif action["type"] == "tab_close":
                    await shell.close_tab(action["workspace"], action["text"])
                elif action["type"] == "session_open":
                    if any(row["id"] == action["text"] and row["workspace"] == action["workspace"] for row in [*shell.sessions, *shell.tabs]):
                        await shell.switch_project(action["workspace"], action["text"])
                shell.ui_ack = max(shell.ui_ack, int(action.get("ui_sequence", 0)))
                if not controller.running:
                    controller.resume(update)
                await update()
            except Exception as exc:
                shell.flash(str(exc), "error")
                await update()
        return await process.wait()
    finally:
        await background.close()
        await shell.preferences.flush()
        trace.finish()
        if shell.usage_task:
            shell.usage_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await shell.usage_task
        if poll_task:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
        with contextlib.suppress(Exception):
            await shell.voice.discard()
        if update_coalescer is not None:
            with contextlib.suppress(Exception):
                await update_coalescer.flush()
        with contextlib.suppress(Exception):
            await controller.close()
        if process is not None and process.returncode is None:
            process.stdin.close()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), 2)
            if process.returncode is None:
                process.terminate()
                await process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="Experimental Nexus Ratatui client")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--session", default="ratatui-prototype")
    parser.add_argument("--binary", type=Path)
    args = parser.parse_args()
    from .run import binary_path
    asyncio.run(run(args.workspace.resolve(), args.session, args.binary.resolve() if args.binary else binary_path()))


if __name__ == "__main__":
    main()
