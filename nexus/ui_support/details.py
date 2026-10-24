"""Pure details-sidebar data for the native shell.

Session facts, files modified by tools and MCP server health, reduced from the
canonical view and the redacted Doctor report. No UI toolkit is imported here,
so every native client projects the same session facts.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .context import context_usage, thinking_status
from .text import sanitize
from .timeline import split_diff_files

_EDIT_TOOLS = frozenset({"edit", "multiedit", "write", "apply_patch", "patch"})

#: The terminal's right sidebar tabs as stored. ``Files`` is the Changes tab (the stored id predates
#: the label); ``Changes`` is accepted as an alias and normalised to it.
TABS = ("Session", "Files", "MCP", "Jobs", "Logs")
DEFAULT_TAB = "Session"
#: Mirror ``rust/tui/src/details_sidebar.rs`` (pinned by ``tests/test_ratatui_details_sidebar.py``).
MIN_WIDTH = 24
MAX_WIDTH = 60
DEFAULT_WIDTH = 40


def normalize_tab(value: object) -> str:
    """A stored or requested tab as one of ``TABS`` (``Changes`` is ``Files``; anything else is Session)."""
    if value == "Changes":
        return "Files"
    return value if isinstance(value, str) and value in TABS else DEFAULT_TAB


def clamp_width(value: object) -> int:
    """A stored width bounded to ``MIN_WIDTH``..``MAX_WIDTH``; anything unusable is the default."""
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_WIDTH
    return max(MIN_WIDTH, min(MAX_WIDTH, value))


@dataclass(frozen=True)
class FileChange:
    path: str
    added: int = 0
    removed: int = 0
    created: bool = False
    hunks: tuple[str, ...] = ()


def diff_preview_lines(hunks: Iterable[str], limit: int = 60) -> list[str]:
    """A file's diff lines without ``---``/``+++`` headers, clipped with a notice.

    Plain text (the first character carries add/remove/hunk meaning), so each
    shell colours it its own way.
    """
    lines = [sanitize(line, 200) for hunk in hunks for line in hunk.splitlines()
             if not line.startswith(("---", "+++"))]
    if len(lines) > limit:
        lines = [*lines[:limit], f"… {len(lines) - limit} more lines"]
    return lines


def modified_files(view: Any) -> list[FileChange]:
    """Aggregate successful file edits across the session and its subagents."""
    files: dict[str, FileChange] = {}

    def visit(turns: Iterable[Any]) -> None:
        for turn in turns or ():
            for tool in getattr(turn, "tools", ()) or ():
                if str(tool.name).casefold() not in _EDIT_TOOLS or tool.status != "completed":
                    continue
                if tool.is_error or tool.error:
                    continue
                diff = tool.diff if isinstance(tool.diff, Mapping) else {}
                inputs = tool.input if isinstance(tool.input, Mapping) else {}
                metrics = tool.metrics if isinstance(tool.metrics, Mapping) else {}
                parts = split_diff_files(diff)
                if len(parts) > 1:
                    # A multi-file patch: attribute each file's own lines.
                    for path, hunk in parts:
                        prior = files.get(path, FileChange(path))
                        added = sum(1 for line in hunk.splitlines() if line.startswith("+"))
                        removed = sum(1 for line in hunk.splitlines() if line.startswith("-"))
                        files[path] = FileChange(
                            path, prior.added + added, prior.removed + removed,
                            prior.created, prior.hunks + (hunk,),
                        )
                    continue
                path = diff.get("path") or inputs.get("path") or inputs.get("file_path")
                if not isinstance(path, str) or not path:
                    continue
                prior = files.get(path, FileChange(path))
                hunk = diff.get("hunk")
                files[path] = FileChange(
                    path,
                    prior.added + _int(diff.get("added_lines")),
                    prior.removed + _int(diff.get("removed_lines")),
                    prior.created or bool(metrics.get("created")),
                    prior.hunks + ((hunk,) if isinstance(hunk, str) and hunk else ()),
                )

    visit(getattr(view, "turns", ()))
    for agent in (getattr(view, "agents", {}) or {}).values():
        visit(getattr(agent.body, "turns", ()))
    return list(files.values())


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def session_rows(view: Any, *, phase: str, agent: str, model: str, effort: str | None) -> list[tuple[str, str]]:
    """Label/value rows of the SESSION block."""
    tools = [tool for turn in view.turns for tool in turn.tools]
    usage = view.usage
    rows = [
        ("Status", phase.replace("_", " ")),
        ("Agent", agent),
        ("Model", model or "default"),
        ("Effort", effort or "default"),
        ("Turns", str(len(view.turns))),
        ("Tool calls", str(len(tools))),
    ]
    if usage.input_tokens or usage.output_tokens:
        rows.append(("Tokens", f"{usage.input_tokens:,} in · {usage.output_tokens:,} out"))
    elif getattr(view, "external", None):
        rows.append(("Tokens", "unknown (not reported by Claude Code)"))
    thinking = thinking_status(view)
    if thinking:
        rows.append(("Activity", thinking))
    ctx = context_usage(view)
    if ctx != "Preview":
        rows.append(("Context", ctx))
    return rows


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} KB" if n >= 1024 else f"{n} B"


def kernel_rows(view: Any) -> list[tuple[str, str]]:
    """Label/value rows of the active agent's code-mode kernel, folded from the view (kernel plan §3.11).

    Nothing here asks the registry: the rows are what the session log shows. Idle eviction
    leaves no event, so an evicted kernel reads as active until the next cell restores it.
    Empty when the agent never started a kernel.
    """
    kernel: dict[str, Any] | None = None
    for turn in view.turns:
        for tool in turn.tools:
            meta = tool.code_mode or {}
            if tool.name not in {"code_start", "code_attach", "code_run", "code_reset"} or not isinstance(meta, Mapping):
                continue
            if tool.name == "code_start" or (kernel and meta.get("kernel_id") not in (None, kernel["id"])):
                kernel = {"id": meta.get("kernel_id"), "servers": [], "tools": [], "n": 0, "ckpt": None,
                          "state": "active"}
            if kernel is None:
                kernel = {"id": meta.get("kernel_id"), "servers": [], "tools": [], "n": 0, "ckpt": None,
                          "state": "active"}
            attached = meta.get("attached")
            if isinstance(attached, Mapping):
                kernel["servers"] = list(dict.fromkeys([*kernel["servers"], *map(str, attached.get("servers") or [])]))
                kernel["tools"] = list(dict.fromkeys([*kernel["tools"], *map(str, attached.get("tools") or [])]))
            if tool.name == "code_reset":
                kernel.update(n=0, ckpt=None, state="stopped" if meta.get("stopped") else "active (reset)")
            elif tool.name == "code_run":
                kernel["n"] = max(kernel["n"], _int(meta.get("n")))
                kernel["state"] = f"active as of In [{meta.get('n')}]" if meta.get("n") is not None else "active"
                if isinstance(meta.get("restored"), Mapping):
                    kernel["state"] += f" (restored: {meta['restored'].get('reason')})"
                if isinstance(meta.get("lost"), Mapping):
                    kernel["state"] = f"lost: {meta['lost'].get('reason')}"
                    kernel["ckpt"] = None
            if isinstance(meta.get("checkpoint"), Mapping) and meta["checkpoint"].get("status", "written") == "written":
                kernel["ckpt"] = meta["checkpoint"]
    if kernel is None:
        return []
    rows = [("Kernel", sanitize(str(kernel["id"] or "unknown"), 40))]
    loose = [t for t in kernel["tools"] if not any(t.startswith(f"mcp__{s}__") for s in kernel["servers"])]
    rows.append(("Attached", sanitize(", ".join(kernel["servers"]) + (f" (+{len(loose)} tools)" if loose else "")
                                      if kernel["servers"] else f"{len(kernel['tools'])} tools", 120)))
    rows.append(("Cells", str(kernel["n"])))
    ckpt = kernel["ckpt"]
    rows.append(("Snapshot", f"after In [{ckpt.get('after_n')}] · {_size(_int(ckpt.get('bytes')))}" if ckpt else "none"))
    rows.append(("Liveness", sanitize(kernel["state"], 120)))
    return rows


def mcp_rows(report: Mapping[str, Any] | None, *, error: str | None = None) -> list[tuple[str, str, str]]:
    """``(tone, text, note)`` rows of the MCP SERVERS block.

    Tones: ``success|error|warning|quiet`` for a server dot, ``plain-error`` and
    ``plain-quiet`` for message lines. Used by the native MCP panel.
    """
    if error:
        return [("plain-error", "Unavailable: " + sanitize(error, 120), "")]
    if report is None:
        return [("plain-quiet", "Checking servers…", "")]
    mcp = report.get("mcp")
    if not isinstance(mcp, Mapping):
        return [("plain-quiet", "MCP is disabled for this workspace.", "")]
    servers = [row for row in mcp.get("servers") or () if isinstance(row, Mapping)]
    rows: list[tuple[str, str, str]] = []
    for diagnostic in mcp.get("diagnostics") or ():
        if isinstance(diagnostic, Mapping):
            source = diagnostic.get("path") or diagnostic.get("name") or "MCP config"
            message = diagnostic.get("error") or diagnostic.get("error_type") or "Configuration error"
            rows.append(("plain-error", sanitize(f"{source}: {message}", 600), ""))
    if not servers:
        rows.append(("plain-quiet", "No MCP servers discovered (global or project config).", ""))
    for row in servers:
        health = str(row.get("health") or "unknown").casefold()
        tone = {"ready": "success", "failed": "error"}.get(health, "warning")
        if row.get("enabled", True) is False:
            tone, health = "quiet", "disabled"
        counts = " · ".join(
            f"{row[key]} {label}" for key, label in (("tool_count", "tools"), ("resource_count", "res"))
            if isinstance(row.get(key), int) and row[key]
        )
        rows.append((tone, sanitize(str(row.get("name") or "server"), 40), (counts or health) + " · " + str(row.get("tool_loading", "search"))))
        if row.get("last_error") and not row.get("connected"):
            rows.append(("plain-error", "  " + sanitize(str(row["last_error"]), 80), ""))
    return rows
