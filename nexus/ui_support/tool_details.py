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
                # Each object becomes a headed group: `items[0]` then its fields.
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


# --- Code mode (plan 03 section 3.13) -------------------------------------------------------

_GLYPHS = {"ok": "✓", "error": "✗", "denied": "⊘", "running": "…", "cancelled": "–"}
_ARG_VALUE = 40  # characters of one argument in a compact nested row
_ARGS_TOTAL = 110  # characters of all arguments in a compact nested row
_EXCERPT = 600  # characters of an llm prompt or answer excerpt
_MAX_PROGRAM_LINES = 400


def _size(count: object) -> str:
    if not isinstance(count, (int, float)) or isinstance(count, bool):
        return ""
    return f"{count} B" if count < 1024 else f"{count / 1024:.1f} KB"


def compact_args(args: object) -> str:
    """``key=value`` pairs on one line; every value clipped, the cut announced by ``…``."""
    if not isinstance(args, Mapping):
        return "" if args is None else _clean(args, _ARGS_TOTAL).replace("\n", " ")
    parts = []
    for key, value in args.items():
        if key == "args" and isinstance(value, Sequence) and not isinstance(value, str):
            parts += [_compact_value(item) for item in value]
        else:
            parts.append(f"{key}={_compact_value(value)}")
    line = ", ".join(parts)
    return line if len(line) <= _ARGS_TOTAL else f"{line[:_ARGS_TOTAL]}…"


def _compact_value(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    text = escape_controls(text).replace("\n", "⏎")
    text = text if len(text) <= _ARG_VALUE else f"{text[:_ARG_VALUE]}…"
    return json.dumps(text, ensure_ascii=False) if isinstance(value, str) else text


def _nested_status(call: Mapping[str, Any]) -> str:
    status = str(call.get("status") or "running")
    return {"completed": "ok", "failed": "error"}.get(status, status)


def _nested_summary(call: Mapping[str, Any]) -> tuple[str, str]:
    """(label, value) of a nested call's one-line row: glyph, index, tool; args, time, size."""
    glyph = _GLYPHS.get(_nested_status(call), "?")
    label = f"{glyph} {call.get('index') or '·'}. {call.get('tool') or '?'}"
    bits = [compact_args(call.get("args"))]
    if isinstance(call.get("duration_ms"), (int, float)):
        bits.append(f"{call['duration_ms']} ms")
    if call.get("bytes") is not None:
        bits.append(_size(call["bytes"]) + (" (clipped)" if call.get("clipped") else ""))
    if call.get("kind") == "llm" and call.get("model"):
        bits.append(f"{call['model']} {call.get('input_tokens', '?')}→{call.get('output_tokens', '?')} tok")
    if _nested_status(call) == "denied":
        permission = call.get("permission")
        rule = permission.get("rule") if isinstance(permission, Mapping) else None
        bits.append(f"rule {rule}" if rule else "denied")
    return label, " · ".join(b for b in bits if b)


def _numbered_program(program: str) -> str:
    lines = escape_controls(program).split("\n")
    shown = lines[:_MAX_PROGRAM_LINES]
    width = len(str(len(lines)))
    body = "\n".join(f"{i:>{width}}  {line}" for i, line in enumerate(shown, 1))
    if len(lines) > len(shown):
        body += f"\n[clipped {len(lines) - len(shown)} more lines]"
    return body


def _nested_result_text(call: Mapping[str, Any]) -> str:
    result = call.get("result")
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping) and isinstance(result.get("text"), str):
        return result["text"]
    return "" if result is None else json.dumps(result, default=str, ensure_ascii=False)


