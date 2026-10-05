"""Presentable tool call details: every parameter and output, none of the JSON.

Nexus exists so context is clearly presented to users and agents. A tool call
opened from the timeline therefore renders as labelled sections (Overview,
Parameters, Code, Progress, Summary, Result, Error, Context, Metrics, Diff)
made of ``label: value`` rows. Nested objects flatten to dotted paths
(``edits[0].old``) and multi-line values become indented blocks. Values are
redacted and control-safe; anything clipped says so. The browser port lives in
``ui/web/js/tool-details.js`` and must stay in step (plan section 14.7).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from ..view import ToolCallView
from .text import escape_controls
from .timeline import tool_status

VALUE_LIMIT = 20_000
SECTION_ROWS = 400
_MAX_DEPTH = 8


@dataclass(frozen=True)
class DetailRow:
    """One ``label: value`` pair; ``block`` values render below their label."""

    label: str
    value: str
    block: bool = False
    #: Nesting level: fields of a list item render one level under its heading.
    indent: int = 0
    #: A heading row (``edits[0]``) with no value of its own.
    header: bool = False


@dataclass(frozen=True)
class DetailSection:
    title: str
    rows: tuple[DetailRow, ...] = field(default_factory=tuple)
    #: ``diff`` sections colour their block lines by hunk marker.
    kind: str = "rows"


def _clean(value: object, limit: int = VALUE_LIMIT) -> str:
    text = escape_controls(str(value))
    if len(text) > limit:
        return f"{text[:limit]}\n[clipped {len(text) - limit} more characters]"
    return text


def _scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _row(label: str, value: object) -> DetailRow:
    text = _clean(_scalar(value))
    return DetailRow(label, text, block="\n" in text or len(text) > 120)


def flatten(value: Any, prefix: str = "", depth: int = 0) -> list[DetailRow]:
    """Every leaf of ``value`` as a row, keyed by its dotted/indexed path."""
    if isinstance(value, Mapping):
        if not value:
            return [DetailRow(prefix or "value", "{}")]
        if depth >= _MAX_DEPTH:
            return [_row(prefix, json.dumps(value, default=str, ensure_ascii=False))]
        rows: list[DetailRow] = []
        for key, item in value.items():
            rows += flatten(item, f"{prefix}.{key}" if prefix else str(key), depth + 1)
        return rows
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            return [DetailRow(prefix or "value", "[]")]
        if depth >= _MAX_DEPTH:
            return [_row(prefix, json.dumps(value, default=str, ensure_ascii=False))]
        scalars = all(not isinstance(i, (Mapping, list, tuple)) for i in value)
        if scalars and len(value) <= 16 and sum(len(_scalar(i)) + 2 for i in value) <= 200:
            return [_row(prefix or "value", ", ".join(_scalar(i) for i in value))]
        rows = []
        grouped = all(isinstance(item, Mapping) and item for item in value)
        for index, item in enumerate(value):
            name = f"{prefix}[{index}]" if prefix else f"#{index + 1}"
            if grouped:
                # Each object becomes a headed group: `todos[0]` then its fields.
                rows.append(DetailRow(name, "", header=True))
                rows += [replace(r, indent=r.indent + 1) for r in flatten(item, "", depth + 1)]
            else:
                rows += flatten(item, name, depth + 1)
        return rows
    return [_row(prefix or "value", value)]


def _stamp(value: float | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return str(value)


def _bounded(rows: list[DetailRow]) -> tuple[DetailRow, ...]:
    if len(rows) <= SECTION_ROWS:
        return tuple(rows)
    extra = len(rows) - SECTION_ROWS
    return (*rows[:SECTION_ROWS], DetailRow("…", f"{extra} more rows clipped"))


def _result_rows(result: Sequence[Any]) -> list[DetailRow]:
    rows: list[DetailRow] = []
    for index, block in enumerate(result, 1):
        kind = str(block.get("type", "block")) if isinstance(block, Mapping) else "block"
        label = f"#{index} {kind}" if len(result) > 1 else kind
        if isinstance(block, Mapping) and isinstance(block.get("text"), str):
            rows.append(DetailRow(label, _clean(block["text"]), block=True))
            for key, item in block.items():
                if key not in {"type", "text"}:
                    rows += flatten(item, f"{label}.{key}")
        elif isinstance(block, Mapping):
            rows += flatten({k: v for k, v in block.items() if k != "type"}, label) or [DetailRow(label, "")]
        else:
            rows += flatten(block, label)
    return rows


def _mcp_search_sections(tool: ToolCallView) -> list[DetailSection]:
    """Decode our durable search text into labelled query and schema rows."""
    import re

    text = "\n".join(block.get("text", "") for block in tool.result if isinstance(block, Mapping))
    sections = []
    parts = re.split(r"(?m)^(Query [0-9]+ · [^\n]*)\n?", text)
    for index in range(1, len(parts), 2):
        rows = []
        for line in parts[index+1].splitlines():
            stripped = line.strip()
            if stripped.startswith("Input schema: "):
                payload = stripped.removeprefix("Input schema: ")
                try:
                    schema = json.loads(payload)
                except ValueError:
                    # A clipped schema remains inspectable and the missing tail
                    # is explicitly announced by the producer.
                    rows.append(_row("Input schema (clipped)", payload))
                else:
                    rows.extend(flatten(schema, "Input schema"))
            elif re.match(r"[0-9]+\. ", stripped):
                rows.append(_row("Tool", stripped.split(". ", 1)[1]))
            elif stripped.startswith("Description: "):
                rows.append(_row("Description", stripped.removeprefix("Description: ")))
            elif stripped and stripped not in {"</untrusted-mcp-data>", "```"}:
                rows.append(_row("Details", stripped))
        sections.append(DetailSection(_clean(parts[index], 500), _bounded(rows)))
    return sections


def tool_detail_sections(tool: ToolCallView) -> list[DetailSection]:
    """Project one reducer tool call into sections; empty ones are omitted."""
    overview = [DetailRow("Status", tool_status(tool))]
    for label, value in (
        ("Tool", tool.name or None),
        ("Target", tool.target),
        ("Called through", "McpCall" if tool.name == "McpCall" else None),
        ("Bundle", tool.bundle),
        ("Duration", None if tool.duration_ms is None else f"{tool.duration_ms} ms"),
        ("Requested", _stamp(tool.requested_ts)),
        ("Started", _stamp(tool.started_ts)),
        ("Finished", _stamp(tool.finished_ts)),
        ("Call ID", tool.call_id or None),
        ("Model iteration", tool.iteration or None),
        ("Executed", _scalar(tool.executed) if tool.status != "requested" else None),
        ("Error result", "yes" if tool.is_error else None),
    ):
        if value is not None:
            overview.append(_row(label, value))
    sections = [DetailSection("Overview", tuple(overview))]
    if tool.input:
        sections.append(DetailSection("Parameters", _bounded(flatten(tool.input))))
    if tool.code:
        sections.append(DetailSection("Code", (DetailRow("code", _clean(tool.code), True),)))
    if tool.progress:
        rows = [DetailRow(f"#{i}", _clean(item, 2_000)) for i, item in enumerate(tool.progress, 1)]
        sections.append(DetailSection("Progress", _bounded(rows)))
    if tool.display:
        sections.append(DetailSection("Summary", (DetailRow("display", _clean(tool.display), True),)))
    if tool.result:
        sections.extend((_mcp_search_sections(tool) if tool.name == "McpSearch" else []) or [DetailSection("Result", _bounded(_result_rows(tool.result)))])
    if tool.error:
        sections.append(DetailSection("Error", (DetailRow("error", _clean(tool.error), True),)))
    if tool.context_note:
        sections.append(DetailSection("Context", (DetailRow("note", _clean(tool.context_note), True),)))
    todos = (tool.metrics or {}).get("todos") if isinstance(tool.metrics, Mapping) else None
    if isinstance(todos, Sequence) and todos and all(isinstance(t, Mapping) for t in todos):
        glyph = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]", "cancelled": "[-]"}
        rows = [
            DetailRow(
                f"{glyph.get(str(t.get('status')), '[?]')} {t.get('id', index)}",
                " ".join(filter(None, [_clean(t.get("content", ""), 2_000), f"({t['priority']})" if t.get("priority") else ""])),
            )
            for index, t in enumerate(todos, 1)
        ]
        sections.append(DetailSection("Todo list", _bounded(rows)))
    if tool.metrics:
        sections.append(DetailSection("Metrics", _bounded(flatten(tool.metrics))))
    if isinstance(tool.diff, Mapping):
        diff = tool.diff
        rows = [
            _row("Path", diff.get("path") or "edit"),
            _row("Added lines", diff.get("added_lines", 0)),
            _row("Removed lines", diff.get("removed_lines", 0)),
        ]
        if diff.get("truncated"):
            rows.append(_row("Preview", "clipped"))
        if isinstance(diff.get("hunk"), str) and diff["hunk"]:
            rows.append(DetailRow("Hunk", _clean(diff["hunk"]), True))
        sections.append(DetailSection("Diff", tuple(rows), kind="diff"))
    return sections


def sections_to_text(sections: Sequence[DetailSection]) -> str:
    """Plain-text form of ``sections`` (what tests and logs read)."""
    parts: list[str] = []
    for section in sections:
        lines = [section.title.upper()]
        for row in section.rows:
            pad = "  " * (row.indent + 1)
            if row.header:
                lines.append(f"{pad}{row.label}")
            elif row.block:
                lines.append(f"{pad}{row.label}:")
                lines += [f"{pad}  {line}" for line in row.value.split("\n")]
            else:
                lines.append(f"{pad}{row.label}: {row.value}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def styled_lines(sections: Sequence[DetailSection]) -> tuple[list[str], list[str]]:
    """``sections`` as text lines plus one tone per line, for shells that colour them.

    Tones follow the terminal modal: ``title`` (bold section name), ``header`` (bold),
    ``label`` (dim block label), ``kv`` (dim ``label: `` then the value), ``add``,
    ``del`` and ``hunk`` (diff blocks), and ``""`` for plain text. Joined with
    newlines the lines equal :func:`sections_to_text`.
    """
    lines: list[str] = []
    tones: list[str] = []

    def put(line: str, tone: str = "") -> None:
        lines.append(line)
        tones.append(tone)

    for index, section in enumerate(sections):
        if index:
            put("")
        put(section.title.upper(), "title")
        for row in section.rows:
            pad = "  " * (row.indent + 1)
            if row.header:
                put(f"{pad}{row.label}", "header")
            elif row.block:
                put(f"{pad}{row.label}:", "label")
                for line in row.value.split("\n"):
                    tone = ""
                    if section.kind == "diff":
                        tone = ("add" if line.startswith("+") and not line.startswith("+++") else
                                "del" if line.startswith("-") and not line.startswith("---") else
                                "hunk" if line.startswith("@@") else "")
                    put(f"{pad}  {line}", tone)
            else:
                put(f"{pad}{row.label}: {row.value}", "kv")
    return lines, tones


__all__ = [
    "styled_lines",
    "DetailRow", "DetailSection", "flatten", "sections_to_text", "tool_detail_sections",
]
