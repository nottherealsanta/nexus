"""The relevant-only view of a tool call: what it did and what it produced.

``tool_details`` shows everything the agent saw, labelled. This module is its
quiet sibling for the expanded transcript row (plans/done/GROK_BUILD_TUI_PLAN.md
§5.2): typed body rows with no Call ID, timestamps, status words, bundle,
iteration, executed flag or parameter dump. Nothing is lost: the full labelled
sections stay behind the ``inspect_operation`` the projection offers (``i``) and
in ``/verbose``. Pure: no clock, no I/O, deterministic for replay.

Row kinds: ``text`` plain, ``code`` a command, ``out`` raw program output,
``add`` an added line, ``path`` a ``path:line: snippet`` hit, ``kv`` a
``key: value`` parameter, ``md`` Markdown prose, ``error`` a failure line and
``clip`` an announcement that rows were omitted.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from ..view import ToolCallView
from .text import escape_controls
from .timeline import tool_status
from .tool_details import SECTION_ROWS, VALUE_LIMIT, code_signal_rows

FIRST_LINES = 5  # Grok's FIRST_LINES / LAST_LINES for a long excerpt
LAST_LINES = 3
LONG_EXCERPT = FIRST_LINES + LAST_LINES + 1
_VALUE_ROWS = 6  # lines of one parameter value shown in a ``key: value`` row
MAX_SIGNAL_CHARS = 100_000  # characters across all rows of one signal; the rest is announced
_MAX_BLOCKS = 200  # result blocks read for the signal
_MAX_PARAMS = 50  # parameters shown for an unknown tool


@dataclass(frozen=True)
class SignalRow:
    kind: str
    text: str


@dataclass(frozen=True)
class SignalView:
    #: One-line description, e.g. ``Read a.py (lines 1–120)``.
    summary: str
    rows: tuple[SignalRow, ...]
    #: Rows omitted by a bound; each omission is also announced by a ``clip`` row.
    clipped: int = 0

    def wire(self) -> list[list[str]]:
        """``[[kind, text], ...]`` for the bridge."""
        return [[row.kind, row.text] for row in self.rows]

    def text(self) -> str:
        return "\n".join(row.text for row in self.rows)


def _clean(value: object, limit: int = VALUE_LIMIT, cuts: list[int] | None = None) -> str:
    """Escaped ``value`` cut to ``limit`` characters.

    With ``cuts`` the omitted character count is appended there for the caller to announce
    as a ``clip`` row; without it the count is appended inline.
    """
    raw = value if isinstance(value, str) else str(value)
    if len(raw) <= limit:
        return escape_controls(raw)
    # Slice before escaping so a huge value is never fully materialized.
    shown = escape_controls(raw[:limit])
    if cuts is not None:
        cuts.append(len(raw) - limit)
        return shown
    return shown + f"\n… {len(raw) - limit} more characters"


def _result_text(tool: ToolCallView, cuts: list[int] | None = None) -> str:
    parts = []
    size = 0
    omitted = 0
    for index, block in enumerate(tool.result or ()):
        value = block.get("text") if isinstance(block, Mapping) else block
        if not isinstance(value, str) or not value:
            continue
        room = VALUE_LIMIT - size
        if index >= _MAX_BLOCKS or room <= 0:
            omitted += len(value)  # counted from the source length, never copied
            continue
        parts.append(value[:room])
        omitted += len(value) - len(parts[-1])
        size += len(parts[-1]) + 1
    if not parts:
        return _clean(tool.display or "", cuts=cuts)
    if omitted and cuts is not None:
        cuts.append(omitted)
    elif omitted:
        parts.append(f"… {omitted} more characters")
    return escape_controls("\n".join(parts))


def _lines(text: str) -> list[str]:
    return text.rstrip("\n").split("\n") if text.strip("\n") else []


def _bound(rows: list[SignalRow]) -> tuple[tuple[SignalRow, ...], int]:
    """At most SECTION_ROWS rows and MAX_SIGNAL_CHARS characters; any omission is announced by a ``clip`` row."""
    kept: list[SignalRow] = []
    chars = 0
    for row in rows:
        if len(kept) >= SECTION_ROWS:
            break
        room = MAX_SIGNAL_CHARS - chars
        if len(row.text) > room:
            if room > 0:
                kept.append(SignalRow(row.kind, row.text[:room]))
            break
        kept.append(row)
        chars += len(row.text)
    extra = len(rows) - len(kept)
    if not extra:
        return tuple(kept), 0
    return (*kept, SignalRow("clip", f"… {extra} more rows")), extra


def _excerpt(kind: str, lines: list[str], numbers: int | None = None) -> tuple[list[SignalRow], int]:
    """All of a short excerpt; the first and last lines of a long one with the middle announced."""
    shown = lines
    omitted = 0
    if len(lines) > LONG_EXCERPT:
        omitted = len(lines) - FIRST_LINES - LAST_LINES
    width = len(str((numbers or 1) + len(lines)))

    def row(index: int) -> SignalRow:
        text = lines[index]
        return SignalRow(kind, f"{(numbers + index):>{width}}  {text}" if numbers else text)

    if not omitted:
        return [row(i) for i in range(len(shown))], 0
    rows = [row(i) for i in range(FIRST_LINES)]
    rows.append(SignalRow("clip", f"… {omitted} lines"))
    rows += [row(i) for i in range(len(lines) - LAST_LINES, len(lines))]
    return rows, omitted


def strip_workspace(command: str, workspace: str | None) -> str:
    """Drop a leading ``cd <workspace> && `` only when it names exactly the session workspace."""
    if not workspace:
        return command
    root = workspace.rstrip("/") or "/"
    for path in (root, root + "/"):
        for quoted in (path, f"'{path}'", f'"{path}"'):
            prefix = f"cd {quoted} && "
            if command.startswith(prefix):
                return command[len(prefix):]
    return command


_JOB_HEADER = re.compile(r"(?:job_id|status|exit_code|signal|stdout_offset|stderr_offset): [^\n]*")
SHELL_TOOLS = frozenset({"shell", "bash", "run"})
COMMAND_CELLS = 40  # display cells of a command on a closed row


def strip_job_header(text: str) -> str:
    """Drop the ``job_id:``/``status:``/``exit_code:`` header a shell result opens with.

    Only a result that starts exactly ``job_id: …`` then ``status: …`` is touched, and only
    its leading header lines and a first ``stdout:`` label go; everything else, including
    a ``stderr:`` label and any program output that merely looks like a header, stays.
    """
    lines = text.split("\n")
    if len(lines) < 2 or not lines[0].startswith("job_id: ") or not lines[1].startswith("status: "):
        return text
    index = 0
    while index < len(lines) and _JOB_HEADER.fullmatch(lines[index]):
        index += 1
    rest = lines[index:]
    if rest and rest[0] == "stdout:":
        rest = rest[1:]
    return "\n".join(rest)


def clip_cells(text: str, limit: int = COMMAND_CELLS) -> str:
    """``text`` cut to ``limit`` display cells (wide characters count two), ending in ``…``."""
    import unicodedata

    def cells(char: str) -> int:
        return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1

    if sum(cells(c) for c in text) <= limit:
        return text
    used, kept = 1, []
    for char in text:
        used += cells(char)
        if used > limit:
            break
        kept.append(char)
    return "".join(kept).rstrip() + "…"


def _read_span(tool: ToolCallView, out: str, failed: bool) -> str:
    args = tool.input if isinstance(tool.input, Mapping) else {}
    start = args.get("offset") if isinstance(args.get("offset"), int) and args.get("offset") >= 1 else 1
    count = len(_lines(out))
    return f" (lines {start}–{start + count - 1})" if count and not failed else ""


def closed_summary(tool: ToolCallView) -> str | None:
    """What a closed row says after its verb, or ``None`` to keep the generic summary.

    ``Read``: ``path (lines 1–120)``, never the file's first line. Shells: the command's
    first line clipped to :data:`COMMAND_CELLS`; the duration is the row's own time column.
    The full command and output stay in the signal rows and behind ``inspect_operation``.
    """
    name = tool.name.casefold()
    args = tool.input if isinstance(tool.input, Mapping) else {}
    if name == "read":
        path = _path(tool)
        if not path:
            return None
        if tool_status(tool) != "completed":  # no range until the excerpt exists
            return path
        return path + _read_span(tool, _result_text(tool), False)
    if name in SHELL_TOOLS:
        command = escape_controls(str(args.get("command") or args.get("cmd") or "")).strip()
        if not command:
            return None
        return clip_cells(command.split("\n", 1)[0])
    return None


def _failure(tool: ToolCallView) -> str:
    """``exit 2`` / ``timed out after 120s`` for a failed shell call, else the error text."""
    metrics = tool.metrics if isinstance(tool.metrics, Mapping) else {}
    code = metrics.get("exit_code")
    evidence = f"{tool.error or ''}\n{_result_text(tool)}"
    timed_out = re.search(r"timed out after (\d+(?:\.\d+)?)s", evidence)
    if timed_out:
        return f"timed out after {timed_out.group(1)}s"
    if isinstance(code, int) and code != 0:
        return f"exit {code}"
    exited = re.search(r"exited (-?\d+)", evidence)
    if exited:
        return f"exit {exited.group(1)}"
    return _clean(tool.error, 2_000) if tool.error else "failed"


def _path(tool: ToolCallView) -> str:
    args = tool.input if isinstance(tool.input, Mapping) else {}
    return escape_controls(str(args.get("path") or args.get("file_path") or ""))


def _params(tool: ToolCallView, skip: frozenset[str] = frozenset(), cuts: list[int] | None = None) -> list[SignalRow]:
    """Only the non-empty parameters the call was made with."""
    rows = []
    args = tool.input if isinstance(tool.input, Mapping) else {}
    for index, (key, value) in enumerate(args.items()):
        if index >= _MAX_PARAMS:
            rows.append(SignalRow("clip", f"… {len(args) - _MAX_PARAMS} more parameters"))
            break
        if key in skip or value in (None, "", [], {}):
            continue
        text = _clean(value if isinstance(value, str) else _json(value) if isinstance(value, (dict, list)) else repr(value), 2_000, cuts)
        lines = text.split("\n")
        if len(lines) > _VALUE_ROWS:
            text = "\n".join([*lines[:_VALUE_ROWS], f"… {len(lines) - _VALUE_ROWS} more lines"])
        rows.append(SignalRow("kv", f"{escape_controls(str(key))}: {text}"))
    return rows


def _json(value: object) -> str:
    """JSON of ``value``, stopping once more than the displayed 2,000 characters exist."""
    import json
    parts: list[str] = []
    size = 0
    for chunk in json.JSONEncoder(default=str, ensure_ascii=False).iterencode(value):
        parts.append(chunk)
        size += len(chunk)
        if size > 2_100:
            parts.append("…")
            break
    return "".join(parts)


def _hits(kind: str, text: str) -> list[SignalRow]:
    return [SignalRow(kind, line) for line in _lines(text)]


def tool_signal(tool: ToolCallView, workspace: str | None = None) -> SignalView:
    """The relevant-only rows of ``tool``; ``workspace`` enables ``cd`` stripping for shells."""
    name = tool.name.casefold()
    failed = tool_status(tool) == "failed"
    cuts: list[int] = []  # characters dropped before row building; announced by one clip row
    out = _result_text(tool, cuts)
    rows: list[SignalRow] = []
    omitted = 0
    if name in {"code", "code_start", "code_attach", "code_run", "code_reset"}:
        rows = [SignalRow(kind, text) for kind, text in code_signal_rows(tool)]
        return SignalView("Code program", tuple(rows[:2000]) + (
            (SignalRow("clip", "… additional details clipped"),) if len(rows) > 2000 else ()
        ), max(0, len(rows) - 2000))
    if name == "read":
        path = _path(tool)
        args = tool.input if isinstance(tool.input, Mapping) else {}
        start = args.get("offset") if isinstance(args.get("offset"), int) and args.get("offset") >= 1 else 1
        lines = _lines(out)
        summary = f"Read {path}{_read_span(tool, out, failed)}".rstrip()
        if not failed:
            rows, omitted = _excerpt("out", lines, start)
    elif name == "write":
        path = _path(tool)
        content = (tool.input or {}).get("content") if isinstance(tool.input, Mapping) else None
        lines = _lines(_clean(content, cuts=cuts)) if isinstance(content, str) else []
        summary = f"Wrote {path}" + (f" ({len(lines)} lines)" if lines else "")
        if not failed:
            rows, omitted = _excerpt("add", lines, 1)
    elif name in SHELL_TOOLS:
        out = strip_job_header(out)
        args = tool.input if isinstance(tool.input, Mapping) else {}
        command = strip_workspace(_clean(args.get("command") or args.get("cmd") or "", cuts=cuts), workspace)
        suffix = f" · {tool.duration_ms / 1000:.1f}s" if tool.duration_ms is not None and not failed else ""
        summary = f"$ {command.splitlines()[0] if command else ''}{suffix}".rstrip()
        rows = [SignalRow("code", f"$ {line}" if i == 0 else f"  {line}") for i, line in enumerate(command.split("\n"))] if command else []
        rows += _hits("out", out)
    elif name in {"grep", "glob", "ls", "list"}:
        args = tool.input if isinstance(tool.input, Mapping) else {}
        needle = args.get("pattern") or args.get("query") or ""
        where = args.get("path") or ""
        verb = "Searched" if name in {"grep", "glob"} else "Listed"
        summary = (f'{verb} "{escape_controls(str(needle))}"' if needle else f"{verb} {escape_controls(str(where))}").rstrip()
        if needle and where:
            summary += f" in {escape_controls(str(where))}"
        hits = _hits("path", out)
        if hits and name == "grep":
            summary += f" · {len(hits)} matches"
        rows = hits
    elif name in {"websearch", "webfetch"}:
        args = tool.input if isinstance(tool.input, Mapping) else {}
        subject = args.get("query") or args.get("url") or ""
        summary = (f'Searched web for "{escape_controls(str(subject))}"' if name == "websearch"
                   else f"Fetched {escape_controls(str(subject))}")
        rows = [SignalRow("md" if name == "webfetch" else "text", line) for line in _lines(out)]
    else:
        args = tool.input if isinstance(tool.input, Mapping) else {}
        target = escape_controls(tool.target or str(args.get("tool", "")) or tool.name)
        label = target.removeprefix("mcp__").replace("__", " · ").replace("/", " · ")
        summary = label
        rows = _params(tool, frozenset({"tool"} if name == "mcpcall" else ()), cuts)
        looks_markdown = bool(re.search(r"(?m)^(#{1,6} |\s*[-*] |```)", out))
        rows += [SignalRow("md" if looks_markdown else "text", line) for line in _lines(out)]
    if failed:
        if name in {"shell", "bash", "run"}:  # `$ command`, its output, then the red `exit 2` line
            rows = [*rows, SignalRow("error", _failure(tool))]
        else:  # the error text first, then whatever output exists
            rows = [SignalRow("error", _clean(tool.error, 2_000) if tool.error else "failed"), *rows]
    bounded, extra = _bound(rows)
    if cuts:  # appended after the bound so the announcement itself can never be dropped
        bounded = (*bounded, SignalRow("clip", f"… {sum(cuts)} more characters"))
        extra += 1
    return SignalView(summary=summary, rows=bounded, clipped=omitted + extra)


__all__ = ["SignalRow", "SignalView", "clip_cells", "closed_summary", "strip_job_header", "strip_workspace", "tool_signal"]
