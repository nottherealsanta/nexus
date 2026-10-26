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
from ...ui_support import manager as manager_ui
from ...ui_support.manager import MANAGER_MODEL_REF
from ...ui_support.prompts import approval_choices, pending_questions
from .actions import ShellActions, labelled
from .permission_prompt import permission_fields
from ...ui_support.context import context_usage, context_measure, price_tier_thresholds
from ...ui_support.completion import root_agents
from ...ui_support.prompt_history import load_history
from .controller import NativeController as TuiController
from .worktree_bar import root_move_text
from ...view import RootMoveView
from .verb_groups import CHANGE_TOOLS, activity_entries, format_time
from .turn_status import running_agents, turn_status
from ...host_support.errors import error_text
from ...ui_support.text import escape_controls, sanitize
from ...ui_support.tool_details import sections_to_text
from ...ui_support.tool_signal import shell_preview, tool_signal
from ...ui_support.timeline import TASK_TOOL_NAMES


_OUTPUT_SECTIONS = frozenset({"Result", "Summary", "Error", "Progress"})
_EXPAND_AFTER_LINES = 12  # outputs longer than this expand in place on click
#: File changes each get their own transcript row (never an activity run), with +/- line counts.
EDIT_TOOLS = frozenset({"edit", "multiedit", "write", "apply_patch", "patch"})
#: Calls drawn one per row (`change` blocks), never folded into an activity run.
STANDALONE_TOOLS = EDIT_TOOLS | {"question"}
_CHANGE_ROW_LIMIT = 400  # diff rows sent per call; the rest is announced by a clip row
_USER_CHARACTER_LIMIT = 16_000
_USER_LINE_LIMIT = 200


def _user_prompt(prompt: str) -> str:
    """Bound literal user previews and announce omitted content with its total size."""
    text = escape_controls(prompt)
    preview = "\n".join(text[:_USER_CHARACTER_LIMIT].split("\n")[:_USER_LINE_LIMIT])
    if preview != text:
        preview += f"\n... ({len(prompt):,} characters total)"
    return preview


def _worktree_name(workspace) -> str:
    """The workspace directory name when it is a linked Git worktree (`.git` is a file), else ""."""
    try:
        path = Path(workspace)
        return path.name[:80] if (path / ".git").is_file() else ""
    except OSError:
        return ""


async def refresh_branch(shell) -> bool:
    """Re-read the current workspace's branch from ``.git`` off the event loop.

    Sets ``breadcrumb`` (``path › branch``), ``branch``, ``worktree`` and ``worktree_kind`` for
    whichever workspace and session the active tab shows. The Git facts are remembered on the shell
    and combined with the session's root (``ConversationView.root``) by ``worktree_bar.apply_bar``,
    which also redraws the bar when an agent move changes the root. The result is dropped when the
    workspace, session or switch generation moved on during the await. Returns whether any field changed.
    """
    from ...host_support.git_head import git_head
    from .worktree_bar import apply_bar

    def token():
        return (str(shell.workspace or ""), getattr(getattr(shell, "controller", None), "session", ""),
                getattr(shell, "generation", 0))

    start = token()
    workspace = start[0]
    if not workspace:
        return False
    head, worktree = await asyncio.to_thread(lambda: (git_head(workspace), _worktree_name(workspace)))
    if token() != start:
        return False
    shell._git_branch, shell._git_worktree = str(head.get("branch") or ""), worktree
    shell._bar_root = getattr(getattr(getattr(shell, "controller", None), "view", None), "root", None)
    return apply_bar(shell)


def _display_breadcrumb(breadcrumb: str, home: Path | None = None) -> str:
    """Abbreviate only the actual home component; show the branch as ``path:branch``."""
    home_text = str(Path.home() if home is None else home).rstrip("/")
    path, separator, branch = breadcrumb.partition(" › ")
    # Root is not a useful home abbreviation, nor is a relative home path.
    if home_text and Path(home_text).is_absolute():
        if path == home_text:
            path = "~"
        elif path.startswith(home_text + "/"):
            path = "~" + path[len(home_text):]
    return path + (":" if separator else "") + branch


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
    dashboard = getattr(workflows, "dashboard", None)
    if dashboard is not None and dashboard.accepts(operation):
        return True
    overview = workflows.settings_overview if page else None
    return sp.accepts(page, operation) or sp.accepts(overview, operation)


def _dashboard_hidden(shell, prompt) -> bool:
    """A prompt, panel or form owns input; the dashboard stays out of the snapshot meanwhile."""
    return bool(prompt or shell.panel_title or shell.workflows.form)


def _dashboard_state(shell, prompt=None):
    """The ``dashboard`` snapshot field: the visible Sessions dashboard, else ``None``.

    Always ``None`` where the left sidebar is enabled: its Sessions tab carries the list
    (``sidebar.sessions``), so the conversation is never replaced by a page.
    """
    sidebar = getattr(shell, "sidebar", None) if shell else None
    if getattr(sidebar, "enabled", False) is True:
        return None
    dashboard = getattr(shell.workflows, "dashboard", None) if shell else None
    if dashboard is None or _dashboard_hidden(shell, prompt):
        return None
    return dashboard.snapshot()


def _sidebar_state(shell) -> dict:
    """The ``sidebar`` snapshot field; ``available`` is false for hosts that do not own a sidebar."""
    from .sidebar import SidebarController
    sidebar = getattr(shell, "sidebar", None) if shell else None
    return sidebar.snapshot() if isinstance(sidebar, SidebarController) else {"available": False, "open": False}


def _block_operations(blocks):
    """Allow only actions present in the current projected transcript, including chips."""
    for block in blocks:
        for key in ("operation", "output_operation", "input_operation", "chip_operation", "inspect_operation"):
            if operation := block.get(key):
                yield operation
        yield from block.get("chip_operations", [])
        yield from _block_operations(block.get("members", []))
        yield from _block_operations(block.get("failed_members", []))
        yield from _block_operations([block["live_member"]] if block.get("live_member") else [])


def _explored_summary(tools) -> str:
    """`1 search, 2 reads` when every call is a lookup, else empty."""
    kinds = [_EXPLORE_KINDS.get(tool.name.casefold()) for tool in tools]
    if not kinds or not all(kinds):
        return ""
    counts = {}
    for one, many in kinds:
        counts[(one, many)] = counts.get((one, many), 0) + 1
    return ", ".join(f"{n} {one if n == 1 else many}" for (one, many), n in counts.items())


def _change_rows(tool) -> tuple[list[list], int, int, list[str]]:
    """Unified diff rows ``[kind, line_no, text]``, ``+added``, ``-removed`` and paths.

    A durable ``diff`` artifact wins; a Write without one shows the content it wrote,
    every line an addition. Multi-file diffs open each file with a ``file`` row.
    """
    from ...ui_support.timeline import diff_sections, diff_unified_rows, split_diff_files, tool_status
    if tool.diff:
        files = split_diff_files(tool.diff)
        sections = diff_sections(tool.diff)
        rows = []
        for path, hunk in files:
            if len(files) > 1:
                rows.append(["file", 0, escape_controls(path)])
            rows.extend([kind, number, escape_controls(text)] for kind, number, text in diff_unified_rows(hunk, _CHANGE_ROW_LIMIT))
        return (rows, sum(s.added for s in sections), sum(s.removed for s in sections),
                [s.path for s in sections])
    content = tool.input.get("content") if isinstance(tool.input, dict) else None
    if tool.name.casefold() == "write" and isinstance(content, str) and content and tool_status(tool) == "completed":
        lines = content.splitlines()
        rows = [["add", n, escape_controls(line)] for n, line in enumerate(lines[:_CHANGE_ROW_LIMIT], 1)]
        if len(lines) > _CHANGE_ROW_LIMIT:
            rows.append(["clip", 0, f"… {len(lines) - _CHANGE_ROW_LIMIT} more rows"])
        return rows, len(lines), 0, [str(tool.input.get("path", ""))]
    return [], 0, 0, []


def _question_detail(tool, status) -> str:
    """Compact Question card body: the prompt, its options, and the chosen answer.

    ``●`` marks the option whose label equals the recorded answer, ``○`` the others; an
    answer that matches no option (free text) is shown as ``✎ answer``. The answer is
    the label the tool recorded, so a typed reply equal to an option's label cannot be
    told from choosing it. Empty when the call carries no ``question`` string, so the
    caller falls back to the labelled details.
    """
    data = tool.input if isinstance(tool.input, dict) else {}
    prompt = data.get("question")
    if not isinstance(prompt, str) or not prompt.strip():
        return ""
    raw = data.get("options")
    options = [" ".join(item.split()) for item in (raw if isinstance(raw, list) else [])
               if isinstance(item, str) and item.strip()]
    answer = ""
    if status == "completed":
        recorded = (tool.metrics or {}).get("answer") if isinstance(tool.metrics, dict) else None
        answer = " ".join(str(recorded if recorded is not None else tool.display or "").split())
    lines = [" ".join(prompt.split())]
    lines += [f"  {'●' if answer and option == answer else '○'} {option}" for option in options]
    if answer and answer not in options:
        lines.append(f"  ✎ {answer}")
    elif not answer and status in {"requested", "running"}:
        lines.append("  … awaiting answer")
    return "\n".join(lines)


