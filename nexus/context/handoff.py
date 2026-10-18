"""Versioned, bounded, deterministic same-session handoff briefs.

No model or tool is invoked here. The source transcript remains authoritative;
this intentionally conservative first brief is a redacted extract, not a claim
that a generative summarizer preserved every fact.
"""
from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from ..model.message import Message, Text
from ..util import redact_secrets

BRIEF_VERSION = 1
MAX_BRIEF_CHARS = 6000


def strip_truncation(text: str) -> str:
    """Drop trailing clip markers a nested prior brief carries, so they never accumulate.

    Only marker lines at the very end are removed; a literal "[truncated]" inside
    the text is user content and stays.
    """
    marker = "\n[truncated]"
    end = len(text)
    cut = -1
    while True:
        stop = end
        while stop > 0 and text[stop - 1].isspace():
            stop -= 1
        if stop < len(marker) or not text.startswith(marker, stop - len(marker)):
            break
        end = cut = stop - len(marker)
    if cut < 0:
        return text
    while cut > 0 and text[cut - 1].isspace():
        cut -= 1
    return text[:cut]


def safe_brief(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("handoff summary must be text")
    text = redact_secrets(text)
    text = "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C") else " " for c in text)
    text = strip_truncation(text.strip()).strip()
    if not text:
        raise ValueError("handoff summary must not be empty")
    if len(text) > MAX_BRIEF_CHARS:
        text = text[:MAX_BRIEF_CHARS - 14] + "\n[truncated]"
    return text


_TRUNCATED = "\n[truncated]"
_MIN_LINE = 12


def fit_brief(brief: str, limit: int) -> str | None:
    """Shorten a structured brief to ``limit`` chars keeping every line/section.

    Lines are cut to one shared cap (short lines stay whole), so the heading,
    history pointer, intent, tool/file summaries and state all survive in
    shortened form. Returns ``None`` when the limit is too small for that.
    """
    if len(brief) <= limit:
        return brief
    lines = brief.split("\n")
    budget = limit - len(_TRUNCATED) - (len(lines) - 1)
    if budget < _MIN_LINE * len(lines):
        return None
    low, high = _MIN_LINE, max(map(len, lines))
    while low < high:  # largest cap whose clipped lines still fit the budget
        mid = (low + high + 1) // 2
        if sum(min(len(line), mid) for line in lines) <= budget:
            low = mid
        else:
            high = mid - 1
    cap = low
    clipped = [line if len(line) <= cap else line[:cap - 1] + "…" for line in lines]
    return "\n".join(clipped) + _TRUNCATED


def build_brief(messages: Sequence[Message], *, handle=None, boundary=0) -> str:
    """Keep recent user intent and assistant decisions; never inline tool blobs."""
    entries = []
    for message in messages:
        if message.role not in {"user", "assistant"}:
            continue
        text = "\n".join(b.text for b in message.content if isinstance(b, Text)).strip()
        if text:
            entries.append(f"{message.role}: {strip_truncation(text)[:1200]}")
    first = next((entry for entry in entries if entry.startswith("user:")), "(none)")
    state = ""
    if handle is not None:
        import json
        from ..view import apply, initial_state
        from ..session.records import EventRecord
        state_view = initial_state(handle.id)
        for record in handle.read().records:
            if isinstance(record, EventRecord):
                state_view = apply(state_view, record.event)
        view = state_view.to_dict()
        body = view
        todos = None
        jobs = {}
        for record in handle.read().records:
            if isinstance(record, EventRecord) and record.event.type == "todo.updated":
                todos = record.event.data
            if isinstance(record, EventRecord) and record.event.type == "shell.started":
                jobs[str(record.event.data.get("shell_id", record.seq))] = record.event.data
            elif isinstance(record, EventRecord) and record.event.type == "shell.completed":
                jobs.pop(str(record.event.data.get("shell_id", "")), None)
        original = next((m for m in handle.messages if m.role == "user" and any(
            isinstance(b, Text) for b in m.content)), None)
        if original is not None:
            first = "\n".join(b.text for b in original.content if isinstance(b, Text))[:600]
        latest = next((m for m in reversed(handle.messages) if m.role == "user" and any(
            isinstance(b, Text) for b in m.content)), None)
        if latest is not None:
            state += "\nLatest user input: " + "\n".join(b.text for b in latest.content if isinstance(b, Text))[:600]
        owner_rows = []
        if handle.id not in handle._store._drafts:
            owner_rows, _, _ = handle._store.ownership_tree_rows(
                handle.id, namespace=handle._store.namespace, max_depth=2, limit=50)
        owner_rows = [{k: r.get(k) for k in ("id", "namespace", "agent_id", "kind", "root_id")}
                      for r in owner_rows]
        state += "\nSession state (not moved): " + json.dumps({
            "todos": todos, "live_children": body.get("agents"),
            "open_shell_jobs": jobs, "worktree": view.get("worktree"),
            "owned_children": owner_rows,
        }, default=str)[:1800]
        state += "\nPending queue IDs: " + ", ".join(handle.queued_ids)[:500]
    calls = []
    changed = []
    for message in messages:
        for block in message.content:
            if hasattr(block, "name"):
                calls.append(str(block.name))
                args = getattr(block, "input", {})
                if block.name in {"Write", "Edit", "write", "edit"} and isinstance(args, dict):
                    changed.append(str(args.get("file_path", args.get("path", ""))))
            if getattr(block, "is_error", False):
                calls.append("ERROR " + str(getattr(block, "tool_use_id", "")))
    return safe_brief(
        "Handoff brief v1 (redacted transcript extract; consult history for details).\n"
        f"Earlier history: this session, seq 1..{max(0, boundary - 1)} (not deleted).\n"
        "First user intent: " + first[:600] + "\n"
        "Recent tool calls: " + ", ".join(calls[-20:])[:500] + "\n"
        "Files changed (tool-derived): " + ", ".join(dict.fromkeys(changed[-100:]))[:600] + "\n"
        + state + "\nRecent decisions / previous brief:\n"
        + ("\n".join(entries[-4:]) or "No textual decisions in the retired context.")
    )


def active_context(records, *, cutoff: int | None = None, head=None):
    """Derive model messages and their durable seqs without changing transcript."""
    from ..session.records import MessageRecord, SummaryRecord

    visible = [r for r in records if cutoff is None or r.seq <= cutoff]
    if head is None:
        head = next((r for r in reversed(visible) if isinstance(r, SummaryRecord)
                     and r.strategy == "handoff"), None)
    messages = [r for r in visible if isinstance(r, MessageRecord)
                and (head is None or r.seq >= head.kept_from_seq)]
    if head is None:
        return [r.message for r in messages], tuple(r.seq for r in messages)
    return ([Message(role="user", content=[Text(text=head.text)]),
             *(r.message for r in messages)],
            (0, *(r.seq for r in messages)))
