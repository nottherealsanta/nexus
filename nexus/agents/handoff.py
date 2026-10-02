"""Failure handoff for subagents: what a failed child knew, kept for its parent.

A child that stops without finishing (iteration or wall-clock limit, token
budget, a provider error, or a crash) must not take its context with it. The
child's session stays in the durable store, and this module distills it into a
bounded, deterministic markdown report -- the task, the stop reason, the last
thing the agent said, files it changed, and a timeline of its recent tool calls
with errors flagged. It needs no model call, so it still works when the child
failed *because* of the clock or a dead provider.

Two outputs, one report:

* an inline digest returned to the parent in the ``Task`` result, and
* the full report written to ``<home>/handoffs/<session>.md`` (best effort; a
  write failure never hides the inline digest) so the root can read it when
  it decides to start a fresh child; there is no resume field.

The module sits beside the runner (L3) and imports only ``util``.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..util import redact_secrets

#: Cap on one rendered field (task prompt, last message, a tool argument).
FIELD_CHARS = 1_500
#: Tool calls kept in the timeline, newest last.
TIMELINE_CALLS = 30
#: Cap on the digest inlined in the Task result; the file keeps the full report.
DIGEST_CHARS = 4_000
#: Cap on the stored report and on what ``load_handoff`` reads back.
REPORT_CHARS = 24_000

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


def handoff_path(home_root: Path, session_id: str) -> Path:
    """Where the report for ``session_id`` lives (``home_root`` = ``~/.nexus``)."""
    name = _SAFE_ID.sub("_", str(session_id)).strip("._") or "subagent"
    return Path(home_root) / "handoffs" / f"{name[:120]}.md"


def _clip(value: object, limit: int = FIELD_CHARS) -> str:
    text = redact_secrets(str(value)).strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [clipped, {len(text) - limit} more characters]"


def _result_text(block: object) -> str:
    content = getattr(block, "content", None)
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content or ():
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _args_line(args: object) -> str:
    if not isinstance(args, Mapping):
        return ""
    shown = ", ".join(f"{key}={_clip(value, 80)!r}" for key, value in list(args.items())[:4])
    return shown


def build_handoff(
    *,
    agent: str,
    session_id: str,
    prompt: str,
    status: str,
    stop_reason: str | None,
    error: str | None,
    iterations: int,
    messages: Iterable[object],
    files_changed: Iterable[str] = (),
    total_tokens: int = 0,
    detail: str = "",
) -> str:
    """The markdown report for one failed child, bounded and secret-redacted."""
    last_text = ""
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for message in messages:
        role = getattr(message, "role", None)
        for block in getattr(message, "content", None) or ():
            name = getattr(block, "name", None)
            call_id = getattr(block, "id", None)
            if isinstance(name, str) and isinstance(call_id, str):
                entry = {"name": name, "args": _args_line(getattr(block, "input", None)),
                         "error": False, "result": ""}
                calls.append(entry)
                by_id[call_id] = entry
                continue
            result_id = getattr(block, "tool_use_id", None)
            if isinstance(result_id, str) and result_id in by_id:
                by_id[result_id]["error"] = bool(getattr(block, "is_error", False))
                by_id[result_id]["result"] = _result_text(block)
                continue
            text = getattr(block, "text", None)
            if role == "assistant" and isinstance(text, str) and text.strip():
                last_text = text

    reason = stop_reason or "unknown"
    lines = [
        f"# Subagent handoff: {agent}",
        "",
        f"- session: `{session_id}`",
        f"- status: {status}",
        f"- stop reason: `{reason}`" + (f" ({_clip(detail, 200)})" if detail else ""),
        f"- iterations: {iterations}; tool calls: {len(calls)}; tokens: {total_tokens}",
    ]
    if error:
        lines.append(f"- error: {_clip(error, 600)}")
    lines += ["", "## Task given to the subagent", "", _clip(prompt)]
    lines += ["", "## Last thing the subagent said", "", _clip(last_text) or "(nothing)"]
    changed = list(files_changed)
    lines += ["", "## Files changed (file-editing tools only)", ""]
    lines += [f"- {_clip(path, 300)}" for path in changed[:50]] or ["(none)"]
    if len(changed) > 50:
        lines.append(f"- … and {len(changed) - 50} more")
    shown = calls[-TIMELINE_CALLS:]
    lines += ["", f"## Last {len(shown)} of {len(calls)} tool calls", ""]
    for entry in shown:
        mark = "ERROR" if entry["error"] else "ok"
        line = f"- [{mark}] {entry['name']}({entry['args']})"
        if entry["error"]:
            line += f" -> {_clip(entry['result'], 240)}"
        lines.append(line)
    if not shown:
        lines.append("(no tool calls)")
    return _clip("\n".join(lines), REPORT_CHARS)


def write_handoff(home_root: Path, session_id: str, report: str) -> Path | None:
    """Persist ``report``; ``None`` when the disk write fails (never raises)."""
    path = handoff_path(home_root, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
    except OSError:
        return None
    return path


def load_handoff(home_root: Path, session_id: str) -> str | None:
    """The stored report for ``session_id``, or ``None`` when absent."""
    with contextlib.suppress(OSError):
        return handoff_path(home_root, session_id).read_text(encoding="utf-8")[:REPORT_CHARS]
    return None


def digest(report: str) -> str:
    """The inline form returned to the parent."""
    return _clip(report, DIGEST_CHARS)