def _change_block(tool, shell, local_ui, body):
    """One standalone row per file change or question: ``Edit a.py  +2 −1``.

    File changes start closed: the header row carries the path and counts; Enter opens the
    diff in add/delete colours (first ``_EXPAND_AFTER_LINES`` rows, the rest on a
    second Enter). Without a diff, the labelled tool details (parameters, answer,
    error) are shown instead. A question starts open so its answer is visible.
    """
    from ...ui_support.timeline import tool_heading, tool_status, tool_summary
    status = tool_status(tool)
    rows, added, removed, paths = _change_rows(tool)
    heading = tool_heading(tool).split(" ", 1)[-1]
    suffix = ""
    if status == "failed":
        suffix = tool_summary(tool)
    is_question = tool.name.casefold() == "question"
    compact = _question_detail(tool, status) if is_question else ""
    op_id, out_id = tool.call_id + ":change", tool.call_id + ":change-output"
    detail = "" if rows else body
    if compact and not (shell and shell.verbose):  # /verbose and `i` keep the raw labelled details
        detail = compact
    default_open = tool.name.casefold() == "question"
    block = {"id": tool.call_id, "kind": "change", "status": status, "title": tool.name.title(),
             "text": heading, "heading": suffix, "added": added, "removed": removed,
             "files": len(paths), "path": ", ".join(p for p in paths if p),
             "operation": {"kind": "block_toggle", "id": op_id},
             "inspect_operation": {"kind": "tool_details", "id": tool.call_id},
             "output_operation": {"kind": "block_toggle", "id": out_id} if len(rows) > _EXPAND_AFTER_LINES else None}
    if local_ui:
        block.update(local_ui=True, local_open=default_open, diff_lines=rows, local_detail=detail,
                     fold_lines=_EXPAND_AFTER_LINES)
        return block
    verbose = bool(shell and shell.verbose)
    opened = verbose or default_open != bool(shell and op_id in shell.expanded)  # Enter flips the default
    full = verbose or bool(shell and out_id in shell.expanded)
    if opened and not full and len(rows) > _EXPAND_AFTER_LINES:
        rows = [*rows[:_EXPAND_AFTER_LINES], ["clip", 0, f"… {len(rows) - _EXPAND_AFTER_LINES} more lines · Enter for all"]]
    block.update(collapsed=not opened, diff_lines=rows if opened else [], detail=detail if opened else "")
    return block


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


def _agent_card_model(agent) -> tuple[str, str, str]:
    """A sub-agent card's model text, reasoning effort, and full tier name."""
    from ...ui_support.timeline import _literal, agent_actual_model
    if agent is None:
        return "model pending", "", ""
    tier = (agent.tier or agent.requested_tier or "").strip()
    actual = agent_actual_model(agent) or agent.selected_model
    if actual:
        model = _literal(actual, 120).rsplit("/", 1)[-1]
    else:
        model = "model pending" if agent.status in {"spawned", "running"} else "model unavailable"
    effort = next((t.reasoning_effort for t in agent.body.turns if t.reasoning_effort), "") or ""
    return model, effort, tier


def _agent_card_route(route: dict) -> dict:
    """A sub-agent's quota routing decision (``agent.routed``) with its text control-escaped."""
    def text(value):
        return escape_controls(value) if isinstance(value, str) else value
    skipped = [{**item, **{key: text(item.get(key)) for key in ("reference", "quota", "kind", "label")}}
               for item in route.get("skipped") or [] if isinstance(item, dict)]
    return {**route, "requested": text(route.get("requested")), "first": text(route.get("first")),
            "chosen": text(route.get("chosen")), "skipped": skipped}


def _agent_card_metrics(agent) -> tuple[int | None, int | None]:
    """Return reported tool-call and token counts, preserving unknown values."""
    if agent is None:
        return None, None
    calls = None if (agent.body is None or getattr(agent.body, "external", None)) else sum(len(turn.tools) for turn in agent.body.turns)
    tokens = None if agent.usage_unknown or agent.usage is None else getattr(agent.usage, "total_tokens", None)
    return calls, tokens


def _agent_card_external_tools(agent) -> int | None:
    """Tools an external worker (Claude Code/Antigravity) ran in its own session.

    Counted from the child's reduced ``external.tool`` rows (unique by ``call_id``, so
    requested+completed never double count and replay matches live); a larger
    ``external.finished.tool_calls`` wins when the transcript clipped rows. ``None`` for
    native agents, which keep ``tool_calls``, and while nothing has been seen.
    """
    external = getattr(getattr(agent, "body", None), "external", None)
    if not external:
        return None
    seen = sum(len(turn.tools) for turn in agent.body.turns)
    reported = external.get("tool_calls")
    if isinstance(reported, int) and not isinstance(reported, bool):
        seen = max(seen, reported)
    return seen or None


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


def _chip_operations(message, count):
    """One click operation per attachment chip, in chip order.

    An image chip opens that image's preview (the image block follows its
    ``Attachment: image N`` text block); a document chip opens the message page.
    """
    operations = []
    blocks = message.blocks
    for index, block in enumerate(blocks):
        if block.kind != "text" or not block.text.startswith("\n\nAttachment: "):
            continue
        if block.text.startswith("\n\nAttachment: image "):
            image = next((i for i in range(index + 1, len(blocks)) if blocks[i].kind == "image"), None)
            if image is not None:
                operations.append({"kind": "submitted_image", "id": message.id, "index": image})
                continue
        operations.append({"kind": "message_page", "id": message.id})
    return operations[:count] + [{"kind": "message_page", "id": message.id}] * (count - len(operations))


def _is_prompt(message) -> bool:
    """A user message with content that a person (or Live) sent: not a subagent report or follow-up."""
    from ...ui_support.timeline import _has_message_content
    return (message.role == "user" and _has_message_content(message)
            and (message.origin or {}).get("kind") not in {"subagent_report", "root_followup"})


def _agent_origin_row(message, agents) -> tuple[str, str, dict | None] | None:
    """``(title, tag, operation)`` for a delivered subagent report or a root follow-up row.

    ``report · <agent> · <role> · run <n> · <status>`` (a batch lists every ``id:run``);
    ``follow-up from root · <agent> · run <n>`` on the child's page. The body stays the card text.
    """
    origin = message.origin or {}
    kind = origin.get("kind")
    if kind not in {"subagent_report", "root_followup"}:
        return None
    agents = agents or {}
    def name(agent_id):
        child = agents.get(agent_id)
        return escape_controls(str(agent_id))[:80], escape_controls(str((child.type if child and child.type else "") or ""))[:40]
    batch = [item for item in (origin.get("agents") or []) if isinstance(item, str)]
    agent_id = str(origin.get("agent") or "")
    run = origin.get("run")
    if kind == "root_followup":
        label, role = name(agent_id)
        title = " · ".join(part for part in ("follow-up from root", label, f"run {run}" if run else "") if part)
        return title, "followup", None
    status = escape_controls(str(origin.get("status") or ""))[:40]
    if len(batch) > 1:
        title = f"reports · {len(batch)} subagents · " + ", ".join(escape_controls(item)[:60] for item in batch[:8])
        if len(batch) > 8:
            title += f", +{len(batch) - 8} more"
        return title, "report", None
    label, role = name(agent_id)
    title = " · ".join(part for part in ("report", label, role, f"run {run}" if run else "", status) if part)
    operation = {"kind": "agent_page", "id": agent_id} if agent_id in agents else None
    return title, "report", operation


_REPORT_PREVIEW_LINES = 2


def _agent_report_block(message, agents, shell=None) -> dict | None:
    """The compact ``agent_report`` block for a delivered subagent report, else ``None``.

    Carries the agent name and task title, a preview capped to two content lines and the number
    of lines left out (``count``); the click opens the full message, ``i`` the agent page.
    """
    origin = message.origin or {}
    if origin.get("kind") != "subagent_report":
        return None
    agents = agents or {}
    batch = [item for item in (origin.get("agents") or []) if isinstance(item, str)]
    agent_id = str(origin.get("agent") or "")
    child = agents.get(agent_id)
    lines = [line.strip() for line in message.text.splitlines() if line.strip()]
    if lines and lines[0].startswith("[Subagent report"):
        lines = lines[1:]
    shown = [escape_controls(line) for line in lines[:_REPORT_PREVIEW_LINES]]
    if len(batch) > 1:
        name = f"{len(batch)} subagents"
        task = ", ".join(escape_controls(item)[:60] for item in batch[:4]) + (", …" if len(batch) > 4 else "")
    else:
        name = escape_controls((child.type if child and child.type else "") or agent_id or "subagent")[:60]
        task = escape_controls(" ".join(((child.description or child.task or "") if child else "").split()))[:120]
    block = {"id": message.id + ":report", "kind": "agent_report", "agent_name": name, "task": task,
             "title": name, "text": "\n".join(shown), "count": max(0, len(lines) - len(shown)),
             "status": escape_controls(str(origin.get("status") or ""))[:40],
             "operation": {"kind": "message_page", "id": message.id}}
    if len(batch) <= 1:
        # The same per-agent colour the subagent call's first line uses.
        block["color"] = _agent_color(shell, name)
    if len(batch) <= 1 and child is not None:
        block["inspect_operation"] = {"kind": "agent_page", "id": agent_id}
    return block


def _turn_number(turns, turn):
    """``#N`` of ``turn``: user-initiated turns before it plus one (0 for shell or prompt-less turns)."""
    ordinal = 0
    for item in turns:
        if item.kind != "shell" and any(_is_prompt(m) for m in item.messages):
            ordinal += 1
        if item.id == turn.id:
            return ordinal if item.kind != "shell" and any(_is_prompt(m) for m in item.messages) else 0
    return 0