def _nested_detail_rows(call: Mapping[str, Any]) -> list[DetailRow]:
    """Everything one nested call knew: permission, full arguments, full result."""
    status = _nested_status(call)
    rows = [_row("Tool", call.get("tool") or "?"), _row("Kind", call.get("kind") or "tool"),
            DetailRow("Status", status)]
    permission = call.get("permission")
    if isinstance(permission, Mapping):
        rows.append(_row("Permission", permission.get("decision") or "none"))
        if permission.get("rule"):
            rows.append(_row("Matched rule", permission["rule"]))
    if call.get("duration_ms") is not None:
        rows.append(_row("Duration (ms)", call["duration_ms"]))
    if call.get("bytes") is not None:
        rows.append(_row("Bytes", call["bytes"]))
    if call.get("kind") == "llm":
        rows += [_row("Model", call.get("model") or "unknown"),
                 _row("Input tokens", call.get("input_tokens", "unknown")),
                 _row("Output tokens", call.get("output_tokens", "unknown"))]
    elif _nested_status(call) == "denied" and not (isinstance(permission, Mapping) and permission.get("rule")):
        rows.append(_row("Matched rule", "none recorded"))
    rows += flatten(args, "Arguments") if (args := call.get("args")) else []
    args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
    text = _nested_result_text(call)
    # The tool's own renderer (the one a direct call uses) lays out arguments and result.
    stand_in = ToolCallView(
        name=str(call.get("tool") or ""), input=dict(args), status="completed",
        is_error=status in {"error", "denied"},
        result=[{"type": "text", "text": text}] if text else [],
    )
    for section in tool_detail_sections(stand_in):
        if section.title in {"Overview", "Parameters"}:
            continue
        rows.append(DetailRow(section.title, "", header=True))
        rows += [replace(r, indent=r.indent + 1) for r in section.rows]
    if call.get("kind") == "llm":
        for key, label in (("prompt", "Prompt excerpt"), ("text", "Input excerpt")):
            if isinstance(args.get(key), str):
                rows.append(DetailRow(label, _clean(args[key], _EXCERPT), True))
    if call.get("clipped"):
        rows.append(_row("Result clipped", f"shown {len(text)} characters of {call.get('bytes', 'unknown')} bytes"))
    if call.get("error"):
        rows.append(DetailRow("Error", _clean(call["error"]), True))
    return rows


def _diagnostic_rows(diagnostic: object) -> list[DetailRow]:
    if isinstance(diagnostic, Mapping):
        rows = [_row("Kind", diagnostic.get("kind") or "unknown"),
                DetailRow("Message", _clean(diagnostic.get("message", "")), True)]
        if diagnostic.get("line") is not None:
            rows.append(_row("Line", diagnostic["line"]))
        return rows
    return [DetailRow("Diagnostic", _clean(diagnostic), True)] if diagnostic else []


def _nested_row(call: Mapping[str, Any]) -> DetailRow:
    label, value = _nested_summary(call)
    return DetailRow(label, value)


def _code_sections(tool: ToolCallView) -> list[DetailSection]:
    """Labelled sections of one ``code`` call: program, every nested call, result, store writes."""
    meta = tool.code_mode or {}
    args = tool.input if isinstance(tool.input, Mapping) else {}
    purpose = meta.get("purpose") or args.get("purpose") or ""
    rows = [DetailRow("Status", tool_status(tool))]
    status_rows = []
    if meta.get("status"):
        status_rows.append(_row("Status", meta["status"]))
    if meta.get("limit"):
        status_rows.append(_row("Limit hit", meta["limit"]))
    timeout = meta.get("timeout_s", args.get("timeout_s"))
    if timeout is not None:
        rows.append(_row("timeout_s", timeout))
    for key, label in (("exposure", "Exposure"), ("monty_version", "Monty"), ("program_digest", "Program digest"),
                       ("wall_ms", "Wall time (ms)"), ("calls", "Nested calls"), ("ok", "OK"), ("errors", "Errors"),
                       ("denied", "Denied"), ("sub_calls", "Model sub-calls"), ("sub_tokens", "Sub-call tokens")):
        if meta.get(key) is not None:
            (rows if key in {"exposure", "monty_version", "program_digest"} else status_rows).append(_row(label, meta[key]))
    if meta.get("callable"):
        rows.append(_row("Callable", ", ".join(map(str, meta["callable"]))))
    if tool.call_id:
        rows.append(_row("Call ID", tool.call_id))
    sections = [DetailSection(_clean(f"code · {purpose}" if purpose else "code", 300), tuple(rows))]
    if status_rows:
        sections.append(DetailSection("Program status", tuple(status_rows)))
    targets = meta.get("targets") or []
    if targets:
        sections.append(DetailSection("Targets", tuple(
            _row(str(t.get("name") or t.get("ref") or "?"), f"logical id {t.get('logical_id') or 'unknown'}")
            if isinstance(t, Mapping) else _row("target", t) for t in targets)))
    program = args.get("program") or args.get("code") or tool.code
    if program:
        sections.append(DetailSection("Code", (DetailRow("code", _numbered_program(str(program)), True),)))
    if tool.nested_calls:
        calls = list(tool.nested_calls)
        sections.append(DetailSection(f"Call list ({len(calls)})", _bounded([_nested_row(c) for c in calls])))
        for number, call in enumerate(calls, 1):
            sections.append(DetailSection(f"Nested call {number}", _bounded(_nested_detail_rows(call))))
    result_rows = _result_rows(tool.result) if tool.result else []
    if tool.error:
        result_rows.append(DetailRow("Error", _clean(tool.error), True))
    if result_rows:
        sections.append(DetailSection("Result", _bounded(result_rows)))
    diag = _diagnostic_rows(meta.get("diagnostic"))
    if diag:
        sections.append(DetailSection("Diagnostic", tuple(diag)))
    store = (tool.metrics or {}).get("code.store") if isinstance(tool.metrics, Mapping) else None
    if isinstance(store, Mapping):
        writes = store.get("set") if isinstance(store.get("set"), Mapping) else None
        if writes is None and isinstance(store.get("entries"), Mapping):
            writes = store["entries"]
        scalar = (str, int, float, bool, type(None))
        srows = [_row(str(k), v if isinstance(v, scalar) else json.dumps(v, default=str, ensure_ascii=False))
                 for k, v in (writes or {}).items()]
        srows += [_row(str(k), "deleted") for k in store.get("delete") or []]
        if srows:
            sections.append(DetailSection("Store writes", _bounded(srows)))
        if isinstance(store.get("entries"), Mapping) and store["entries"]:
            sections.append(DetailSection("Stored entries", _bounded(flatten(store["entries"]))))
    if tool.progress:
        sections.append(DetailSection("Progress", _bounded(
            [DetailRow(f"#{i}", _clean(x, 2_000)) for i, x in enumerate(tool.progress, 1)])))
    if tool.context_note:
        sections.append(DetailSection("Context", (DetailRow("note", _clean(tool.context_note), True),)))
    return sections


