"""Built-in ``Grep``: deterministic, text-only recursive content search.

The regex scan itself runs in a killable worker subprocess (see
:mod:`nexus.tools.builtin._grep_scan`) so a model-supplied pattern that
catastrophically backtracks cannot block the event loop or leak a thread: the
parent enforces a strict deadline, terminates the worker's process group, and
reaps it. Every input is bounded first — pattern length, per-line scan length,
stored line length, total stored match bytes, match count, files scanned, and
file size — so a huge file or a flood of matches cannot exhaust memory.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import signal
import sys
from pathlib import Path
from typing import Any

from ...errors import OperationCancelled, ToolError
from ..permissions import PathSecurityError
from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from ._grep_scan import (
    DEFAULT_MAX_MATCHES as _DEFAULT_MAX_MATCHES,
)
from ._grep_scan import (
    HARD_MAX_MATCHES as _HARD_MAX_MATCHES,
)
from ._grep_scan import (
    MAX_PATTERN_CHARS as _MAX_PATTERN_CHARS,
)
from .read import (
    _byte_budget,
    _cap_lines,
    _check_cancel,
    _error,
    _guard,
    _opt_bool,
    _opt_int,
    _opt_str,
    _require_str,
    _resolve_directory,
    _truncation_marker,
    canonical_root_key,
    glob_pattern_error,
)

#: Longest accepted file glob.
_MAX_GLOB_CHARS = 1024
#: Fallback strict deadline for the worker when config is unavailable; on expiry
#: the worker is SIGKILLed and reaped. The effective value comes from
#: ``tools.grep_timeout_s``.
_DEFAULT_GREP_TIMEOUT_S = 5.0


def _grep_timeout(ctx: ToolContext) -> float:
    """Resolve the validated worker deadline from the frozen config."""
    v2 = getattr(ctx.config, "v2", None)
    tools = getattr(v2, "tools", None)
    value = getattr(tools, "grep_timeout_s", None)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        return _DEFAULT_GREP_TIMEOUT_S
    return float(value)

#: Worker processes currently alive (live tracking for leak assertions/tests).
_LIVE_WORKERS: set[asyncio.subprocess.Process] = set()

_GREP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Text or regular expression to search for.",
            "maxLength": _MAX_PATTERN_CHARS,
        },
        "path": {
            "type": "string",
            "description": "Directory to search under, relative to the workspace.",
        },
        "glob": {
            "type": "string",
            "description": "Only search files whose relative path matches this glob.",
            "maxLength": _MAX_GLOB_CHARS,
        },
        "regex": {
            "type": "boolean",
            "description": "Treat pattern as a regular expression (default true).",
        },
        "case_insensitive": {
            "type": "boolean",
            "description": "Case-insensitive matching (default false).",
        },
        "include_hidden": {
            "type": "boolean",
            "description": "Include dot-prefixed files and directories (default false).",
        },
        "max_matches": {
            "type": "integer",
            "description": f"Maximum matches to return (default {_DEFAULT_MAX_MATCHES}).",
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}

SPEC = ToolSpec(
    name="Grep",
    description=(
        "Search workspace text files for a literal or regular-expression "
        "pattern, returning deterministic path:line matches. Binary files and "
        "symlinks escaping the workspace are skipped."
    ),
    input_schema=_GREP_SCHEMA,
    bundle="fs",
    mutates=False,
    concurrency="parallel",
    permission_key=canonical_root_key,
    max_result_tokens=25_000,
)


class _GrepDeadline(Exception):
    """The regex worker exceeded its strict deadline and was terminated."""


def live_workers() -> tuple[int, ...]:
    """PIDs of live scan workers; used by tests to assert none leak."""
    return tuple(
        sorted(pid for pid in (proc.pid for proc in _LIVE_WORKERS) if pid)
    )


async def _spawn_worker() -> asyncio.subprocess.Process:
    env = dict(os.environ)
    package_parent = str(Path(__file__).resolve().parents[3])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        package_parent if not existing else package_parent + os.pathsep + existing
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "nexus.tools.builtin._grep_scan",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise ToolError(f"Grep: could not start the scan worker: {exc}") from exc
    _LIVE_WORKERS.add(proc)
    return proc


async def _kill_worker(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    pid = proc.pid
    if pid is not None and hasattr(os, "killpg"):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGKILL)
    else:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await proc.wait()


async def _scan_in_worker(
    request: dict[str, Any], ctx: ToolContext, timeout: float
) -> dict[str, Any]:
    proc = await _spawn_worker()
    communicate = None
    cancel_task = None
    try:
        payload = json.dumps(request).encode("utf-8")
        communicate = asyncio.ensure_future(proc.communicate(payload))
        waiters: set[asyncio.Future[Any]] = {communicate}
        token = ctx.cancel_token
        if token is not None:
            cancel_task = asyncio.ensure_future(token.wait())
            waiters.add(cancel_task)
        done, _ = await asyncio.wait(
            waiters,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise _GrepDeadline()
        if communicate not in done:
            # The cancellation token fired first; cancel cooperatively.
            if token is not None:
                token.raise_if_cancelled()
            raise OperationCancelled("cancelled")
        out, err = communicate.result()
        if proc.returncode != 0:
            detail = err.decode("utf-8", "replace")[:200]
            raise ToolError(f"Grep: scan worker failed ({proc.returncode}): {detail}")
        try:
            response = json.loads(out)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Grep: scan worker returned invalid output: {exc}") from exc
        if not isinstance(response, dict):
            raise ToolError("Grep: scan worker returned a non-object response")
        return response
    finally:
        if cancel_task is not None:
            if not cancel_task.done():
                cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cancel_task
        if proc.returncode is None:
            await _kill_worker(proc)
        if communicate is not None:
            if not communicate.done():
                communicate.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await communicate
        _LIVE_WORKERS.discard(proc)


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, dict):
        return _error("Grep arguments must be an object")
    try:
        pattern = _require_str(args, "pattern")
        root_raw = _opt_str(args, "path", ".") or "."
        file_glob = _opt_str(args, "glob", None)
        regex = _opt_bool(args, "regex", True)
        case_insensitive = _opt_bool(args, "case_insensitive", False)
        include_hidden = _opt_bool(args, "include_hidden", False)
        max_matches = _opt_int(args, "max_matches", _DEFAULT_MAX_MATCHES)
    except ToolError as exc:
        return _error(str(exc))
    if "\x00" in pattern:
        return _error("Grep pattern must not contain a NUL byte")
    if len(pattern) > _MAX_PATTERN_CHARS:
        return _error(
            f"Grep pattern is too long ({len(pattern)} characters; limit "
            f"{_MAX_PATTERN_CHARS})"
        )
    if file_glob is not None and "\x00" in file_glob:
        return _error("Grep glob must not contain a NUL byte")
    if file_glob is not None and len(file_glob) > _MAX_GLOB_CHARS:
        return _error(
            f"Grep glob is too long (limit {_MAX_GLOB_CHARS} characters)"
        )
    if file_glob is not None:
        glob_error = glob_pattern_error(file_glob)
        if glob_error is not None:
            return _error(f"Grep glob rejected: {glob_error}")
    if max_matches < 1:
        return _error("Grep max_matches must be >= 1")
    max_matches = min(max_matches, _HARD_MAX_MATCHES)

    try:
        # Validate the pattern in-process for a fast, actionable error; actual
        # scanning still happens in the killable worker.
        re.compile(pattern if regex else re.escape(pattern))
    except re.error as exc:
        return _error(f"Invalid Grep pattern: {exc}")

    guard = _guard(ctx)
    try:
        root = _resolve_directory(guard, root_raw)
    except PathSecurityError as exc:
        return _error(str(exc))
    except ToolError as exc:
        return _error(str(exc))

    request: dict[str, Any] = {
        "root": str(root.absolute),
        "denied_roots": [str(item) for item in guard.read_denyroots],
        "include_hidden": include_hidden,
        "file_glob": file_glob,
        "pattern": pattern,
        "regex": regex,
        "case_insensitive": case_insensitive,
        "max_matches": max_matches,
    }
    timeout = _grep_timeout(ctx)
    _check_cancel(ctx)
    try:
        response = await _scan_in_worker(request, ctx, timeout)
    except _GrepDeadline:
        # A timeout is a resource-limit outcome, never a "bad pattern" error.
        return ToolExecutionResult.text(
            "Grep: regex evaluation exceeded the "
            f"{timeout:g}s deadline and the worker was terminated (possible "
            "catastrophic backtracking); simplify the pattern or raise "
            "tools.grep_timeout_s",
            is_error=True,
            display="Grep: regex evaluation timed out",
            metrics={"timeout": True, "timeout_s": timeout, "root": root.key},
        )
    except OperationCancelled:
        raise
    except ToolError as exc:
        return _error(str(exc))

    if "error" in response:
        return _error(f"Invalid Grep pattern: {response['error']}")

    raw_matches = response.get("matches") or []
    matches = [
        (str(rel), int(lineno), str(line))
        for rel, lineno, line in raw_matches
    ]
    match_count = int(response.get("total_matches", len(matches)))
    max_bytes = _byte_budget(ctx, 128 * 1024)
    lines = [f"{rel}:{lineno}:{line}" for rel, lineno, line in matches]
    shown, byte_truncated = _cap_lines(lines, max_bytes)
    truncated = bool(
        response.get("truncated")
    ) or byte_truncated or len(shown) < match_count

    body = "\n".join(shown)
    context_note: str | None = None
    if truncated:
        marker = _truncation_marker(
            "Grep",
            len(shown),
            match_count,
            "matches",
            "narrow the pattern, path, or glob",
        )
        body = f"{body}\n{marker}" if body else marker
        context_note = marker
    display = (
        f"Grep {pattern!r}: {len(shown)} of {match_count} matches in "
        f"{int(response.get('files', 0))} file(s)"
    )
    if truncated:
        display += " (truncated)"
    return ToolExecutionResult.text(
        body,
        display=display,
        context_note=context_note,
        metrics={
            "matches": len(shown),
            "total_matches": match_count,
            "files": int(response.get("files", 0)),
            "files_scanned": int(response.get("files_scanned", 0)),
            "binary_skipped": int(response.get("binary_skipped", 0)),
            "truncated": truncated,
            "input_truncated": bool(response.get("input_truncated")),
            "line_truncated": bool(response.get("line_truncated")),
            "scan_truncated": bool(response.get("scan_truncated")),
            "store_truncated": bool(response.get("store_truncated")),
            "root": root.key,
        },
    )


__all__ = ["SPEC", "live_workers", "run"]