def _project_turn(turn, shell, agents=None, literal=True, number=None):
    """Blocks of one turn in canonical transcript order and spacing.

    Each block carries ``gap`` (blank rows before it, after margin collapsing),
    so Rust only draws. Reply/tool/thought order follows the durable event seq.
    """
    from ...ui_support.timeline import (
        _has_message_content, _literal, submitted_attachment_summary,
        thought_title, tool_status, turn_footer_text,
        _turn_duration, _text)
    if number is None:  # streamed tail / child page: derive it from the view's turn order
        turns = getattr(getattr(getattr(shell, "controller", None), "view", None), "turns", None) or []
        number = _turn_number(turns, turn)
    local_ui = bool(shell and getattr(shell, "local_transcript", False))
    collapsed = bool(not local_ui and shell and turn.id in shell.collapsed_turns)
    lines = [f"\nTurn {turn.index} · {turn.phase}"] if literal else []
    emit = lines.append if literal else (lambda _line: None)
    entries = []  # (seq, rank, block, top_margin, bottom_margin)
    for context in turn.context:
        if context.kind == "handoff":
            seq = context.data.get("head_seq", 0) + 1
            entries.append((seq, 0, {"id": f"{turn.id}:handoff:{seq}",
                "kind": "literal", "title": "Context handed off",
                "text": "Earlier history is preserved · /handoff read"}, 1, 1, "context"))
    numbered = False  # only the turn's first user card carries #N; a steer folded in does not
    for retry in turn.retries:
        if retry.reason == "provider_overloaded":
            entries.append((retry.event_seq, 1, {"id": f"{turn.id}:retry:{retry.event_seq}", "kind": "literal",
                "title": "Provider overloaded", "text": f"Retry {retry.attempt}/3 after {retry.delay_seconds}s"}, 1, 1, "retry"))
    for index, message in enumerate(turn.messages):
        if message.role == "user":
            if not _has_message_content(message):
                continue
            prompt, chips = submitted_attachment_summary(message)
            text = _user_prompt(prompt)
            first, _, rest = text.partition("\n")
            emit(f"user · text\n{message.text}")
            if collapsed and rest:
                first, rest = first + " …", ""
            from ...ui_support.timeline import _clock
            # Each message's own send time; only the turn's first card may fall back to the
            # turn clock (older recordings), a steer never borrows it.
            sent = message.sent_ts if message.sent_ts is not None else (None if numbered else turn.user_ts)
            block = {"sent_at": _clock(sent), "id": message.id + ":user", "kind": "user", "title": first, "text": rest,
                     "number": 0 if numbered else number, "collapsed": collapsed,
                     "chips": [] if collapsed else chips,
                     "operation": {"kind": "turn_toggle", "id": turn.id}}
            if numbered:
                # A consumed steer lands after the turn's own prompt (reduce appends it; the
                # view keeps no per-message mode), so any later user card is a steer.
                block["status"] = "steer"  # drawn as "steer" beside the card
            report = _agent_report_block(message, agents, shell)
            if report is not None:
                # Compact notification, not a user card: no turn number, no fold, the whole
                # report is one click away (message page) and never leaves the record.
                entries.append((message.event_seq, 2, report, 1, 1, "user"))
                continue
            agent_row = _agent_origin_row(message, agents)
            if agent_row is not None:
                # Not typed by the user: a labelled subagent notification. It never takes #N, and the
                # body (the whole report, clipped by the pool with the clip announced) stays readable.
                block.update(title=agent_row[0], text=message.text if collapsed is False else "",
                             status=agent_row[1], number=0)
                if agent_row[2] is not None:
                    block["operation"] = agent_row[2]
                block.pop("chips", None)
                entries.append((message.event_seq, 2, block, 0, 0, "user"))
                continue
            numbered = True
            if (message.origin or {}).get("kind") == "live":
                block["status"] = "live"  # drawn as "via voice" beside the turn number
            if chips and not collapsed:
                block["chip_operation"] = {"kind": "message_page", "id": message.id}
                block["chip_operations"] = _chip_operations(message, len(chips))
            entries.append((message.event_seq, 2, block, 0, 0, "user"))
            continue
        if message.role == "assistant" and any(block.kind == "thinking" for block in message.blocks):
            thinking = message.thinking
            if thinking.strip():
                expanded = bool(not local_ui and shell and message.id + "thinking" in shell.expanded)
                count = len([line for line in thinking.splitlines() if line.strip()])
                # `Thought for 3.2s`; the reasoning opens beneath it. Collapsed, the first
                # sentence and the line count announce what is hidden. A thought that is the
                # newest event of an unfinished turn and has no closed run is still thinking.
                took = message.thinking_ms
                newest = message.event_seq >= max([message.event_seq, *(t.event_seq for t in turn.tools),
                                                   *(m.event_seq for m in turn.messages)])
                live = took is None and newest and not turn.terminal
                title = "Thinking…" if live else "Thought" if took is None else f"Thought for {format_time(took)}"
                started = next((b.started_ts for b in message.blocks if b.kind == "thinking" and b.started_ts), None)
                suffix = "" if expanded else (f"{thought_title(thinking)} · {count} lines ▸" if count > 1 else f"{thought_title(thinking)} ▸")
                block = {"id": message.id + "thinking", "kind": "thought", "title": title,
                         "status": "running" if live else "completed",
                         "started_ms": round(started * 1000) if started else 0, "elapsed_ms": took or 0,
                         "text": suffix, "detail": _literal(thinking) if expanded and not local_ui else "",
                         **({"local_ui": True, "local_detail": _literal(thinking), "local_open": False} if local_ui else {}),
                         "operation": {"kind": "block_toggle", "id": message.id + "thinking"}}
                entries.append((message.event_seq, 0, block, 1, 1, "thought"))
                emit(f"assistant · thinking\n{thinking}")
        if message.text if message.role == "assistant" else _has_message_content(message):
            block = {"id": message.id + "text", "kind": "markdown", "text": message.text}
            if message.role == "assistant" and not message.done and not turn.terminal:
                block["streaming"] = True  # the client glows the write head (DESIGN OVERHAUL step 4)
            entries.append((message.event_seq, 2, block, 0, 1, "assistant"))
            emit(f"assistant · text\n{message.text}")
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
        operation = {"kind": "block_toggle", "id": tool.call_id + ":detail"}
        # `i` on the row opens the full labelled details (tool_details), which the relevant-only view omits.
        inspect = {"kind": "tool_details", "id": tool.call_id}
        if tool.name.casefold() in TASK_TOOL_NAMES:
            from ...ui_support.timeline import _task_children, _task_header
            child = next(iter(_task_children(tool, agents or {})), None)
            head, running = _task_header(tool, child, 0, with_model=False)
            name, _, phrase = head.partition(" ")[2].partition(" · ")
            phrase = " ".join(phrase.split())[:64]
            title = f"{name} Subagent — {phrase}"
            model, effort, tier = _agent_card_model(child)
            from ...ui_support.timeline import task_refusal
            refusal = task_refusal(tool, child)
            if refusal:
                model = "not started"
            tool_calls, tokens = _agent_card_metrics(child)
            args = getattr(tool, "input", None) or getattr(tool, "arguments", None) or {}
            agent_name = (
                (child.type if child and child.type else "")
                or (args.get("subagent_type") if isinstance(args, dict) else "")
                or (args.get("agent") if isinstance(args, dict) else "")
                or "task"
            )
            run_start = child.last_run_ts if child and child.runs > 1 and child.status == "spawned" and child.last_run_ts else (child.spawned_ts if child else None)
            started_ms = int(run_start * 1000) if run_start else 0
            elapsed_ms = int((child.completed_ts - child.spawned_ts) * 1000) if child and child.completed_ts and child.spawned_ts else 0
            if elapsed_ms < 0:
                elapsed_ms = 0
            operation = {"kind": "agent_page", "id": child.id} if child is not None else None
            # Never the tool details: a click opens the subagent page, so only a failure shows inline.
            detail = f"Error: {child.error}" if child is not None and child.error else ""
            if refusal:
                # Muted, not red: the native card tints "failed"/"Error:" with the error colour.
                detail = refusal[1]
            if child is not None and child.body.external:
                from ...ui_support.external_worker import header_rows, task_note
                provider = next((v for k, v in header_rows(child.body.external) if k == "Provider"), "")
                ext_note = f" · {provider.removesuffix(' (external worker)') or 'External worker'} (external, own tools)"
                phrase = f"{phrase}{ext_note}"
                title += ext_note
                detail = "\n".join(part for part in (task_note(child.body.external), detail) if part)
            from ...ui_support.timeline import agent_state, agent_state_label
            state = agent_state(child)
            block = {"id": tool.call_id, "kind": "task", "title": title,
                     "status": "running" if running else "done",
                     # `idle · run 2` / `stopped by system: …`; empty for a plain running or finished child.
                     "state": refusal[0] if refusal else agent_state_label(child) if state in {"interrupted", "stopped"} or (state == "running" and child.runs > 1) else "",
                     "model": model, "effort": effort, "tier": tier, "metrics": "",
                     "agent_name": agent_name, "task": phrase,
                     "tool_calls": tool_calls, "external_tool_calls": _agent_card_external_tools(child), "tokens": tokens, "started_ms": started_ms, "elapsed_ms": elapsed_ms,
                     "color": _agent_color(shell, agent_name), "operation": operation,
                     "text": "", "detail": detail, "inspect_operation": inspect}
            if child is not None and child.route:
                block["route"] = _agent_card_route(child.route)
            entries.append((tool.event_seq, 3, block, 1, 1, "task"))
            continue
        if tool.name.casefold() in STANDALONE_TOOLS and tool.name.casefold() not in CHANGE_TOOLS:
            block = _change_block(tool, shell, local_ui, full_body or sections_to_text(sections))
            entries.append((tool.event_seq, 3, block, 1, 1, "change"))
            continue
        else:
            text = cached_row
            if expandable:
                first, _, rest = text.partition("\n")
                text = f"{first} · {out_lines} lines {'▾' if expanded else '▸'}" + (f"\n{rest}" if rest else "")
        if tool_status(tool) == "running":  # the native client animates this slot (docs/ratatui-parity.md)
            text = text.replace(SPINNER_FRAMES[0], SPINNER_SLOT, 1)
        block = {"id": tool.call_id, "kind": "tool", "status": tool_status(tool), "text": text,
                 "detail": body if detail_open else "", "operation": operation,
                 "inspect_operation": inspect,
                 "output_operation": {"kind": "block_toggle", "id": out_id} if (detail_open or local_ui) and expandable else None}
        if local_ui or detail_open:
            # The relevant-only expanded body; `detail`/`local_detail` keep the full labelled
            # details for /verbose and for `inspect_operation`.
            block["signal"] = tool_signal(tool, getattr(shell, "workspace", "") or None).wire()
            preview = shell_preview(tool, getattr(shell, "workspace", "") or None)
            if preview is not None:
                block["shell"] = preview
                block["input_operation"] = {"kind": "tool_input", "call_id": tool.call_id}
        if local_ui:
            block.update(local_ui=True, local_detail=full_body, fold_lines=output_end, local_open=False)
        if tool.name.casefold() in CHANGE_TOOLS:
            # Its own row; `activity_entries` swaps in `_member` when consecutive changes group.
            change = _change_block(tool, shell, local_ui, full_body or sections_to_text(sections))
            entries.append((tool.event_seq, 3, {**change, "_member": block}, 1, 1, "change"))
            continue
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
    grouped = activity_entries(entries, tools_by_id, diff_members, turn=turn, shell=shell, local_ui=local_ui)
    # A running group shows its newest members as a tree under `Working` (verb_groups);
    # the old one-member live preview is gone. Local expansion still reveals every item.
    entries = grouped
    if collapsed:
        entries = [entry for entry in entries if entry[5] == "user"]
    blocks, previous = [], None
    for _, _, block, top, bottom, role in entries:
        if previous is not None:
            if previous[0] == "user":
                top = max(top, 1)
            elif (previous[0] in {"tool", "change"} and role == "assistant") or (previous[0] == "assistant" and role in {"tool", "change"}):
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
            # One blank row above the turn footer; the next turn's card (or the composer
            # margin) supplies the blank row below it.
            bottom = previous[1] if previous else 0
            blocks.append({"id": turn.id + ":summary", "kind": "summary", "text": footer, "gap": max(1, bottom)})
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
    defs = getattr(shell, "agent_definitions", {}) or {}
    if not (isinstance(identity, dict) and identity.get("name") == name or getattr(identity, "name", None) == name):
        identity = defs.get(name, {}) if isinstance(defs, dict) else {}
    color = identity.get("color") if isinstance(identity, dict) else getattr(identity, "color", None)
    return color or agent_color(name)