def code_signal_rows(tool: ToolCallView) -> list[tuple[str, str]]:
    """Compact ``(kind, text)`` rows for the expanded timeline body of a ``code`` call.

    Numbered program, one glyph row per nested call (live as ``running`` rows arrive), then
    the outcome. The full labelled sections stay behind the details view.
    """
    meta = tool.code_mode or {}
    args = tool.input if isinstance(tool.input, Mapping) else {}
    rows: list[tuple[str, str]] = []
    program = args.get("program") or args.get("code") or tool.code
    if program:
        lines = _numbered_program(str(program)).split("\n")
        rows += [("out", line) for line in lines[:60]]
        if len(lines) > 60:
            rows.append(("clip", f"… {len(lines) - 60} more program lines; open details for all"))
    for call in tool.nested_calls:
        label, value = _nested_summary(call)
        failed = _nested_status(call) in {"error", "denied"}
        rows.append(("error" if failed else "text", f"{label}  {value}".rstrip()))
    diag = meta.get("diagnostic")
    if isinstance(diag, Mapping):
        where = f" (line {diag['line']})" if diag.get("line") is not None else ""
        rows.append(("error", f"{diag.get('kind', 'error')}{where}: {_clean(diag.get('message', ''), 400)}"))
    if meta.get("limit"):
        rows.append(("error", f"limit hit: {meta['limit']}"))
    text = "\n".join(str(b.get("text", "")) for b in tool.result if isinstance(b, Mapping))
    if text:
        lines = _clean(text, 4_000).split("\n")
        rows += [("out", line) for line in lines[:12]]
        if len(lines) > 12:
            rows.append(("clip", f"… {len(lines) - 12} more result lines; open details for all"))
    store = (tool.metrics or {}).get("code.store") if isinstance(tool.metrics, Mapping) else None
    if isinstance(store, Mapping) and isinstance(store.get("set"), Mapping) and store["set"]:
        rows.append(("kv", "store: " + ", ".join(map(str, store["set"]))))
    return rows


def tool_detail_sections(tool: ToolCallView) -> list[DetailSection]:
    """Project one reducer tool call into sections; empty ones are omitted."""
    if tool.name == "code" and (tool.code_mode or tool.nested_calls or "program" in (tool.input or {})):
        return _code_sections(tool)
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
        ("Provenance", tool.provenance),
        ("Executed", _scalar(tool.executed) if tool.status != "requested" and not tool.provenance else None),
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


def _capitalized(text: str) -> str:
    """Upper-case only the first character; the rest of a section title stays as given."""
    return text[:1].upper() + text[1:]


def sections_to_text(sections: Sequence[DetailSection]) -> str:
    """Plain-text form of ``sections`` (what tests and logs read)."""
    parts: list[str] = []
    for section in sections:
        lines = [_capitalized(section.title)]
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
        put(_capitalized(section.title), "title")
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
