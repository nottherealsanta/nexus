"""Verb groups: the quiet one-line summary of a run of tool calls and thoughts.

Moved out of ``prototype._project_turn`` (plans/in-progress/GROK_BUILD_TUI_PLAN.md §4.1).
Membership is unchanged: every tool and thought joins the activity run except
``task``/``subagent`` rows and standalone ``change`` blocks, and visible replies end a run.
Vocabulary follows Grok Build's verb groups (``xai-grok-pager`` ``verb_group.rs``),
reimplemented here. The projection holds no clock: every figure derives from durable
event timestamps, so replay produces the same blocks.
"""
from __future__ import annotations

#: Tools whose rows are never part of an activity run.
SUBAGENT_TOOLS = frozenset({"task", "subagent"})

#: tool name -> (past verb, present verb, singular noun, plural noun)
CATEGORIES = {
    "bash": ("Ran", "Running", "command", "commands"),
    "run": ("Ran", "Running", "command", "commands"),
    "read": ("Read", "Reading", "file", "files"),
    "edit": ("Edited", "Editing", "file", "files"),
    "write": ("Wrote", "Writing", "file", "files"),
    "apply_patch": ("Applied", "Applying", "patch", "patches"),
    "grep": ("Searched", "Searching", "pattern", "patterns"),
    "glob": ("Searched", "Searching", "pattern", "patterns"),
    "ls": ("Listed", "Listing", "dir", "dirs"),
    "list": ("Listed", "Listing", "dir", "dirs"),
    "webfetch": ("Fetched", "Fetching", "website", "websites"),
    "websearch": ("Searched", "Searching", "website", "websites"),
    "mcpsearch": ("Searched", "Searching", "MCP tool", "MCP tools"),
    "mcpcall": ("Called", "Calling", "MCP tool", "MCP tools"),
}
THOUGHT = ("Thought", "Thinking", "time", "times")


def format_time(ms: int) -> str:
    """Grok's ``format_time``: ``671ms``, ``3.2s``, ``1m20s``."""
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms // 60_000}m{(ms // 1000) % 60:02d}s"


def split_runs(entries, tools_by_id):
    """Partition sorted entries into ``("run", [entries])`` and ``("entry", entry)`` items.

    Diff rows ride with their tool and are skipped. A run is a maximal stretch of
    tool and thought entries; anything else (replies, ``task`` rows, changes) ends it.
    """
    pending = []
    for entry in entries:
        if entry[5] == "diff":
            continue
        name = tools_by_id[entry[2]["id"]].name.casefold() if entry[5] == "tool" else ""
        if entry[5] in {"tool", "thought"} and name not in SUBAGENT_TOOLS:
            pending.append(entry)
            continue
        if pending:
            yield "run", pending
            pending = []
        yield "entry", entry
    if pending:
        yield "run", pending


def _category(tool):
    """The tool's verb category; an external worker's tools are never counted as Nexus's."""
    if tool.provenance:
        owner = tool.provenance.removesuffix(" (own tools)").removesuffix(" own tools").strip()
        verb = f"{owner or 'External worker'} ran"
        return (verb, verb, "tool", "tools")
    found = CATEGORIES.get(tool.name.casefold())
    if found:
        return found
    title = tool.name.title()
    return (title, title, "call", "calls")


def _label(counts, running):
    """``Ran 2 commands, Read 3 files``; a category with a running member uses its present verb."""
    return ", ".join(
        f"{present if category in running else past} {count} {one if count == 1 else many}"
        for category, count in counts.items()
        for past, present, one, many in [category])


def _span_ms(member, tool):
    """(start, end) epoch milliseconds from durable timestamps; 0 where unknown."""
    if tool is None:
        start, elapsed = member.get("started_ms", 0), member.get("elapsed_ms", 0)
        return start, (start + elapsed if start and elapsed else 0)
    start = tool.started_ts or tool.requested_ts
    start_ms = round(start * 1000) if start else 0
    if tool.finished_ts:
        return start_ms, round(tool.finished_ts * 1000)
    if start_ms and tool.duration_ms is not None:
        return start_ms, start_ms + tool.duration_ms
    return start_ms, 0