def _context_blocks(shell, view):
    """The request-context header that opens every conversation."""
    from ...ui_support.context_header import HeaderBlock, header_blocks
    from ...ui_support.context import _compact_tokens
    preview = shell.preview
    if preview is not None and hasattr(preview, "tools"):
        color = _agent_color(shell, getattr(getattr(shell, "controller", None), "agent_name", (getattr(preview, "agent", {}) or {}).get("name", "build")))
        found = header_blocks(preview, color)
    else:
        placeholder = _agent_color(shell, getattr(getattr(shell, "controller", None), "agent_name", "build"))
        found = [HeaderBlock(key, label, "", "", None, placeholder) for key, label in (
            ("system", "System prompt"), ("tools", "Tools"), ("agents", "AGENTS.md"), ("skills", "Skills"), ("mcp", "MCP"))]
    def token_status(block):
        return f"~{_compact_tokens(block.tokens)} tokens" if block.tokens is not None else "tokens unknown"

    def active(block):
        """False when the block contributes nothing: zero or unknown tokens with no content, or not loaded."""
        return bool(block.tokens) or bool(block.inventory) or (block.tokens is None and bool(block.body))

    # Closed by default (GROK_BUILD_TUI_PLAN §8C): `tokens` is all a closed row shows. The body
    # fields ride along marked local so Rust opens a header without a round trip. No agent colour:
    # headers are grey, and `active` picks the lighter or darker grey.
    return [{"id": "context:" + block.key, "kind": "context", "title": block.label,
             "text": "\n".join(block.inventory) if block.key in {"tools", "skills", "mcp"} else block.body,
             "local_preview": block.inventory_note,
             "tokens": f"~{_compact_tokens(block.tokens)}" if block.tokens is not None else "~?",
             "active": active(block),
             "local_ui": True, "local_open": False,
             "status": token_status(block),  # deprecated: the pre-§8C spelling of `tokens`
             "gap": 0, "operation": {"kind": "context_show", "key": block.key},
             "inspect_operation": {"kind": "context_show", "key": block.key}}
            for block in found]


