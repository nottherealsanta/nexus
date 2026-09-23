"""Structured, transport-neutral session export (Phase 8a0).

An export is a **pure rendering** of a consistent log prefix: it never mutates a
session, never takes an exclusive lock, and never reaches outside the session
content contract. Three formats are supported, each aimed at a different
consumer:

* ``json`` — a single versioned document (``version``, ``session`` metadata,
  ``messages``, ``events``, ``summaries``) that round-trips losslessly through
  ``msgspec``; ``Image``/``Document`` bytes ride as base64 exactly as the log
  stores them.
* ``markdown`` — a human-readable transcript. Binary blocks are summarized
  (``[image image/png]``) rather than inlined, so a large attachment cannot turn
  a document into an unreadable blob.
* ``jsonl`` — the authoritative log re-encoded one record per line. It is
  produced from the *consistent* read, so a crash tail is dropped and every line
  is valid JSON.

The module imports only the store/model/events contracts, so it stays at L3 and
is safe for the manager to call while holding a shared read lock.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import msgspec

from ..model.message import (
    ContentBlock,
    Document,
    Image,
    Message,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)
from .store import ReadResult, SessionRecord

#: Export document version. Bumped when the JSON shape changes so a consumer can
#: refuse an unknown document instead of misreading it.
EXPORT_VERSION = 1

#: Canonical format names plus the ``md`` convenience alias.
FORMATS = ("json", "markdown", "jsonl")
_ALIASES = {"md": "markdown", "markdown": "markdown", "json": "json", "jsonl": "jsonl"}

#: Title length cap, in characters.
TITLE_LIMIT = 80


def derive_title(messages: Sequence[Message], *, limit: int = TITLE_LIMIT) -> str:
    """A short, single-line title from the first user ``Text`` block.

    Returns ``""`` when the session has no user text yet. Tool results (which
    also carry the ``user`` role) are skipped, and whitespace is collapsed so a
    multi-line prompt still yields one title line.
    """
    for message in messages:
        if message.role != "user":
            continue
        for block in message.content:
            if isinstance(block, Text):
                text = " ".join(block.text.split())
                if text:
                    return text[:limit]
    return ""


def last_activity(
    records: Sequence[SessionRecord], *, fallback: Any = None
) -> float:
    """The newest record timestamp, or ``fallback``'s mtime, or ``0.0``.

    Record timestamps are the durable signal; a legacy log written before
    timestamps were recorded falls back to the file's mtime so ordering in
    ``list`` stays useful.
    """
    latest = 0.0
    for record in records:
        ts = getattr(record, "ts", 0.0) or 0.0
        if isinstance(ts, (int, float)) and ts > latest:
            latest = float(ts)
    if latest == 0.0 and fallback is not None:
        try:
            latest = float(fallback.stat().st_mtime)
        except OSError:
            latest = 0.0
    return latest


def document(
    session_id: str,
    read: ReadResult,
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the structured, msgspec-encodable export document."""
    return {
        "version": EXPORT_VERSION,
        "session": dict(meta or {"id": session_id}),
        "messages": list(read.messages()),
        "events": list(read.events()),
        "summaries": list(read.summaries()),
    }


def to_json(
    session_id: str, read: ReadResult, *, meta: dict[str, Any] | None = None
) -> str:
    """Render the versioned JSON document, bytes preserved as base64."""
    return msgspec.json.encode(document(session_id, read, meta=meta)).decode("utf-8")


def to_jsonl(read: ReadResult) -> str:
    """Render the consistent log prefix as JSONL, one record per line.

    The read is already crash-tail tolerant, so the output contains only valid
    records; a malformed tail is dropped rather than copied.
    """
    lines = [msgspec.json.encode(record).decode("utf-8") for record in read.records]
    return "".join(line + "\n" for line in lines)


def to_markdown(
    session_id: str, read: ReadResult, *, meta: dict[str, Any] | None = None
) -> str:
    """Render a human-readable transcript with a metadata header."""
    meta = dict(meta or {"id": session_id})
    title = str(meta.get("title") or "") or session_id
    lines: list[str] = [f"# {title}", ""]
    lines.append(f"- **Session:** `{session_id}`")
    lines.append(f"- **State:** {meta.get('state', 'idle')}")
    lines.append(f"- **Messages:** {len(read.messages())}")
    lines.append(f"- **Last seq:** {meta.get('last_seq', read.next_seq)}")
    lines.append(f"- **Viewers:** {meta.get('viewers', 0)}")
    activity = meta.get("last_activity", 0.0)
    if activity:
        lines.append(f"- **Last activity:** {_iso(float(activity))}")
    lines.append("")
    lines.append("## Transcript")
    lines.append("")
    for message in read.messages():
        lines.append(f"### {message.role}")
        lines.append("")
        rendered = _render_content(message.content)
        lines.append(rendered if rendered else "_(empty)_")
        lines.append("")
    summaries = read.summaries()
    if summaries:
        lines.append("## Summaries")
        lines.append("")
        for record in summaries:
            label = record.summary_id or f"seq {record.seq}"
            lines.append(f"- **{label}** ({record.strategy or 'unknown'})")
            if record.text:
                lines.append("")
                lines.append(record.text)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render(
    session_id: str,
    read: ReadResult,
    *,
    format: str = "json",
    meta: dict[str, Any] | None = None,
) -> str:
    """Dispatch to the requested format; unknown formats raise ``ValueError``."""
    if not isinstance(format, str):
        raise TypeError("format must be a string")
    canonical = _ALIASES.get(format.strip().lower())
    if canonical is None:
        raise ValueError(
            f"Unsupported export format {format!r}; expected one of {FORMATS}"
        )
    if canonical == "json":
        return to_json(session_id, read, meta=meta)
    if canonical == "jsonl":
        return to_jsonl(read)
    return to_markdown(session_id, read, meta=meta)


def _render_content(blocks: Sequence[ContentBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Text):
            parts.append(block.text)
        elif isinstance(block, Thinking):
            parts.append(f"**Thinking:** {block.text}")
        elif isinstance(block, ToolUse):
            parts.append(
                f"**Tool call `{block.name}`** (`{block.id}`)\n"
                f"```json\n{json.dumps(block.input, ensure_ascii=False)}\n```"
            )
        elif isinstance(block, ToolResult):
            marker = " (error)" if block.is_error else ""
            parts.append(f"**Tool result `{block.tool_use_id}`**{marker}")
            body = _render_content(block.content)
            if body:
                parts.append(body)
        elif isinstance(block, Image):
            parts.append(f"[image {block.media_type}]")
        elif isinstance(block, Document):
            label = f" {block.title}" if block.title else ""
            parts.append(f"[document {block.media_type}{label}]")
    return "\n\n".join(part for part in parts if part)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


__all__ = [
    "EXPORT_VERSION",
    "FORMATS",
    "TITLE_LIMIT",
    "derive_title",
    "document",
    "last_activity",
    "render",
    "to_json",
    "to_jsonl",
    "to_markdown",
]