def build_run(pending, tools_by_id, diff_members, *, turn, shell, local_ui):
    """One ``tool_group`` entry for a run, or the thoughts themselves when it has no tool.

    A run of thoughts alone is not a group (Grok's ``RunScan::folds``): the thought
    rows are drawn as they are. Group identity stays ``turn.id + ":activity:" +
    first durable member id`` so streaming additions keep local disclosure.
    """
    if not any(role == "tool" for *_, role in pending):
        return list(pending)
    from ...ui_support.timeline import diff_sections
    from .transcript_data import tool_presentation
    seq, rank, first, *_ = pending[0]
    group_id = turn.id + ":activity:" + first["id"]
    opened = bool(not local_ui and shell and (shell.verbose or group_id in shell.expanded))
    members, counts, running_categories, spans = [], {}, set(), []
    for _, _, block, _, _, role in pending:
        member, tool = block, None
        if role == "thought":
            category = THOUGHT
        else:
            tool = tools_by_id[block["id"]]
            name = tool.name.casefold()
            category = _category(tool)
            _, _, heading_full, summary = tool_presentation(tool, shell)
            heading = heading_full.split(" ", 1)[-1]
            if tool.diff:
                diffs = diff_sections(tool.diff)
                summary = f"+{sum(d.added for d in diffs)} −{sum(d.removed for d in diffs)}"
            if tool.provenance and heading_full:
                heading_full += f" · {tool.provenance}"
            start_ms, end_ms = _span_ms(block, tool)
            if name not in SUBAGENT_TOOLS:
                member = {**block, "heading": heading_full + (f" · {summary}" if summary else ""),
                    "title": tool.name.title(), "text": heading.removeprefix(tool.name.title()).strip() + (f" · {summary}" if summary else ""),
                    "started_ms": start_ms, "elapsed_ms": max(0, end_ms - start_ms) if start_ms and end_ms else 0}
            member = {**member, "members": diff_members[tool.call_id]}
        if member.get("status") == "running":
            running_categories.add(category)
        spans.append(_span_ms(member, tool))
        # Thoughts are members but never part of the heading's counts.
        if category is not THOUGHT:
            counts[category] = counts.get(category, 0) + 1
        members.append(member)
    running = bool(running_categories)
    failed = [m for m in members if m.get("status") == "failed"]
    live = next((m for m in reversed(members) if m.get("status") == "running"), None)
    starts = [s for s, _ in spans if s]
    ends = [e for _, e in spans if e]
    started = min(starts) if starts else 0
    elapsed = max(0, max(ends) - started) if started and ends and not running else 0
    block = {"id": group_id, "kind": "tool_group", "text": _label(counts, set()),
        "running_label": _label(counts, running_categories) if running else "",
        "status": "running" if running else "completed", "count": len(members), "collapsed": not opened,
        "failures": len(failed), "started_ms": started, "elapsed_ms": elapsed,
        "elapsed_label": format_time(elapsed) if elapsed else "",
        "members": members if opened or local_ui else [],
        # A failure is never folded away: Rust draws these under the folded header.
        "failed_members": failed,
        **({"live_member": live} if live is not None else {}),
        **({"local_ui": True, "local_open": False} if local_ui else {}),
        "operation": {"kind": "block_toggle", "id": group_id}}
    return [(seq, rank, block, 1, 1, "tool")]


def activity_entries(entries, tools_by_id, diff_members, *, turn, shell, local_ui):
    """``entries`` with each activity run replaced by its group (see :func:`build_run`)."""
    grouped = []
    for kind, item in split_runs(entries, tools_by_id):
        if kind == "run":
            grouped.extend(build_run(item, tools_by_id, diff_members, turn=turn, shell=shell, local_ui=local_ui))
        else:
            grouped.append(item)
    return grouped


__all__ = ["CATEGORIES", "activity_entries", "build_run", "format_time", "split_runs"]