def _compact_header(shell, view):
    """Compact chip strip with a right-aligned estimated-token footer."""
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
    chips = [{**chip, "counts": counts.get(chip["id"], []) if preview else [], "gap": 0} for chip in chips]
    return [{"id": "context:header", "kind": "context_header", "gap": 1,
             "members": chips, "operation": {"kind": "context_menu"}}]


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
    from ...ui_support.details import DEFAULT_WIDTH, diff_preview_lines, mcp_rows, modified_files, session_rows
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
    manager = getattr(shell, "manager", None)
    if manager is not None and manager.is_active(getattr(controller, "session", view.session_id)):
        # The manager has no role or effort and a pinned model: say so instead of showing the ordinary defaults.
        selected = {"title": "Manager"}
        rows = [("Kind", "Manager · pinned model, no role, shell or fork"),
                *[(label, MANAGER_MODEL_REF if label == "Model" else value)
                  for label, value in rows if label not in {"Agent", "Effort"}]]
    from .codemode import sidebar_rows
    rows = [*rows, *sidebar_rows(view)]
    rows = [("ID", str(getattr(controller, "session", view.session_id))),
            ("Title", selected.get("title") or getattr(controller, "title", None) or "Untitled"), *rows,
            ("Provider", getattr(controller, "provider", None) or "unavailable"),
            ("Cost", str(getattr(view.usage, "cost", None) or "unavailable")),
            ("Created", stamp(view.turns[0].started_ts) if view.turns else "unavailable"),
            ("Updated", stamp(view.turns[-1].updated_ts) if view.turns else "unavailable"),
            ("Workspace", str(getattr(shell, "workspace", "") or "unavailable")),
            ("Branch", str(getattr(shell, "branch", "") or (report.get("git") or {}).get("branch") or "unavailable"))]
    return {
        "tab": shell.preferences.values["details_tab"] if shell else "Session",
        "width": shell.preferences.values.get("details_width", DEFAULT_WIDTH) if shell else DEFAULT_WIDTH,
        "logs_header": [["Session", str(getattr(controller, "session", view.session_id))], ["Daemon pid", str(daemon.get("pid") or "unavailable")],
                        ["Socket", escape_controls(str(daemon.get("socket") or "unavailable"))],
                        ["Client", str(getattr(getattr(getattr(controller, "client", None), "health", None), "version", "unavailable"))],
                        ["Bridge", "3 (schema 1/2 compatible)"], ["Nexus", version("nexus-harness")],
                        *[["Timing", line] for line in (getattr(getattr(shell, "logs", None), "trace", []) + getattr(getattr(shell, "logs", None), "python_trace", []))]],
        "session": [[label, escape_controls(value)] for label, value in rows],
        "files": [{"path": escape_controls(f.path), "added": f.added, "removed": f.removed, "created": f.created,
                   "open": (is_open := bool(shell and f.path in shell.open_files)),
                   "diff": [escape_controls(line) for line in diff_preview_lines(f.hunks)] if is_open or bool(shell and getattr(shell, "local_transcript", False)) else []} for f in files],
        "files_summary": f"+{added} -{removed} across {len(files)} file{'s' if len(files) != 1 else ''}" if files else "",
        "mcp": [[tone, escape_controls(text), escape_controls(note)]
                for tone, text, note in mcp_rows(mcp_report, error=getattr(shell, "mcp_error", None))],
        "jobs": shell.jobs.projection() if shell is not None and hasattr(shell, "jobs") else {},
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


def _waiting_inputs(view, slot_wait=None):
    """Queued inputs that really wait for a running turn.

    A send to an idle session is journaled as ``input.queued`` and only becomes the
    user message at ``input.consumed``, after ``turn.started``. The head entry is
    the one starting the turn while the session is idle or the active turn has no
    user message yet; it is that turn's own input, not a steer, so it is not listed.
    The exception is a session the host holds back on its global turn cap
    (``slot_wait``): nothing has started, so the head is a waiting message.
    """
    queue = list(view.input_queue)
    turn = getattr(view, "active_turn", None)
    running = getattr(view, "phase", "") in {"running", "awaiting_permission", "awaiting_input"} and turn is not None
    parked = bool(slot_wait and slot_wait.get("waiting"))
    if queue and not parked and (not running or not any(m.role == "user" for m in turn.messages)):
        queue = queue[1:]
    return queue


def _queue_lines(view, slot_wait=None):
    """Messages waiting for the running turn,."""
    from ...ui_support.text import sanitize
    queue = _waiting_inputs(view, slot_wait)
    rows = [f"{'Queued' if item.mode == 'queue' else item.mode.title()} · " + sanitize("".join(
        block.get("text", "") for block in item.content if isinstance(block, dict)), 160) for item in queue[:3]]
    if len(queue) > 3:
        rows.append(f"+{len(queue) - 3} more queued")
    return [escape_controls(row) for row in rows]




def _queue_items(view, slot_wait=None):
    """Pending steering/queued messages for the boxed list above the composer.

    Each row carries its durable ``queued_id`` so ↑ ↓ ✕ act on exactly that
    message; the tag is ``S`` (steer), ``Q`` (queue) or ``I`` (interrupt).

    ``can_up``/``can_down`` exist only for queued items, and only when the adjacent waiting
    item in the real queue is also queued, because a move swaps exactly with that neighbour:
    swapping across groups would change nothing the user can see (steers are consumed at the
    next model boundary, queued items one turn at a time after the turn ends). Steering and
    interrupt items are never reordered by this client (they are promoted or removed
    instead). The first waiting item has no movable neighbour above it: the one before it in
    the host queue is the turn's own, unlisted input.
    """
    from ...ui_support.text import sanitize
    tags = {"steer": "S", "queue": "Q", "interrupt": "I"}
    waiting = _waiting_inputs(view, slot_wait)

    def queued(item):
        return item.mode not in {"steer", "interrupt"}

    def movable(index, other):
        return queued(waiting[index]) and 0 <= other < len(waiting) and queued(waiting[other])

    return [{"id": item.queued_id, "tag": tags.get(item.mode, "Q"), "mode": item.mode,
             "can_up": movable(index, index - 1), "can_down": movable(index, index + 1),
             "text": escape_controls(sanitize("".join(
                 block.get("text", "") for block in item.content if isinstance(block, dict)), 160))}
            for index, item in enumerate(waiting[:64]) if item.queued_id]


async def queue_edit(shell, controller, queued_id, op):
    """Apply one ``queue_edit`` action from a waiting-message control.

    ``up``/``down`` reorder queued items, ``steer``/``interrupt`` promote (upgrades
    only; the host refuses the rest), ``remove`` drops. Removing a steering item
    (``S``/``I``) also asks the host for its full content and hands the text to the
    composer's one-shot restore channel with ``restore_if_empty``: Rust owns the
    draft, so it applies the text only when the draft is still empty.
    """
    view = controller.view
    waiting = _waiting_inputs(view, getattr(controller, "slot_wait", None))
    index = next((i for i, item in enumerate(waiting) if item.queued_id == queued_id), None)
    if index is None or op not in {"up", "down", "remove", "steer", "interrupt"}:
        return
    item = waiting[index]
    steering = item.mode in {"steer", "interrupt"}
    if op in {"up", "down"}:
        if not steering:
            await shell.client.queue_move(controller.session, queued_id, -1 if op == "up" else 1)
    elif op in {"steer", "interrupt"}:
        await shell.client.queue_set_mode(controller.session, queued_id, op)
    elif not steering:
        await shell.client.queue_remove(controller.session, queued_id)
    else:
        removed, blocks = await shell.client.queue_remove_content(controller.session, queued_id)
        text, restored = await _restore_steer_attachments(shell, blocks)
        if removed and (text or restored):
            shell.composer_restore = text
            shell.composer_restore_if_empty = text
            shell.composer_restore_attachments = restored
            shell.composer_restore_note = f"restored from steer #{index + 1}"


_ATTACHMENT_HEAD = "\n\nAttachment: "


async def _restore_steer_attachments(shell, blocks):
    """Split a removed steering message into draft text and re-prepared attachments.

    The host stored each attachment as a text block starting ``Attachment: <label> · <name>``
    (images: followed by the image block). When the draft already holds attachments nothing
    is re-attached (the same "only into an empty draft" rule as the text). Returns
    ``(text, attached_count)``; the text gets the new ``[image N]`` markers so deleting one
    drops its attachment (``reconcile_attachment_markers``). Rust applies or drops the whole
    restore; if it drops it, the next ``draft_changed`` removes the unreferenced attachments.
    """
    import base64
    from pathlib import PurePosixPath

    body, found = [], []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type", "text")
        if kind == "text" and block.get("text", "").startswith(_ATTACHMENT_HEAD):
            found.append({"head": block["text"][len(_ATTACHMENT_HEAD):], "image": None})
        elif kind == "image" and found and found[-1]["image"] is None:
            found[-1]["image"] = block
        elif kind == "text" and not found:
            body.append(block.get("text", ""))
    text = "".join(body)
    if not found or getattr(shell, "attachments", None):
        return text, 0
    attached = 0
    for entry in found[:8]:
        head, _, rest = entry["head"].partition("\n")
        old_label, _, tail = head.partition(" · ")
        try:
            if entry["image"] is not None:
                name = tail.rsplit(" · ", 2)[0] if tail.count(" · ") >= 2 else tail
                data = base64.b64decode(entry["image"].get("data") or "")
                item = await shell.client.prepare_attachment(name=name, data=data)
            else:
                name = PurePosixPath(tail or "attachment").stem + ".md"
                item = await shell.client.prepare_attachment(name=name, data=rest.lstrip("\n").encode())
        except Exception as exc:  # noqa: BLE001 - the text still restores; say what was lost
            shell.flash(f"Could not restore {old_label}: {exc}", "warning")
            continue
        shell.attachments.append(item)
        marker = shell.attachment_marker(len(shell.attachments) - 1)
        old_marker = f"[{old_label}]"
        text = text.replace(old_marker, marker) if old_marker in text else (text + (" " if text else "") + marker)
        attached += 1
    return text, attached


def _tab_rows(controller, shell):
    """Tabs with the current one marked; the current tab reads "working" while its turn runs."""
    rows = []
    manager = getattr(shell, "manager", None)
    for tab in shell.tabs:
        if manager is not None and manager.is_active(tab["id"]):
            continue  # the manager is a view behind the top-bar button, never a tab
        current =tab["id"] == controller.session and tab.get("workspace") == shell.workspace
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


def _prompt_of(view, failures: list[str]) -> dict | None:
    """The permission or question prompt a reduced session view is waiting on, control-safe."""
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
        data = permission.to_dict()
        prompt = {"kind": "permission", "id": permission.id,
                  "lines": labelled(data),
                  **_guarded(failures, "Permission details", lambda: permission_fields(permission), {}),
                  "choices": [{"label": choice.label, "value": choice.value,
                               "key": choice.key, "disabled": choice.disabled}
                              for choice in approval_choices(data)]}
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
    return prompt


def _agent_state_blocks(agent) -> list[dict]:
    """The labelled end-of-page state of a subagent that is not running and not plainly finished."""
    from ...ui_support.timeline import agent_state, agent_state_label
    state = agent_state(agent)
    if state == "stopped":
        by = escape_controls(agent.stopped_by or "unknown")
        reason = escape_controls(agent.stopped_reason or "no reason recorded")
        return [{"id": agent.id + ":state", "kind": "literal", "title": f"Subagent stopped · by {by}",
                 "text": f"Reason: {reason}\nThe conversation log is kept.", "gap": 1}]
    if state == "interrupted":
        return [{"id": agent.id + ":state", "kind": "literal", "title": f"Subagent {agent_state_label(agent)}",
                 "text": "The run was cut short by a daemon restart.", "gap": 1}]
    return []


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
    blocks = _safe_blocks([b for b in projected if b.get("kind") != "user" or b.get("status") == "followup"])
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


def _session_title(session_id: str, shell=None) -> str:
    """Use the host's session summary, never the opaque session identifier."""
    manager = getattr(shell, "manager", None)
    if manager is not None and manager.is_active(session_id):
        return "Manager"
    for row in getattr(shell, "sessions", ()):
        if row.get("id") == session_id:
            return escape_controls(row.get("name") or row.get("title") or "New session")
    return "New session"


def project(controller: TuiController, revision: int, error: str = "", shell=None, literal: bool = True) -> dict:
    """Project labelled, control-safe context and transcript from canonical state.

    Each section is built independently: one failing section is replaced by a
    labelled notice and the others still render.
    """
    view = controller.view
    slot_wait = getattr(controller, "slot_wait", None)
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
    live_cards = [] if on_agent_page or not getattr(shell, "live", None) else _guarded(
        failures, "Live calls", lambda: sorted(shell.live.card_blocks(view), key=lambda card: card[0]), [])
    live_cards = [(ts, _safe_blocks([block])[0]) for ts, block in live_cards]

    def cards_before(moment):
        """Live cards that began before ``moment``, in start order."""
        while live_cards and live_cards[0][0] <= moment:
            blocks.append({**live_cards.pop(0)[1], "gap": 1})

    # `context.notice` events the reducer keeps as diagnostics (e.g. paused subagent-report
    # delivery) draw as labelled rows placed by time among the turns, never hidden.
    # Worktree creates and moves (`view.worktree.moves`) are durable, reason-labelled rows in the same
    # time-ordered list.
    notices = [] if on_agent_page else sorted(
        [(d.ts if d.ts is not None else 0.0, d.seq, d) for d in view.diagnostics if d.type == "context.notice"
         and isinstance(d.data, dict) and d.data.get("message")]
        + [(m.ts if m.ts is not None else 0.0, m.seq, m) for m in view.worktree.moves],
        key=lambda item: item[:2])

    def notices_before(moment):
        while notices and notices[0][0] <= moment:
            _, _, item = notices.pop(0)
            if isinstance(item, RootMoveView):
                blocks.append({"id": f"root-move:{item.seq}", "kind": "literal", "title": "Worktree",
                               "text": escape_controls(root_move_text(item)), "gap": 1})
                continue
            title = {"subagent_wake_paused": "Subagent reports paused", "tool_call_nudge": "Tool-call wrap-up reminder"}.get(str(item.data.get("kind")), "Notice")
            from ...view.notices import model_visible
            context = model_visible(item.data)
            identity = json.dumps([str(shell.workspace) if shell else "", view.session_id, item.seq], separators=(",", ":"))
            if not context and shell and identity in shell.preferences.values["dismissed_notices"]:
                continue
            blocks.append({"id": f"notice:{item.seq}", "kind": "context_notice" if context else "notice", "title": title,
                           "text": escape_controls(str(item.data.get("message"))), "gap": 1,
                           "operation": None if context else {"kind": "dismiss_notice", "id": identity}})

    ordinal = 0  # user-initiated turns so far (shell turns and turns without a prompt don't count)
    for turn in () if on_agent_page else view.turns:
        number = 0
        if turn.kind != "shell" and any(_is_prompt(m) for m in turn.messages):
            ordinal += 1
            number = ordinal
        cards_before(turn.started_ts if turn.started_ts is not None else float("inf"))
        notices_before(turn.started_ts if turn.started_ts is not None else float("inf"))
        saved_flags = tuple((item.seq, item.sha) for item in view.worktree.commits if item.sha in turn.commit_shas)
        turn_flags = (*flags, saved_flags, tuple((tool.call_id, tuple(dependencies.get(tool.call_id, ())),
                                    tuple(id(view.agents.get(child)) for child in tool.child_agent_ids))
                                   for tool in turn.tools if tool.child_agent_ids or tool.call_id in dependencies), number)
        cache_key = f"{getattr(controller, 'session', view.session_id)}\x00{turn.id}"
        cached = shell.turn_cache.get(cache_key) if shell else None
        if cached and cached[0] is turn and cached[1] == turn_flags:
            turn_lines, turn_blocks = cached[2], cached[3]
        else:
            turn_lines, turn_blocks = _guarded(failures, f"Turn {turn.id}", lambda: _project_turn(turn, shell, view.agents, literal, number),
                                               ([], [{"id": turn.id, "title": "Turn", "text": "This turn could not be rendered", "kind": "literal"}]))
            saved_rows = []
            for commit in view.worktree.commits:
                if commit.sha in turn.commit_shas:
                    saved_rows.append((commit.seq, f"Saved turn · {commit.sha[:10]} · {commit.insertions} added · {commit.deletions} removed"))
            turn_blocks = [*turn_blocks, *[{
                "id": f"{turn.id}:saved:{seq}", "kind": "summary", "gap": 1, "text": label,
                "operation": {"kind": "saved_turn_diff", "seq": seq},
            } for seq, label in sorted(set(saved_rows))]]
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
    cards_before(float("inf"))
    notices_before(float("inf"))
    linked = {agent_id for turn in view.turns for tool in turn.tools for agent_id in tool.child_agent_ids}
    from ...ui_support.timeline import agent_state_label
    for agent in () if on_agent_page else view.agents.values():
        if agent.id in linked:  # shown by its Task card
            continue
        blocks.append({"id": agent.id, "title": escape_controls(f"Agent {agent.id} · {agent_state_label(agent)}"), "text": escape_controls(agent.description),
                       "kind": "literal", "operation": {"kind": "agent_page", "id": agent.id}})
    prompt = _prompt_of(view, failures)
    if prompt is None and shell is not None and getattr(shell, "live", None) is not None:
        # The Live call is attached to the manager: its approval reaches the user in any session.
        prompt = shell.live.manager_prompt(getattr(controller, "session", view.session_id),
                                           lambda manager_view: _prompt_of(manager_view, failures))
    details_panel = {} if on_agent_page else _guarded(failures, "Details sidebar", lambda: _details_panel(controller, view, shell), {})
    # Projection failures stay in the transcript (durable context); shell notices are toasts.
    notice = "\n".join(part for part in [error, *failures] if part)
    if notice:
        lines.append(escape_controls(f"Error: {notice}"))
        blocks.append({"id": "notice", "title": "Notice", "text": escape_controls(notice), "kind": "literal"})
    display_view = _context_display_view(view, shell)
    used, window, _measured = context_measure(display_view)
    manager_entry = _guarded(failures, "Manager", lambda: shell.manager.entry(), {}) if shell and getattr(shell, "manager", None) else {}
    from .manager_board import board as _manager_board
    manager_board = _guarded(failures, "Manager board", lambda: _manager_board(shell), None) if shell else None
    snapshot = {"schema": 1, "revision": revision, "title": _session_title(view.session_id, shell),
            "status": view.phase,
            "panel_layout": shell.panel_layout if shell else "modal",
            "panel_format": shell.panel_format if shell else "plain",
            "panel_toggle": getattr(shell, "panel_toggle", None) if shell else None,
            "preview_image": base64.b64encode(shell.preview_image).decode("ascii") if shell and shell.preview_image else "",
            "preview_image_media": shell.preview_image_media if shell else "",
            "panel_loading": bool(shell and shell.panel_loading),
            "blocks": blocks,
            "transcript_verbose": bool(shell and shell.verbose),
            "local_ui_enabled": bool(shell and getattr(shell, "local_transcript", False)),
            "ui_ack": getattr(shell, "ui_ack", 0) if shell else 0,
            "commands": getattr(shell, "static_commands", []) if shell else [],
            "command_help": getattr(shell, "command_help", {}) if shell else {},
            "context_lines": [escape_controls(line) for line in context_lines],
            "context_used": used,
            "context_window": window,
            "context_marks": [],
            "context_tiers": _context_tiers(display_view),
            "context_usage": context_usage(view),
            "context_note": "",
            "context_label": _context_label(display_view),
            "queue_lines": _queue_lines(view, slot_wait),
            "queue_items": _queue_items(view, slot_wait),
            "slot_wait": slot_wait,
            "turn_status": turn_status(view),
            "agents_running": running_agents(view),
            "composer_key": json.dumps([shell.workspace if shell else "", getattr(controller, "session", view.session_id)]),
            "nav": _settings_nav(shell),
            "update_notice": "",  # announced once as a toast, never pinned to the footer
            "toasts": list(shell.toasts) if shell else [],
            "notifications_open_seq": shell.notifications_open_seq if shell else 0,
            "disconnected": bool(
                shell and str(getattr(shell, "notice", "")).startswith("Disconnected")
            ),
            "provider": getattr(controller, "provider", None) or "",
            "effort": "" if manager_entry.get("active") else getattr(controller, "reasoning_effort", None) or "default",
            "theme": shell.preferences.values["theme"] if shell else "nexus-dark",
            "sessions_sidebar": shell.preferences.values["sessions_sidebar"] if shell else True,
            "details_sidebar": shell.preferences.values["details_sidebar"] if shell else True,
            "context_preview": shell.preferences.values["context_preview"] if shell else True,
            "tabs_vertical": shell.preferences.values["tabs_vertical"] if shell else False,
            "centered_layout": shell.preferences.values["centered_layout"] if shell else False,
            "sessions": shell.sessions if shell else [],
            "archived_label": shell.archived_label if shell else "",
            "sessions_truncated": bool(shell and shell.sessions_truncated),
            "dashboard": _dashboard_state(shell, prompt),
            "sidebar": _sidebar_state(shell),
            "settings_page": (shell.workflows.settings_page if shell.panel_title and shell.panel_title == shell.workflows.page_title else None) if shell else None,
            "settings_overview": (shell.workflows.settings_overview if shell.panel_title and shell.panel_title == shell.workflows.page_title else None) if shell else None,
            "tabs": _tab_rows(controller, shell) if shell else [],
            "breadcrumb": escape_controls(_display_breadcrumb(shell.breadcrumb)) if shell else "",
            "worktree": escape_controls(str(getattr(shell, "worktree", "") or ""))[:80] if shell else "",
            "worktree_kind": str(getattr(shell, "worktree_kind", "") or "") if shell else "",
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
            "live": (getattr(shell, "live", None) and shell.live.bar()) or {} if shell else {},
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
            "restore_if_empty": bool(shell and shell.composer_restore
                                     and shell.composer_restore == shell.composer_restore_if_empty),
            "restore_attachments": shell.composer_restore_attachments
                                   if shell and shell.composer_restore and shell.composer_restore == shell.composer_restore_if_empty else 0,
            "restore_note": escape_controls(shell.composer_restore_note)
                            if shell and shell.composer_restore and shell.composer_restore == shell.composer_restore_if_empty else "",
            "agent": "manager" if manager_entry.get("active") else getattr(controller, "agent_name", "build"),
            "model": escape_controls(
                (shell.model_names.get((getattr(controller, "provider", None) or "", getattr(controller, "model", None) or "")) if shell else None)
                or getattr(controller, "model", None) or "default"),
            "manager": manager_entry,
            "manager_board": manager_board,
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
            prompt_text = next((str(block.get("text") or "") for message in preview.messages[:1]
                               for block in message.get("blocks", []) if block.get("text")), "")
            if not prompt_text:
                prompt_text = next((str(tool.input.get("prompt") or "") for turn in view.turns for tool in turn.tools
                                    if tool.call_id == agent.parent_call_id), agent.task or agent.description)
            from ...ui_support.timeline import _literal
            page_color = _agent_color(child_shell, (getattr(preview, "agent", {}) or {}).get("name", "build"))
            # Reuse the root header with only the child's host-projected request.
            # The task remains a prompt card, followed by thoughts, tools and reply.
            first, _, rest = prompt_text.partition("\n")
            child_blocks = [{"id": agent.id + ":task", "kind": "user", "title": _literal(first),
                             "text": _literal(rest), "gap": 0}]
            if not context and agent.status == "spawned":
                child_blocks.append({"id": agent.id + ":wait", "kind": "literal", "text": "Waiting for the first request…", "gap": 1})
            child_blocks = _safe_blocks(child_blocks)
            if shell.preferences.values["context_preview"]:
                child_blocks.extend(_safe_blocks(_compact_header(child_shell, agent.body)))
            from ...ui_support.timeline import agent_model_label
            from ...ui_support.external_worker import external_blocks
            external_head, external_foot = external_blocks(agent.id, agent.body.external)
            child_blocks.extend(_safe_blocks(external_head))
            for turn in agent.body.turns:
                child_blocks.extend(_child_turn_blocks(shell, agent, turn, flags))
            child_blocks.extend(_safe_blocks(external_foot))
            child_blocks.extend(_safe_blocks(_agent_state_blocks(agent)))
            from dataclasses import replace
            # The page's context meter is the child's own accounting; never the root's counters.
            child_display = _context_display_view(agent.body, child_shell)
            child_used, child_window, _child_measured = context_measure(child_display)
            snapshot.update(context_used=child_used, context_window=child_window,
                            context_tiers=_context_tiers(child_display),
                            context_usage=context_usage(child_display),
                            context_label=_context_label(child_display) if child_used is not None else "")
            agent_status = "failed" if agent.is_error or agent.ok is False else agent.status
            snapshot.update(blocks=child_blocks, agent_page=agent.id,
                status="running" if agent.status == "spawned" else "done",
                sessions_sidebar=False, sessions=[], tabs=[], prompt=None, queue_lines=[],
                completions=[], voice_phase="idle", voice_preview="",
                agent=agent.type or "subagent", model=agent_model_label(agent),
                title=f"{agent.type or 'Subagent'} · {agent.description or agent.task}",
                details_panel=_details_panel(SimpleNamespace(
                    agent_name=agent.type, model=agent_model_label(agent),
                    reasoning_effort=next((t.reasoning_effort for t in reversed(agent.body.turns) if t.reasoning_effort), None)),
                    replace(agent.body, phase=agent_status), shell))
    if shell and getattr(shell, "inline_images_enabled", False):
        from ...ui_support.native_images import inline_images
        snapshot["inline_images"] = inline_images(view, shell)
    snapshot["agent_color"] = _agent_color(shell, snapshot["agent"])
    if snapshot.get("agent_page"):
        snapshot["agent_color"] = page_color
    for block in snapshot["blocks"]:
        if not block.get("rev"):
            block["rev"] = _revision(block)
    return snapshot


async def dismiss(shell) -> bool:
    """The native ``dismiss`` action (Esc, Back, outside click); False when it was ignored."""
    # Only the provider usage modal sets panel_loading; it stays dismissable while its fetch is
    # pending (back() bumps panel_revision, so the refresh drops its result).
    if shell.panel_loading and shell.panel_title != "Provider usage":
        return False
    if await shell.workflows.speak_stop():
        return False
    form = shell.workflows.form
    if form and not form["secret"] and form["status"] and not form.get("saved", False):
        shell.workflows.menu("Discard unsaved changes?", [("Keep editing", {"kind": "back"}),
            ("Discard draft", {"kind": "discard_form"})], [form["status"], form["body"]])
    else:
        shell.workflows.back()
        await shell.workflows.refresh_page()
    return True


async def run(workspace: Path, session: str, binary: Path, client=None, reconnect=None, *, process=None) -> int:
    client = client or await open_client(workspace)
    controller = TuiController(client, session)
    shell = ShellActions(controller)
    from ..cli.commands import SPECS
    shell.static_commands = sorted((spec.name, list(spec.aliases)) for spec in SPECS if not spec.hidden)
    shell.command_help = {spec.name: spec.summary for spec in SPECS if not spec.hidden}
    shell.local_transcript = True
    shell.native_reload = True
    shell.sidebar.enabled = True  # the left sidebar is a terminal feature
    shell.ui_ack = 0
    shell.workspace = str(workspace)
    shell.tabs = [{"id": session, "title": session, "workspace": str(workspace), "state": "idle"}]
    shell.reconnect = reconnect or (lambda: open_client(workspace))
    controller.reconnect = lambda: shell.reconnect()
    shell.history = load_history()
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
    update_coalescer = None
    async def update(event=False, *, immediate=True, _scheduled=False):
        nonlocal revision, event_sent_at, event_kind, allowed_operations, displayed_prompt, stream_turn
        if event is not False and event is not None:
            event_kind = event.type
            if event.type == "manager.show" or (event.type == "session.linked" and isinstance(event.data, dict)
                    and event.data.get("kind") == "managed"):
                # before ingest: only events past the replayed cursor count
                try:
                    shell.manager.on_event(event, session=controller.session, fresh=event.seq > controller.cursor)
                except Exception as exc:  # noqa: BLE001 - a bad request never breaks the stream
                    shell.logs.add_notice("warning", f"{event.type} ignored: {type(exc).__name__}")
            stream_turn =event.turn if event.type in STREAM_EVENTS and (stream_turn in {None, event.turn}) else False
            if event.type in {"agent.spawned", "agent.completed", "agent.failed", "agent.cancelled"}:
                try:
                    await shell.workflows.refresh_ownership()
                except Exception as exc:
                    shell.notice = f"Ownership refresh failed: {type(exc).__name__}: {exc}"
        elif not _scheduled:
            stream_turn = False
        if event is not False and event is not None:
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
        from .worktree_bar import sync_bar_with_root
        sync_bar_with_root(shell, controller.view)  # an agent move (session.root_set) redraws the bar now
        revision += 1
        async with write_lock:
            # The literal `lines` projection is a test seam; the native typed blocks already
            # carry the transcript and the full-detail actions, so it is not built here.
            started = time.perf_counter()
            snapshot = stream_projection.project(controller, shell, revision, stream_turn, _project_turn) if stream_turn else None
            full_projection = snapshot is None
            if full_projection:
                snapshot = project(controller, revision, shell=shell, literal=False)
                stream_projection.remember(snapshot, controller, shell)
            stream_turn = None
            # The streamed tail reuses the last full snapshot; the dashboard is never stale.
            snapshot["dashboard"] = _dashboard_state(shell, snapshot.get("prompt"))
            snapshot["sidebar"] = _sidebar_state(shell)
            trace.elapsed("project", started)
            snapshot["event_sent_at"] = event_sent_at if trace.enabled else 0
            snapshot["event_kind"] = event_kind if trace.enabled else ""
            displayed_prompt = snapshot["prompt"]
            if full_projection:
                allowed_operations = {json.dumps(op, sort_keys=True) for op in [
                *[item["operation"] for item in snapshot.get("inline_images", [])],
                *[item.get("action", {}).get("operation") for item in snapshot.get("toasts", []) if item.get("action")],
                *[item.get("operation") for item in shell.items],
                *[item.get("toggle_operation") for item in shell.items if not item.get("toggle_locked")],
                *[item.get(key) for item in shell.items for key in ("move_up", "move_down", "remove")],
                (snapshot.get("live") or {}).get("operation"),  # the Live pane's open/close
                (snapshot.get("manager_board") or {}).get("toggle"),  # the board heading
                *[row.get("operation") for row in (snapshot.get("manager_board") or {}).get("rows", [])],
                *_block_operations(snapshot["blocks"])] if op}
            snapshot["completion_bell"] = controller.completion_bell
            shell.notify_completion()
            # Poll ticks that change nothing are not resent; one-shot composer
            # fields (restore/insert) always are, since they repeat legitimately.
            started = time.perf_counter()
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
                shell.composer_restore_if_empty = ""
                shell.composer_restore_attachments = 0
                shell.composer_restore_note = ""
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
    live_task = None
    startup_task = None
    def polled_state():
        """Everything one poll cycle can change, as a comparable text; unchanged means no repaint."""
        return repr((shell.manager.state, shell.manager.error,
                     shell.tabs, shell.sessions, shell.sessions_truncated, shell.archived_label,
                     shell.workflows.agent_context, shell.mcp_report, shell.mcp_error,
                     shell.update_notice, getattr(shell, "preview_at", None), shell.notice,
                     shell.breadcrumb, shell.branch, shell.worktree, shell.worktree_kind, shell.panel_title, shell.items, shell.panel_lines,
                     shell.sidebar.fingerprint(), shell.jobs.projection()))
    async def poll():
        preview_cursor = -1
        last_polled = None
        while True:
            await asyncio.sleep(.2 if shell.voice.phase != "idle" else 1 if shell.logs.open or shell.jobs.active else 3)
            shell.focus.tick()  # keeps this client's foreground claim fresh on the host
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
                await shell.workflows.refresh_ownership()
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
                    update = await shell.client.update_status(announce=True)
                    shell.update_notice = f"{sanitize(str(update.available), 80)} available: {sanitize(str(update.command), 120)}" if update.available else ""
                    if getattr(update, "announce", False):  # once per release, then only /update shows it
                        shell.toast(f"Nexus {sanitize(str(update.available), 80)} is available", "info",
                                    key="update", body=f"Run {sanitize(str(update.command), 120)} in your terminal to update.")
                except Exception:  # noqa: BLE001 - the notice is advisory
                    shell.update_notice = ""
                try:  # one-time "new session database" notice; the host marks it shown
                    database_notice = await shell.client.database_notice()
                    if database_notice:
                        shell.toast("New session database", "info", key="database-reset", body=sanitize(database_notice, 400))
                except Exception:  # noqa: BLE001 - the notice is advisory
                    pass
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
            async def jobs():
                await shell.jobs.poll()  # only while the Jobs tab is drawn
            async def manager():
                if shell.manager.due():
                    await shell.manager.refresh()  # failures are kept as the entry's text, never raised
            for label, work in (("Sessions", sessions), ("Branch", lambda: refresh_branch(shell)), ("Dictation", dictation),
                                ("Health", health), ("Context", context), ("Logs", logs), ("Jobs", jobs), ("Manager", manager),
                                ("Sidebar", shell.sidebar.poll)):
                await section(label, work)
            if failures:
                shell.notice = "; ".join(failures)
            # Voice, the log tail and the dictation panel change on their own; otherwise a
            # cycle that found nothing new sends no snapshot, so an idle chat stays idle.
            polled = polled_state()
            live = shell.voice.phase != "idle" or shell.logs.open or shell.panel_title == "Local dictation"
            if polled == last_polled and not live and not failures:
                continue
            last_polled = polled
            try:
                await update(immediate=False)
            except Exception as exc:
                shell.notice = f"Display update failed: {type(exc).__name__}: {exc}"
    try:
        await controller.bootstrap()
        await shell.refresh_preview(session)
        if process is None:
            process = await asyncio.create_subprocess_exec(
                str(binary), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024)
        from .run import progress
        await progress(process, 3)  # the session loaded: milestone 3, before the first snapshot
        shell.worktree = _worktree_name(workspace)
        shell.worktree_kind = "worktree" if shell.worktree else ""
        shell.breadcrumb = str(workspace)  # the branch joins it when the doctor report arrives
        await update()
        controller.resume(update)
        shell.focus.report(controller.session)

        async def finish_startup():
            """Everything the first paint does not need, concurrently, each repainting when it lands."""
            async def breadcrumb():
                try:
                    await refresh_branch(shell)
                except Exception:  # noqa: BLE001 - the path alone is enough
                    pass

            async def model_names():
                try:  # full catalogue names for the composer controls; ids remain the fallback
                    shell.model_names = {(str(row.get("provider") or ""), str(row.get("id") or "")): str(row["name"])
                                         for row in await client.list_models() if row.get("name")}
                except Exception:  # noqa: BLE001 - display names are advisory
                    shell.model_names = {}

            async def setup():
                try:
                    status = await shell.client.setup_status()
                    if status.required:  # first run: open Settings on Providers (its page offers "Choose default model")
                        await shell.workflows.open_page("providers")
                except Exception as exc:  # noqa: BLE001 - surfaced as a toast
                    shell.flash(str(exc), "error")

            async def step(work):
                await work()
                await update(immediate=False)

            async def manager():
                await shell.manager.refresh()  # the pinned entry and the manager chat chrome

            async def sidebar():
                await shell.sidebar.activate()  # a sidebar left open last time is live again; closed does nothing

            await asyncio.gather(step(breadcrumb), step(model_names), step(setup), step(manager), step(sidebar),
                                 return_exceptions=True)

        startup_task = asyncio.create_task(finish_startup())
        poll_task = asyncio.create_task(poll())

        async def live_tick():
            """Keep the call bar's clock and level meters moving while a call is open."""
            while True:
                await asyncio.sleep(.25)
                if shell.live.state != "idle":
                    try:
                        await update(immediate=False)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - cosmetic refresh
                        shell.notice = f"Display update failed: {type(exc).__name__}: {exc}"
        live_task = asyncio.create_task(live_tick())
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
                    if not await shell.submit(
                        action["text"], action.get("mode", "steer"), session=action.get("session") or None,
                    ):
                        process.stdin.close()
                        break
                    if action["text"].lstrip().startswith("!"):  # a shell command may have switched branch
                        before = shell.breadcrumb
                        await refresh_branch(shell)
                        if shell.breadcrumb != before:
                            await update(immediate=False)
                elif action["type"] == "draft_state":
                    if action.get("generation") == shell.generation:
                        shell.note_draft_state(bool(action.get("nonempty")))
                elif action["type"] == "draft_changed":
                    shell.reconcile_attachment_markers(action["text"])
                elif action["type"] == "cancel":
                    if not await shell.workflows.speak_stop():
                        await shell.cancel()
                elif action["type"] == "dismiss":
                    if not await dismiss(shell):
                        continue
                elif action["type"] == "command":
                    if not await shell.submit(action["text"]):
                        break
                elif action["type"] == "pick":
                    if action.get("generation") == shell.generation:
                        item = next((item for item in shell.items if item["command"] == action["text"]), None)
                        if item:
                            shell.panel_title = ""
                            shell.items = []
                            if not await shell.submit(item["command"]):
                                break
                elif action["type"] == "answer":
                    current = displayed_prompt
                    if current and current["id"] == action["text"]:
                        value = action.get("value", "")
                        target = current.get("session") or controller.session  # a Live-attached manager prompt names its session
                        allowed = [choice["value"] for choice in current["choices"] if not choice["disabled"]]
                        if current["kind"] == "permission":
                            if value not in allowed:
                                raise ValueError("Unavailable permission choice")
                            resolved = await shell.client.resolve_permission(target, current["id"], value)
                            if not resolved:
                                shell.notice = "Permission already resolved by another client"
                        else:
                            resolved, error = await shell.client.answer_question(target, current["id"], value)
                            if error:
                                raise ValueError(error)
                            if not resolved:
                                shell.notice = "Question already answered by another client"
                elif action["type"] == "operation":
                    operation = action.get("operation")
                    if operation_allowed(shell, allowed_operations, operation) and not shell.panel_loading:
                        if operation["kind"] in {"block_toggle", "turn_toggle"} and shell.local_transcript:
                            continue  # disclosure belongs exclusively to Rust
                        if operation["kind"] == "dismiss_notice":
                            shell.preferences.set("dismissed_notices", [*shell.preferences.values["dismissed_notices"], operation["id"]])
                        elif operation["kind"] == "block_toggle":
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
                elif action["type"] == "queue_edit":
                    await queue_edit(shell, controller, action.get("text"), action.get("key"))
                elif action["type"] == "context_header":
                    key = action.get("key")
                    if key in {"system", "agents", "tools", "skills", "mcp"}:
                        await shell.workflows.operate({"kind": "context_show", "key": key})
                elif action["type"] == "sidebar":
                    await shell.sidebar.handle(action)
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
                    if tab in {"Session", "Files", "Changes", "MCP", "Jobs", "Logs", "Browser", "Git"}:
                        tab = "Files" if tab == "Changes" else tab
                        shell.preferences.set("details_tab", tab)
                        if tab in {"Browser", "Git"}:
                            await shell.sidebar.refresh(force=True)
                        shell.logs.open = tab == "Logs" and shell.preferences.values["details_sidebar"]
                        if shell.logs.open and shell.logs.visible:
                            await shell.logs.poll()
                        if tab != "Jobs":
                            shell.jobs.log = None  # leaving the tab closes the viewer
                elif action["type"] in {"job_select", "job_log", "job_log_close", "job_stop", "job_background"}:
                    job_id = action.get("text")
                    if shell.jobs.active and isinstance(job_id, str):
                        await {"job_select": shell.jobs.select, "job_log": shell.jobs.open_log,
                               "job_stop": shell.jobs.stop, "job_background": shell.jobs.background,
                               "job_log_close": lambda _id: shell.jobs.close_log()}[action["type"]](job_id)
                elif action["type"] == "details_width":
                    width = action.get("width")
                    if isinstance(width, int) and not isinstance(width, bool):
                        from ...ui_support.details import clamp_width
                        shell.preferences.set("details_width", clamp_width(width))
                elif action["type"] == "details_visible":
                    shell.logs.visible = bool(action.get("open"))
                    shell.jobs.visible = bool(action.get("jobs"))
                    if shell.jobs.active:
                        await shell.jobs.poll(force=True)
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
                elif action["type"] in {"cycle_effort", "cycle_agent"} and shell.manager.is_active():
                    raise ValueError(manager_ui.REFUSED_COMMANDS["/effort" if action["type"] == "cycle_effort" else "/agent"])
                elif action["type"] in {"manager_open", "manager_live", "manager_toggle"}:
                    await {"manager_live": shell.manager.live, "manager_toggle": shell.manager.toggle,
                           "manager_open": shell.manager.open}[action["type"]]()
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
                elif action["type"] == "session_tree":
                    row = next((row for row in shell.sessions if row["id"] == action["text"] and row["workspace"] == action["workspace"]), None)
                    if row:
                        await shell.switch_project(row["workspace"], row["id"])
                        await shell.workflows.operate({"kind": "ownership_tree", "id": row["id"]})
                elif action["type"] == "session_actions":
                    row = next((row for row in shell.sessions if row["id"] == action["text"] and row["workspace"] == action["workspace"]), None)
                    if row:
                        await shell.switch_project(row["workspace"], row["id"])
                        await shell.workflows.operate({"kind": "session_actions", **row})
                elif action["type"] == "tab_close":
                    await shell.close_tab(action["workspace"], action["text"])
                    await shell.close_selector()
                elif action["type"] == "session_open":
                    if any(row["id"] == action["text"] and row["workspace"] == action["workspace"] for row in [*shell.sessions, *shell.tabs]):
                        await shell.switch_project(action["workspace"], action["text"])
                        await shell.close_selector()
                shell.ui_ack = max(shell.ui_ack, int(action.get("ui_sequence", 0)))
                if not controller.running:
                    controller.resume(update)
                await update()
            except Exception as exc:
                shell.flash(error_text(exc), "error")
                await update()
        if shell.reload_request is not None:
            return shell.reload_request
        return await process.wait()
    finally:
        shell.workflows.close_dashboard(force=True)
        for task in list(shell.sidebar.tasks):
            task.cancel()
        await shell.manager.aclose()
        await background.close()
        await shell.preferences.flush()
        trace.finish()
        if shell.usage_task:
            shell.usage_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await shell.usage_task
        with contextlib.suppress(Exception):
            await shell.aclose_branch_task()
        if startup_task:
            startup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await startup_task
        if live_task:
            live_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await live_task
        if poll_task:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
        with contextlib.suppress(Exception):
            await shell.voice.discard()
        with contextlib.suppress(Exception):
            await shell.live.shutdown()
        with contextlib.suppress(Exception):
            await shell.focus.clear()
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
                from .run import release
                await release(process)


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
