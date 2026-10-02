"""Python reducer/host adapter for the native prototype (feasibility §4).

Private newline-delimited snapshots go to Rust stdin; typed actions return on
stdout. Rust renders exclusively on stderr. Closing the UI never cancels a turn.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from pathlib import Path

from ...ui.cli import open_client
from ...ui_support.prompts import approval_choices, pending_questions
from .actions import ShellActions, labelled
from ...ui_support.context import context_usage
from ...ui_support.hints import pick_hints
from ...ui_support.tui_history import load_history
from ...ui_support.clipboard import read_clipboard_image
from .controller import NativeController as TuiController
from ...ui_support.text import escape_controls, redact
from ...ui_support.tool_details import sections_to_text, tool_detail_sections


def _safe_blocks(blocks):
    return [{**block, **{key: redact(escape_controls(block[key])) for key in ("title", "text", "path", "detail") if key in block},
             **({"chips": [redact(escape_controls(chip)) for chip in block["chips"]]} if block.get("chips") else {})} for block in blocks]


def _project_turn(turn, shell, agents=None):
    """Blocks of one turn in Textual's order and spacing (ui/tui/timeline.py).

    Each block carries ``gap`` (blank rows before it, after margin collapsing),
    so Rust only draws. Reply/tool/thought order follows the durable event seq.
    """
    from ...ui_support.timeline import (
        BATCH_GLYPHS, _has_message_content, _literal, submitted_attachment_summary,
        thought_title, tool_batches, tool_row_text, tool_status, turn_agent_label, turn_footer_text,
        _turn_duration, _text)
    collapsed = bool(shell and turn.id in shell.collapsed_turns)
    lines = [f"\nTurn {turn.index} · {turn.phase}"]
    entries = []  # (seq, rank, block, top_margin, bottom_margin)
    first_reply = None
    for index, message in enumerate(turn.messages):
        if message.role == "user":
            if not _has_message_content(message):
                continue
            prompt, chips = submitted_attachment_summary(message)
            text = _literal(prompt)
            first, _, rest = text.partition("\n")
            lines.append(f"user · text\n{message.text}")
            if collapsed and rest:
                first, rest = first + " …", ""
            block = {"id": message.id + ":user", "kind": "user", "title": first, "text": rest,
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
                expanded = bool(shell and message.id + "thinking" in shell.expanded)
                count = len([line for line in thinking.splitlines() if line.strip()])
                more = "▾" if expanded else "▸"
                suffix = (f"{count} lines {more}" if count > 1 and not expanded else more)
                block = {"id": message.id + "thinking", "kind": "thought", "title": thought_title(thinking),
                         "text": suffix, "detail": _literal(thinking) if expanded else "",
                         "operation": {"kind": "block_toggle", "id": message.id + "thinking"}}
                entries.append((message.event_seq, 0, block, 0, 1, "thought"))
                lines.append(f"assistant · thinking\n{thinking}")
        if message.text if message.role == "assistant" else _has_message_content(message):
            block = {"id": message.id + "text", "kind": "markdown", "text": message.text}
            entries.append((message.event_seq, 2, block, 0, 1, "assistant"))
            lines.append(f"assistant · text\n{message.text}")
            if first_reply is None:
                first_reply = message.event_seq
    name, color = turn_agent_label(turn)
    if name and first_reply is not None:
        entries.append((first_reply, 1, {"id": turn.id + ":agent", "kind": "agent", "title": name, "color": color}, 0, 0, "agent"))
    batches = tool_batches(turn.tools)
    for tool in turn.tools:
        body = sections_to_text(tool_detail_sections(tool))
        lines.append(body)
        glyph = BATCH_GLYPHS.get(batches.get(tool.call_id, ""))
        gutter = f"{glyph} " if glyph else ""
        operation = {"kind": "tool_page", "id": tool.call_id}
        if tool.name.casefold() in {"task", "subagent"}:
            from ...ui_support.timeline import _task_children, _task_header, _task_metrics
            child = next(iter(_task_children(tool, agents or {})), None)
            head, running = _task_header(tool, child, 0)
            text = f"{gutter}{head}\n{gutter}  {_task_metrics(tool, child, running)}"
            if child is not None:  # Textual opens a spawned child's page straight away
                operation = {"kind": "agent_page", "id": child.id}
        else:
            text = tool_row_text(tool, 0, gutter)
        if tool_status(tool) == "running":  # the native client animates this slot (docs/ratatui-parity.md)
            text = text.replace(SPINNER_FRAMES[0], SPINNER_SLOT, 1)
        block = {"id": tool.call_id, "kind": "tool", "status": tool_status(tool), "text": text,
                 "detail": body if shell and shell.verbose else "", "operation": operation}
        entries.append((tool.event_seq, 3, block, 0, 0, "tool"))
        if tool.diff:
            from ...ui_support.timeline import diff_sections, diff_split_rows, split_diff_files
            hunks = dict(split_diff_files(tool.diff))
            for diff in diff_sections(tool.diff):
                rows = [[old_no, redact(escape_controls(old_text)), new_no, redact(escape_controls(new_text)), kind]
                        for old_no, old_text, new_no, new_text, kind in diff_split_rows(hunks.get(diff.path, ""))]
                entries.append((tool.event_seq, 3, {"id": tool.call_id + diff.path, "kind": "diff", "title": diff.path,
                    "path": diff.path, "added": diff.added, "removed": diff.removed, "diff_rows": rows,
                    "operation": {"kind": "tool_page", "id": tool.call_id}}, 0, 0, "diff"))
    if turn.terminal and turn.error:
        entries.append((max((entry[0] for entry in entries), default=0) + 1, 4,
                        {"id": turn.id + ":error", "kind": "error", "text": "Error: " + _text(turn.error)}, 1, 0, "error"))
        lines.append(f"Error: {turn.error}")
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    if collapsed:
        entries = [entry for entry in entries if entry[5] == "user"]
    blocks, previous = [], None
    for _, _, block, top, bottom, role in entries:
        if previous is not None:
            if previous[0] == "user":
                top = max(top, 1)
            elif previous[0] == "tool" and role in {"assistant", "agent"}:
                top = max(top, 1)
            block = {**block, "gap": max(previous[1], top)}
        blocks.append(block)
        previous = (role, bottom)
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
    return [redact(escape_controls(line)) for line in lines], _safe_blocks(blocks)


def _context_blocks(shell, view):
    """The request-context header that opens every conversation (Textual ContextHeader)."""
    from ...ui_support.context_header import NEUTRAL, HeaderBlock, agent_color, header_blocks
    from ...ui_support.context import _compact_tokens
    preview = shell.preview
    if preview is not None and hasattr(preview, "tools"):
        agent = preview.agent if isinstance(getattr(preview, "agent", None), dict) else {}
        color = agent.get("color") if isinstance(agent.get("color"), str) and agent.get("color") else agent_color(str(agent.get("name") or "build"))
        found = header_blocks(preview, color)
    else:
        found = [HeaderBlock(key, label, "", "", None, NEUTRAL) for key, label in (
            ("system", "System prompt"), ("tools", "Tools"), ("agents", "AGENTS.md"), ("skills", "Skills"), ("mcp", "MCP"))]
    return [{"id": "context:" + block.key, "kind": "context", "title": block.label, "text": block.body,
             "status": f"~{_compact_tokens(block.tokens)} tokens" if block.tokens else "", "color": block.color,
             "gap": 1 if index else 0, "operation": {"kind": "context_show", "key": block.key}}
            for index, block in enumerate(found)]


def _details_panel(controller, view, shell):
    """The Textual details sidebar (SESSION, MODIFIED FILES, MCP SERVERS) as data."""
    from ...ui_support.details import diff_preview_lines, mcp_rows, modified_files, session_rows
    rows = session_rows(view, phase=view.phase, agent=getattr(controller, "agent_name", "build"),
                        model=getattr(controller, "model", None) or "default",
                        effort=getattr(controller, "reasoning_effort", None))
    files = modified_files(view)
    added, removed = sum(f.added for f in files), sum(f.removed for f in files)
    return {
        "session": [[label, redact(escape_controls(value))] for label, value in rows],
        "files": [{"path": redact(escape_controls(f.path)), "added": f.added, "removed": f.removed, "created": f.created,
                   "open": (is_open := bool(shell and f.path in shell.open_files)),
                   "diff": [redact(escape_controls(line)) for line in diff_preview_lines(f.hunks)] if is_open else []} for f in files],
        "files_summary": f"+{added} -{removed} across {len(files)} file{'s' if len(files) != 1 else ''}" if files else "",
        "mcp": [[tone, redact(escape_controls(text)), redact(escape_controls(note))]
                for tone, text, note in mcp_rows(getattr(shell, "mcp_report", None), error=getattr(shell, "mcp_error", None))],
    }


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_SLOT = "\ue000"  # private-use placeholder; Rust draws the current frame here


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


def project(controller: TuiController, revision: int, error: str = "", shell=None) -> dict:
    """Project labelled, control-safe context and transcript from canonical state.

    Each section is built independently: one failing section is replaced by a
    labelled notice and the others still render.
    """
    view = controller.view
    failures: list[str] = []
    lines = []
    blocks = []
    context_lines = []
    if shell and shell.preferences.values["context_preview"]:
        blocks.extend(_guarded(failures, "Context header", lambda: _safe_blocks(_context_blocks(shell, view)), []))
    flags = (bool(shell and shell.verbose), frozenset(shell.expanded) if shell else frozenset(), frozenset(shell.collapsed_turns) if shell else frozenset(), id(view.agents))
    for turn in view.turns:
        cached = shell.turn_cache.get(turn.id) if shell else None
        if cached and cached[0] is turn and cached[1] == flags:
            turn_lines, turn_blocks = cached[2], cached[3]
        else:
            turn_lines, turn_blocks = _guarded(failures, f"Turn {turn.id}", lambda: _project_turn(turn, shell, view.agents),
                                               ([], [{"id": turn.id, "title": "Turn", "text": "This turn could not be rendered", "kind": "literal"}]))
            if shell and not any(f.startswith(f"Turn {turn.id} ") for f in failures):
                if cached:
                    shell.turn_cache_bytes -= cached[4]
                size = len(json.dumps([turn_lines, turn_blocks], ensure_ascii=False).encode())
                shell.turn_cache[turn.id] = (turn, flags, turn_lines, turn_blocks, size)
                shell.turn_cache.move_to_end(turn.id)
                shell.turn_cache_bytes += size
                while len(shell.turn_cache) > 4096 or shell.turn_cache_bytes > 8 * 1024 * 1024:
                    _, removed = shell.turn_cache.popitem(last=False)
                    shell.turn_cache_bytes -= removed[4]
        lines.extend(turn_lines)
        if blocks and turn_blocks:  # Textual's .turn margin-bottom: one blank row between turns
            turn_blocks = [{**turn_blocks[0], "gap": max(1, turn_blocks[0].get("gap", 0))}, *turn_blocks[1:]]
        blocks.extend(turn_blocks)
    if not view.turns:  # grey tips in an empty session; the native client blanks them while typing
        rows = pick_hints(view.session_id or "session")
        keys_width = max(len(keys) for keys, _ in rows)
        tail_width = max(len(text) for _, text in rows)
        blocks.append({"id": "empty-hints", "kind": "hints", "gap": 4,
                       "text": "\n".join(f"{redact(escape_controls(keys.rjust(keys_width)))}\t{redact(escape_controls(text.ljust(tail_width)))}" for keys, text in rows)})
    linked = {agent_id for turn in view.turns for tool in turn.tools for agent_id in tool.child_agent_ids}
    for agent in view.agents.values():
        if agent.id in linked:  # shown by its Task card
            continue
        blocks.append({"id": agent.id, "title": redact(escape_controls(f"Agent {agent.id} · {agent.status}")), "text": redact(escape_controls(agent.description)),
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
        prompt["lines"] = [redact(escape_controls(line)) for line in prompt["lines"]]
        for choice in prompt["choices"]:
            choice["label"] = redact(escape_controls(choice["label"]))
    details_panel = _guarded(failures, "Details sidebar", lambda: _details_panel(controller, view, shell), {})
    notice = "\n".join(part for part in [error or (shell.notice if shell else ""), *failures] if part)
    if notice:
        lines.append(redact(escape_controls(f"Error: {notice}")))
        blocks.append({"id": "notice", "title": "Notice", "text": redact(escape_controls(notice)), "kind": "literal"})
    return {"schema": 1, "revision": revision, "title": f"Nexus · {view.session_id}",
            "status": view.phase,
            "blocks": blocks,
            "context_lines": [redact(escape_controls(line)) for line in context_lines],
            "context_usage": context_usage(view),
            "provider": getattr(controller, "provider", None) or "",
            "effort": getattr(controller, "reasoning_effort", None) or "default",
            "theme": shell.preferences.values["theme"] if shell else "nexus-dark",
            "sessions_sidebar": shell.preferences.values["sessions_sidebar"] if shell else True,
            "details_sidebar": shell.preferences.values["details_sidebar"] if shell else True,
            "context_preview": shell.preferences.values["context_preview"] if shell else True,
            "sessions": shell.sessions if shell else [],
            "archived_label": shell.archived_label if shell else "",
            "sessions_truncated": bool(shell and shell.sessions_truncated),
            "tabs": _tab_rows(controller, shell) if shell else [],
            "breadcrumb": redact(escape_controls(shell.breadcrumb)) if shell else "",
            "details_panel": details_panel,
            "logs": shell.logs.lines() if shell else [],
            "attachment_lines": [f"{shell.attachment_label(i)}: {redact(escape_controls(item.name))}" for i,item in enumerate(shell.attachments)] if shell else [],
            "form": {**shell.workflows.form, "status": redact(escape_controls(shell.workflows.form["status"]))} if shell and shell.workflows.form else None,
            "history": getattr(shell, "history", []) if shell else [],
            "insert": shell.composer_insert if shell else "",
            "auto_send_insert": shell.composer_auto_send if shell else False,
            "insert_kind": shell.composer_insert_kind if shell else "",
            "voice_phase": shell.voice.phase if shell else "idle",
            "voice_preview": redact(escape_controls(shell.voice.preview)) if shell else "",
            "voice_level": shell.voice.level if shell else 0,
            "completion_query": getattr(shell, "completion_query", "") if shell else "",
            "completions": getattr(shell, "completions", []) if shell else [],
            "generation": shell.generation if shell else 0,
            "panel_title": redact(escape_controls(shell.panel_title)) if shell else "",
            "panel_lines": [redact(escape_controls(line)) for line in shell.panel_lines] if shell else [],
            "panel_tones": list(shell.panel_tones) if shell else [],
            "items": [{**item, "label": redact(escape_controls(item["label"]))} for item in shell.items] if shell else [],
            "prompt": prompt,
            "restore": shell.composer_restore if shell else "",
            "agent": getattr(controller, "agent_name", "build"),
            "model": redact(escape_controls(getattr(controller, "model", None) or "default")),
            "attachments": len(shell.attachments) if shell else 0,
            "lines": lines}


async def run(workspace: Path, session: str, binary: Path, client=None, reconnect=None) -> int:
    client = client or await open_client(workspace)
    controller = TuiController(client, session)
    shell = ShellActions(controller)
    shell.workspace = str(workspace)
    shell.tabs = [{"id": session, "title": session, "workspace": str(workspace), "state": "idle"}]
    shell.reconnect = reconnect or (lambda: open_client(workspace))
    controller.reconnect = lambda: shell.reconnect()
    shell.history = load_history()
    process = None
    write_lock = asyncio.Lock()
    revision = 0
    last_sent = [""]
    async def update(event=False):
        nonlocal revision
        if event is None:
            shell.notice = "Disconnected: use /reconnect to replay and reattach"
        elif event is False and shell.notice.startswith("Disconnected"):
            shell.notice = "Reconnected · durable state replayed"
        elif event is not False:
            shell.notice = ""
            controller.ingest(event)
        revision += 1
        async with write_lock:
            snapshot = project(controller, revision, shell=shell)
            # Legacy literal projection is useful to tests; native typed blocks
            # already contain the rendered transcript and full-detail actions.
            snapshot["lines"] = []
            encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
            # Poll ticks that change nothing are not resent; one-shot composer
            # fields (restore/insert) always are, since they repeat legitimately.
            fingerprint = json.dumps({**snapshot, "revision": 0}, ensure_ascii=False)
            one_shot = snapshot.get("restore") or snapshot.get("insert")
            if fingerprint == last_sent[0] and not one_shot:
                return
            last_sent[0] = fingerprint
            process.stdin.write((encoded + "\n").encode())
            shell.composer_restore = ""
            shell.composer_insert = ""
            shell.composer_auto_send = False
            shell.composer_insert_kind = ""
            await process.stdin.drain()
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
                shell.sessions = session_rows(result, controller.session, shell.seen_seq)
                shell.sessions_truncated = bool(result.truncated)
                for row in shell.sessions:
                    existing = next((tab for tab in shell.tabs if tab["id"] == row["id"] and tab["workspace"] == row["workspace"]), None)
                    if existing:
                        existing.update(row)
                    elif row["state"] in {"running", "awaiting_input", "awaiting_permission"}:
                        shell.tabs.append(dict(row))
                archived = await shell.client.list_archived_sessions("", limit=200)
                count = len(archived.sessions)
                shell.archived_label = f"Archived · {count}{'+' if archived.has_more else ''}" if count else ""
                if shell.workflows.agent_page_id and shell.panel_title == "Agent transcript":
                    shell.panel_lines = labelled(await shell.client.agent_transcript(controller.session, shell.workflows.agent_page_id))
            async def dictation():
                if shell.panel_title == "Local dictation":
                    await shell.voice.open()
            async def health():
                if time.monotonic() < shell.mcp_due:
                    return
                shell.mcp_due = time.monotonic() + 30
                try:
                    result = await shell.client.doctor()
                    shell.mcp_report, shell.mcp_error = dict(getattr(result, "report", {}) or {}), None
                except Exception as exc:  # health is advisory; the sidebar names the failure
                    shell.mcp_error = str(exc)
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
                    shell.preview = preview
                    preview_cursor = cursor
            async def logs():
                if shell.logs.open:
                    await shell.logs.poll()
            for label, work in (("Sessions", sessions), ("Dictation", dictation), ("Health", health), ("Context", context), ("Logs", logs)):
                await section(label, work)
            if failures:
                shell.notice = "; ".join(failures)
            try:
                await update()
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
        process = await asyncio.create_subprocess_exec(
            str(binary), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024)
        try:
            setup = await shell.client.setup_status()
            if setup.required:
                await shell.workflows.providers()
                shell.items.append({"label": "Choose default model", "command": "", "operation": {"kind": "setup"}})
        except Exception as exc:
            shell.notice = str(exc)
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
                if action["type"] == "submit":
                    if not await shell.submit(action["text"], action.get("mode", "queue")):
                        process.stdin.close()
                        break
                elif action["type"] == "cancel":
                    await shell.cancel()
                elif action["type"] == "dismiss":
                    form = shell.workflows.form
                    if form and not form["secret"] and form["status"] and not form.get("saved", False):
                        shell.workflows.menu("Discard unsaved changes?", [("Keep editing", {"kind": "back"}),
                            ("Discard draft", {"kind": "discard_form"})], [form["status"], form["body"]])
                    else:
                        shell.workflows.back()
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
                    current = project(controller, revision, shell=shell)["prompt"]
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
                    allowed_operations = [item.get("operation") for item in shell.items] + [block.get("operation") for block in project(controller, revision, shell=shell)["blocks"]]
                    if operation and operation in allowed_operations:
                        if operation["kind"] == "block_toggle":
                            if operation["id"] in shell.expanded: shell.expanded.remove(operation["id"])
                            else: shell.expanded.add(operation["id"])
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
                    async def finish_voice(discard):
                        try:
                            await shell.voice.stop(discard)
                        except Exception as exc:
                            shell.notice = str(exc)
                        await update()
                    if not shell.voice.finish_task or shell.voice.finish_task.done():
                        shell.voice.finish_task = asyncio.create_task(finish_voice(action.get("discard", False)))
                elif action["type"] == "voice_discard":
                    await shell.voice.discard()
                elif action["type"] == "context_header":
                    key = action.get("key")
                    if key in {"system", "agents", "tools", "skills", "mcp"}:
                        await shell.workflows.operate({"kind": "context_show", "key": key})
                elif action["type"] == "toggle":
                    key = action.get("key")
                    if key in {"sessions_sidebar", "details_sidebar", "context_preview"}:
                        shell.preferences.set(key, not shell.preferences.values[key])
                elif action["type"] == "file_toggle":
                    path = action["text"]
                    if path in shell.open_files:
                        shell.open_files.discard(path)
                    elif len(shell.open_files) < 256:
                        shell.open_files.add(path)
                elif action["type"] == "logs":
                    shell.logs.open = bool(action.get("open", True))
                    if shell.logs.open:
                        await shell.logs.poll()
                elif action["type"] == "logs_fold":
                    shell.logs.show_all = not shell.logs.show_all
                elif action["type"] == "clipboard":
                    if len(shell.attachments) >= 8:
                        raise ValueError("At most eight attachments are allowed")
                    image = await asyncio.to_thread(read_clipboard_image)
                    if image:
                        shell.attachments.append(await shell.client.prepare_attachment(name="clipboard.png", data=image))
                        shell.composer_insert = shell.attachment_label(len(shell.attachments)-1)
                elif action["type"] == "copy_text":
                    from .desktop import copy_text
                    await copy_text(action["text"])
                elif action["type"] == "complete":
                    from ...ui_support.completion import complete
                    query = shell.completion_query = action["text"]
                    shell.completions = await complete(
                        shell.client, action.get("prefix", query), query, efforts=controller.supported_levels)
                elif action["type"] == "refresh_models":
                    await shell.client.refresh_models()
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
                    agents = await shell.client.list_agents()
                    names = [row["name"] for row in agents]
                    if names:
                        index = names.index(controller.agent_name) if controller.agent_name in names else -1
                        await controller.select_agent(names[(index+1)%len(names)])
                elif action["type"] == "session_actions":
                    row = next((row for row in shell.sessions if row["id"] == action["text"] and row["workspace"] == action["workspace"]), None)
                    if row:
                        await shell.switch_project(row["workspace"], row["id"])
                        await shell.workflows.operate({"kind": "session_actions", **row})
                elif action["type"] == "tab_close":
                    index = next((i for i,row in enumerate(shell.tabs) if row["id"] == action["text"] and row["workspace"] == action["workspace"]), None)
                    if index is not None and len(shell.tabs) == 1:
                        await shell.command("/new", ())
                    elif index is not None:
                        shell.tabs.pop(index)
                        if controller.session == action["text"] and shell.tabs:
                            row = shell.tabs[min(index, len(shell.tabs)-1)]
                            await shell.switch_project(row["workspace"], row["id"])
                elif action["type"] == "session_open":
                    if any(row["id"] == action["text"] and row["workspace"] == action["workspace"] for row in [*shell.sessions, *shell.tabs]):
                        await shell.switch_project(action["workspace"], action["text"])
                if not controller.running:
                    controller.resume(update)
                await update()
            except Exception as exc:
                shell.notice = str(exc)
                await update()
        return await process.wait()
    finally:
        if poll_task:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
        with contextlib.suppress(Exception):
            await shell.voice.discard()
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
