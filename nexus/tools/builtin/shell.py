"""``shell``: run commands, control jobs, read job logs, and set the session cwd.

The submitted command string is the tool's permission key: the engine matches
allow/deny/ask rules against it. That is **policy matching, not sandboxing** —
it does not constrain what the command may read or write. (Glob allow rules no
longer auto-allow commands with unquoted control syntax; see
``nexus/tools/permissions.py``.)

Foreground commands return their captured output and exit code; a nonzero exit
is a normal, model-visible result. A foreground command still running after the
yield window (``tools.shell_yield_s``) is **yielded** to the background instead
of killed: the result names its ``job_id`` and the model calls ``wait``, which
returns when the job exits (``until="exit"``, the default) with only output not
returned before (a per-job read cursor). Background jobs can be read, waited
on, and stopped through action-specific requests addressed by registry-owned
job IDs. ``timeout_s`` is a hard kill limit capped at ``tools.shell_max_s``;
timeouts and cancellation SIGTERM the whole process group, grace, then SIGKILL,
and always reap the direct child. A cancelled ``wait`` leaves its job running.

Every job also writes one combined, arrival-ordered **log file** (see
``_shell_logs``); output shown to the model is bounded, the log is not. ``log``
reads it back. ``cd`` sets a persistent per-session working directory that
``run`` uses when no ``cwd`` is given. The tool was called ``bash``; that name
(and ``Bash``) is an alias handled by ``nexus/tools/names.py``.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from pathlib import Path
from typing import Any

from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from . import _jobs, _shell_logs, bash_output, kill_shell
from ._output_limit import MAX_BYTES

__all__ = ["SPEC", "run"]

_COMMAND = {
    "type": "string",
    "description": (
        "The shell command to execute. Runs with `-c` and stdin closed; "
        "defaults to `/bin/sh` and the session working directory."
    ),
}
_SHELLS = ("/bin/sh", "/bin/bash", "/bin/zsh")
_SHELL = {
    "type": "string",
    "enum": list(_SHELLS),
    "description": (
        "Optional explicit shell executable. Only /bin/sh, /bin/bash, and "
        "/bin/zsh are accepted; shell arguments are not allowed."
    ),
}
_CWD = {
    "type": "string",
    "description": (
        "Optional working directory for this run: relative to the session "
        "working directory (see action=cd) or an absolute path inside the "
        "workspace. Symlink and traversal escapes are rejected."
    ),
}
_TIMEOUT = {
    "type": "number",
    "description": (
        "Optional hard limit in seconds: the process group is killed when it "
        "elapses (capped at tools.shell_max_s). Omit it for long commands; "
        "they are moved to the background after the yield window instead."
    ),
}
_BACKGROUND = {
    "type": "boolean",
    "description": (
        "Start the command in the background and return its job_id "
        "immediately. With timeout_s it is a finite job; without it the job "
        "is persistent: it runs until you stop it (a dev server, a watcher), "
        "at most 4 per session, and you are continued if it exits on its own. "
        "Use wait with until=match to learn that it is ready."
    ),
}
_ENV = {
    "type": "object",
    "description": (
        "Extra environment variables overlaid on the inherited environment."
    ),
    "additionalProperties": {"type": "string"},
}
_ACTION = {
    "type": "string",
    "enum": ["run", "status", "wait", "stop", "log", "cd"],
    "default": "run",
    "description": (
        "Run a command, control or read a job, or set the session working "
        "directory."
    ),
}
_JOB_ID = {
    "type": "string",
    "description": "A job_id previously returned by shell.",
}
_STDOUT_OFFSET = {
    "type": "integer",
    "description": "Non-negative byte offset into captured stdout.",
}
_STDERR_OFFSET = {
    "type": "integer",
    "description": "Non-negative byte offset into captured stderr.",
}
_WAIT = {
    "type": "number",
    "description": (
        "Maximum seconds to wait. Optional: with until=exit it defaults to "
        "the job's remaining lifetime (at most 600 for a persistent job); with "
        "until=output it is at most 30 and with until=match at most 120."
    ),
}
_UNTIL = {
    "type": "string",
    "enum": ["exit", "output", "match"],
    "default": "exit",
    "description": (
        "wait: return when the job exits (default), as soon as it prints new "
        "output, or when a new output line matches `pattern` (readiness of a "
        "persistent job)."
    ),
}
_PATTERN = {
    "type": "string",
    "description": (
        "wait with until=match: a regular expression (at most 256 characters) "
        "tested against each complete new output line, ANSI colours removed. "
        "On timeout the job keeps running."
    ),
}
_PATH = {
    "type": "string",
    "description": (
        "cd: the new session working directory, relative to the current one "
        "or an absolute path inside the workspace. Use '.' from a broken "
        "directory to go back to the workspace root."
    ),
}
_TAIL_LINES = {
    "type": "integer",
    "description": "log: show the last N lines (default 200, at most 2000).",
}
_LOG_OFFSET = {
    "type": "integer",
    "description": "log: read bytes starting at this non-negative offset.",
}
_LOG_LIMIT = {
    "type": "integer",
    "description": (
        "log: bytes to read at offset (default 32768, at most 51200), or "
        "matching lines for grep (default 100, at most 500)."
    ),
}
_GREP = {
    "type": "string",
    "description": (
        "log: return the lines containing this literal text (at most 256 "
        "characters), with line numbers."
    ),
}
_RUN_FIELDS = {"command", "timeout_s", "background", "env", "shell", "cwd"}
_JOB_FIELDS = {"job_id"}
_OUTPUT_FIELDS = {"stdout_offset", "stderr_offset"}
_WAIT_FIELDS = {"wait_s", "until", "pattern"}
_LOG_FIELDS = {"tail_lines", "offset", "limit", "grep"}
_ACTIONS = ("run", "status", "wait", "stop", "log", "cd")
_MAX_WAIT_S = 30.0
#: Manager backstop for the actions that never block (status, stop, log, cd).
_QUICK_ACTION_BOUND_S = 60.0
#: Extra seconds a ``wait until=match`` may spend scanning past its own ``wait_s``.
_MATCH_SCAN_ALLOWANCE_S = 15.0
_MAX_COMMAND_CHARS = 16_000
_MAX_GREP_CHARS = 256
_DEFAULT_TAIL_LINES = 200
_MAX_TAIL_LINES = 2000
_DEFAULT_LOG_BYTES = 32 * 1024
_DEFAULT_GREP_LINES = 100
_MAX_GREP_LINES = 500
_MAX_GREP_LINE_CHARS = 500
#: Bytes of the file scanned by one ``grep`` call.
_MAX_GREP_SCAN_BYTES = 256 * 1024 * 1024
#: Bytes read from the end of the file to find the last N lines.
_TAIL_WINDOW_BYTES = 512 * 1024
#: Renamed ``bash`` arguments, named in the error when a caller still uses them.
_RENAMED = {"workdir": "cwd", "run_in_background": "background"}

SPEC = ToolSpec(
    name="shell",
    group="shell",
    description=(
        "Run a non-interactive shell command, or inspect, wait for, stop, or "
        "read the log of a job started in this session, or set the session "
        "working directory (action=cd). Commands default to /bin/sh and the "
        "session working directory; run actions may select an allowlisted "
        "shell executable and a checked in-workspace `cwd`. Prefer `cwd` over "
        "`cd dir && ...` in a command: a `cd` inside a command does not "
        "persist, while action=cd does. Use it to run tests, builds, and "
        "programs; prefer the dedicated read, search, and edit tools for file "
        "work when they are available. Run long commands (test suites, "
        "builds) in the foreground. If one is still running after the yield "
        "window you get a job_id; call action=wait once, and it returns when "
        "the job exits. Don't poll. Output shown here is truncated to its "
        "head and tail; the full output is kept in a log file named in the "
        "result, readable with action=log. Do not daemonise processes; a "
        "background process left running by a finished command is stopped. "
        "For a dev server or watcher use background=true without timeout_s "
        "(a persistent job), call action=wait with until=match and a pattern "
        "for its ready line, and action=stop when done."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": _ACTION,
            "command": _COMMAND,
            "shell": _SHELL,
            "cwd": _CWD,
            "path": _PATH,
            "job_id": _JOB_ID,
            "timeout_s": _TIMEOUT,
            "background": _BACKGROUND,
            "env": _ENV,
            "stdout_offset": _STDOUT_OFFSET,
            "stderr_offset": _STDERR_OFFSET,
            "wait_s": _WAIT,
            "until": _UNTIL,
            "pattern": _PATTERN,
            "tail_lines": _TAIL_LINES,
            "offset": _LOG_OFFSET,
            "limit": _LOG_LIMIT,
            "grep": _GREP,
        },
        "additionalProperties": False,
    },
    bundle="shell",
    mutates=True,
    concurrency="exclusive",
    permission_key=lambda data: _permission_key(data),
)


def _error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult.text(message, is_error=True)


def _permission_key(data: dict[str, Any]) -> str:
    """Keep command permissions stable and qualify each non-run action."""
    action = data.get("action", "run")
    if action == "run":
        return str(data.get("command", ""))
    if action == "cd":
        return f"cd:{data.get('path', '')}"
    return f"{action}:{data.get('job_id', '')}"


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_shape(
    args: dict[str, Any], max_runtime_s: float = 3600.0
) -> tuple[str, ToolExecutionResult | None]:
    action = args.get("action", "run")
    if not isinstance(action, str) or action not in _ACTIONS:
        return "", _error(
            "shell: action must be one of " + ", ".join(_ACTIONS)
        )

    if action == "run":
        allowed = _RUN_FIELDS
        required = {"command"}
    elif action in {"status", "wait"}:
        allowed = _JOB_FIELDS | _OUTPUT_FIELDS
        required = {"job_id"}
        if action == "wait":
            allowed = allowed | _WAIT_FIELDS
    elif action == "log":
        allowed = _JOB_FIELDS | _LOG_FIELDS
        required = _JOB_FIELDS
    elif action == "cd":
        allowed = {"path"}
        required = {"path"}
    else:
        allowed = _JOB_FIELDS
        required = _JOB_FIELDS

    extras = set(args) - allowed - {"action"}
    if extras:
        hint = ""
        renamed = [f"{old} -> {new}" for old, new in _RENAMED.items() if old in extras]
        if renamed:
            hint = f" (renamed: {', '.join(renamed)})"
        return "", _error(
            f"shell: {action} does not accept: {', '.join(sorted(extras))}{hint}"
        )
    missing = required - set(args)
    if missing:
        return "", _error(
            f"shell: {action} requires: {', '.join(sorted(missing))}"
        )
    if action in {"status", "wait", "stop", "log"} and (
        not isinstance(args.get("job_id"), str) or not args["job_id"]
    ):
        return "", _error("shell: 'job_id' must be a non-empty string")
    if action == "cd" and (
        not isinstance(args.get("path"), str) or not args["path"].strip()
    ):
        return "", _error("shell: 'path' must be a non-empty string")

    if action in {"status", "wait"}:
        for key in _OUTPUT_FIELDS:
            value = args.get(key, 0)
            if not _is_int(value) or value < 0:
                return "", _error(f"shell: {key} must be a non-negative integer")
    if action == "wait":
        until = args.get("until", "exit")
        if until not in ("exit", "output", "match"):
            return "", _error("shell: until must be 'exit', 'output' or 'match'")
        if until == "match":
            if "pattern" not in args:
                return "", _error("shell: until=match requires: pattern")
            try:
                _jobs.compile_match_pattern(args["pattern"])
            except ValueError as exc:
                return "", _error(f"shell: {exc}")
        elif "pattern" in args:
            return "", _error("shell: pattern applies only with until=match")
    if action == "wait" and "wait_s" in args:
        wait_s = args.get("wait_s")
        if (
            isinstance(wait_s, bool)
            or not isinstance(wait_s, (int, float))
            or wait_s <= 0
            or not math.isfinite(wait_s)
        ):
            return "", _error("shell: wait_s must be a positive finite number")
        until = args.get("until", "exit")
        cap = (
            max_runtime_s
            if until == "exit"
            else _jobs.MATCH_WAIT_MAX_S
            if until == "match"
            else _MAX_WAIT_S
        )
        if wait_s > cap:
            return "", _error(f"shell: wait_s must not exceed {cap:g} seconds")
    if action == "log":
        problem = _validate_log_fields(args)
        if problem is not None:
            return "", _error(problem)
    return action, None


def _validate_log_fields(args: dict[str, Any]) -> str | None:
    modes = [key for key in ("tail_lines", "offset", "grep") if key in args]
    if len(modes) > 1:
        return "shell: log accepts at most one of tail_lines, offset, grep"
    for key in ("tail_lines", "offset", "limit"):
        if key in args and (not _is_int(args[key]) or args[key] < 0):
            return f"shell: {key} must be a non-negative integer"
    if "tail_lines" in args and not 1 <= args["tail_lines"] <= _MAX_TAIL_LINES:
        return f"shell: tail_lines must be between 1 and {_MAX_TAIL_LINES}"
    if "limit" in args and "offset" not in args and "grep" not in args:
        return "shell: limit applies only with offset or grep"
    if "grep" in args:
        pattern = args["grep"]
        if not isinstance(pattern, str) or not pattern:
            return "shell: grep must be a non-empty string"
        if len(pattern) > _MAX_GREP_CHARS or "\x00" in pattern:
            return f"shell: grep must be at most {_MAX_GREP_CHARS} characters"
    return None


# -- working directory --------------------------------------------------------


def _workspace_root(workspace: Path) -> Path:
    try:
        return workspace.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"invalid workspace: {exc}") from exc


def _checked_dir(root: Path, candidate: Path, label: str, raw: object) -> Path:
    if not candidate.is_relative_to(root):
        raise ValueError(f"{label} must resolve inside the workspace")
    if not candidate.is_dir():
        raise ValueError(f"{label} is not a directory: {raw!r}")
    return candidate


def _session_base(workspace: Path, session_id: str, *, strict: bool) -> Path:
    """The session cwd (recorded by ``cd``), else the workspace root.

    A recorded cwd that vanished or left the workspace is an error naming it and
    the reset; there is no silent fallback. ``strict=False`` (used by ``cd``
    itself) resolves from the workspace root instead so ``cd .`` can reset.
    """
    root = _workspace_root(workspace)
    stored = _shell_logs.read_session_cwd(workspace, session_id)
    if stored is None:
        return root
    try:
        return _checked_dir(
            root, Path(stored).resolve(strict=True), "session cwd", stored
        )
    except (OSError, RuntimeError, ValueError) as exc:
        if not strict:
            return root
        raise ValueError(
            f"session cwd {stored!r} is no longer valid ({exc}); run "
            "action=cd with path='.' to go back to the workspace root"
        ) from exc


def _resolve_dir(
    workspace: Path, session_id: str, raw: object, *, label: str, strict: bool = True
) -> Path:
    """Resolve ``raw`` (``None`` = the session cwd) to a real in-workspace directory."""
    try:
        root = _workspace_root(workspace)
        base = _session_base(workspace, session_id, strict=strict)
        if raw is None:
            return _checked_dir(root, base, label, raw)
        if not isinstance(raw, str) or not raw or not raw.strip():
            raise ValueError(f"{label} must be a non-empty path string")
        if "\x00" in raw or raw.startswith("~"):
            raise ValueError(f"{label} must not contain NUL or use home expansion")
        supplied = Path(raw)
        candidate = (supplied if supplied.is_absolute() else base / supplied).resolve(
            strict=True
        )
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    return _checked_dir(root, candidate, label, raw)


def _display_path(workspace: Path, path: Path) -> str:
    try:
        relative = path.relative_to(_workspace_root(workspace))
    except (ValueError, OSError, RuntimeError):
        return str(path)
    return relative.as_posix() if relative.parts else "."


def _resolve_shell(raw: object = None) -> str | None:
    """Accept only an explicit, available shell executable from the allowlist."""
    if raw is None:
        return None
    if not isinstance(raw, str) or raw not in _SHELLS:
        raise ValueError(
            "shell must be one of: " + ", ".join(_SHELLS)
        )
    if not os.path.isfile(raw) or not os.access(raw, os.X_OK):
        raise ValueError(f"shell executable is not available: {raw}")
    return raw


async def _cd(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    workspace = Path(ctx.workspace)
    try:
        session_id = _jobs.require_session_id(ctx.session_id)
    except _jobs.JobRegistryError as exc:
        return _error(f"shell: {exc}")
    if not workspace.is_dir():
        return _error(f"shell: workspace does not exist: {workspace}")
    try:
        previous = _session_base(workspace, session_id, strict=False)
        target = _resolve_dir(
            workspace, session_id, args["path"], label="path", strict=False
        )
        _shell_logs.write_session_cwd(workspace, session_id, str(target))
    except (OSError, ValueError) as exc:
        return _error(f"shell: {exc}")
    shown = _display_path(workspace, target)
    return ToolExecutionResult.text(
        f"cwd: {shown}\nabsolute: {target}\nprevious: "
        f"{_display_path(workspace, previous)}\n"
        "Later runs start here; `cwd` is resolved against it. A `cd` inside "
        "a command does not change it.",
        display=f"cwd {shown}",
        context_note=f"[shell cd: session working directory is now {shown}]",
    )


# -- log -----------------------------------------------------------------------


def _human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def _clip_tail(data: bytes) -> bytes:
    return data[-MAX_BYTES:] if len(data) > MAX_BYTES else data


def _read_range(path: Path, offset: int, count: int) -> tuple[bytes, int]:
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        handle.seek(min(offset, size))
        return handle.read(count), size


def _read_tail(path: Path, lines: int) -> tuple[bytes, int, int]:
    """``(data, start_offset, size)`` for the last ``lines`` lines of the file."""
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        start = max(size - _TAIL_WINDOW_BYTES, 0)
        handle.seek(start)
        data = handle.read()
    pieces = data.splitlines(keepends=True)
    if start > 0 and pieces:
        # The first piece is probably cut mid-line; drop it unless it is all we have.
        if len(pieces) > 1:
            start += len(pieces[0])
            pieces = pieces[1:]
    kept = pieces[-lines:]
    dropped = sum(len(piece) for piece in pieces[: len(pieces) - len(kept)])
    return b"".join(kept), start + dropped, size


def _grep_file(path: Path, needle: bytes, limit: int) -> tuple[list[tuple[int, bytes]], int, int, bool]:
    """``(matches, scanned_bytes, size, more)`` for lines containing ``needle``."""
    matches: list[tuple[int, bytes]] = []
    scanned = 0
    more = False
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        number = 0
        for line in handle:
            number += 1
            scanned += len(line)
            if needle in line:
                if len(matches) >= limit:
                    more = True
                    break
                matches.append((number, line.rstrip(b"\r\n")))
            if scanned >= _MAX_GREP_SCAN_BYTES:
                more = more or scanned < size
                break
    return matches, scanned, size, more


async def _log(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    job_id = args["job_id"]
    registry = _jobs.registry_for(ctx)
    try:
        session_id = _jobs.require_session_id(ctx.session_id)
    except _jobs.JobRegistryError as exc:
        return _error(f"shell: {exc}")
    job = registry.job(job_id, session_id=session_id)
    path: Path | None = None
    if job is not None:
        path = job.log.path
        if path is None:
            return _error(f"shell: {job_id} has no log file")
    elif _shell_logs.JOB_ID_RE.fullmatch(job_id):
        # Evicted from memory: the file is addressed by (session, job) only, so
        # another session's id can never resolve here.
        path = _shell_logs.log_path(Path(ctx.workspace), session_id, job_id)
    if path is None or not path.is_file():
        if job is None:
            return _error(
                f"shell: unknown job_id {job_id!r}, or its log is no longer "
                "available (retention or deletion)"
            )
        return _error(
            f"shell: log for {job_id} is no longer available (retention or "
            "deletion)"
        )

    header = [f"job_id: {job_id}"]
    if job is not None:
        header.append(f"status: {job.status.value}")
    header.append(f"log: {path}")
    notices = job.log.notices() if job is not None else []

    try:
        if "grep" in args:
            limit = min(
                args.get("limit", _DEFAULT_GREP_LINES) or _DEFAULT_GREP_LINES,
                _MAX_GREP_LINES,
            )
            matches, scanned, size, more = await asyncio.to_thread(
                _grep_file, path, args["grep"].encode("utf-8"), limit
            )
            header.append(f"size: {size} bytes")
            lines = [
                f"{number}: "
                + raw.decode("utf-8", "replace")[:_MAX_GREP_LINE_CHARS]
                for number, raw in matches
            ]
            footer = f"[{len(matches)} matching lines; scanned {scanned} of {size} bytes"
            footer += "; more matches or unscanned bytes remain]" if more else "]"
            body = "\n".join(lines) if lines else "(no matches)"
            text = _clip_text(body) + "\n" + footer
        elif "offset" in args:
            count = min(
                args.get("limit", _DEFAULT_LOG_BYTES) or _DEFAULT_LOG_BYTES, MAX_BYTES
            )
            data, size = await asyncio.to_thread(_read_range, path, args["offset"], count)
            start = min(args["offset"], size)
            header.append(f"size: {size} bytes")
            body = data.decode("utf-8", "replace")
            end = start + len(data)
            footer = f"[bytes {start}-{end} of {size}"
            footer += f"; next offset {end}]" if end < size else "; end of log]"
            text = (body if body else "(no output at this offset)") + "\n" + footer
        else:
            wanted = args.get("tail_lines", _DEFAULT_TAIL_LINES)
            data, start, size = await asyncio.to_thread(_read_tail, path, wanted)
            header.append(f"size: {size} bytes")
            data = _clip_tail(data)
            start = size - len(data)  # the kept lines always run to EOF
            body = data.decode("utf-8", "replace") if data else "(log is empty)"
            footer = f"[bytes {start}-{size} of {size}"
            footer += "; earlier output is in the log file]" if start > 0 else "; start of log]"
            text = body + "\n" + footer
    except OSError as exc:
        return _error(f"shell: cannot read log for {job_id}: {exc.strerror or exc}")
    return ToolExecutionResult.text(
        "\n".join(header + notices + [text]),
        context_note=f"[shell log {job_id}: output evicted; call action=log again to re-read it]",
    )


def _clip_text(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= MAX_BYTES:
        return text
    return raw[:MAX_BYTES].decode("utf-8", "ignore") + "\n[matches clipped]"


# -- run -------------------------------------------------------------------------


async def _prune_logs(ctx: ToolContext, registry: _jobs.JobRegistry) -> None:
    """Best-effort retention before a new job's log is created."""
    _per_job, total, days = _jobs.log_settings(ctx.config)
    try:
        await asyncio.to_thread(
            _shell_logs.prune_logs,
            Path(ctx.workspace),
            max_total_bytes=total,
            retention_days=days,
            protected=registry.active_log_paths(),
        )
    except Exception:  # noqa: BLE001 - retention must never block a command
        return


async def run(
    args: dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    """Run a command or perform a validated action on an owned job or the cwd."""
    if not isinstance(args, dict):
        return _error("shell: arguments must be an object")
    action, invalid = _validate_shape(args, _jobs.max_runtime(ctx.config))
    if invalid is not None:
        return invalid

    if action == "status":
        return await bash_output._read_job_output(
            args, ctx, tool_name="shell", unified=True
        )
    if action == "wait":
        return await bash_output._read_job_output(
            args, ctx, tool_name="shell", max_wait_s=_MAX_WAIT_S, unified=True
        )
    if action == "stop":
        return await kill_shell._stop_job(
            args.get("job_id"), ctx, tool_name="shell"
        )
    if action == "log":
        return await _log(args, ctx)
    if action == "cd":
        return await _cd(args, ctx)

    command = args.get("command") if isinstance(args, dict) else None
    if not isinstance(command, str) or not command.strip():
        return _error("shell: 'command' must be a non-empty string")
    if len(command) > _MAX_COMMAND_CHARS:
        return _error(f"shell: command is longer than {_MAX_COMMAND_CHARS} characters")

    try:
        timeout = _jobs.resolve_timeout(args, ctx.config)
        env = _jobs.resolve_env(args)
        shell = _resolve_shell(args.get("shell"))
    except (TypeError, ValueError) as exc:
        return _error(f"shell: {exc}")

    background = bool(args.get("background", False))
    # ``background`` without ``timeout_s`` is a persistent job, except for a
    # legacy ``run_in_background`` call, which keeps its lifetime cap.
    persistent = (
        background
        and timeout is None
        and not getattr(ctx, "legacy_capped_background", False)
    )
    if persistent and getattr(ctx, "agent_id", "root") != "root":
        return _error(
            "shell: a subagent cannot start a persistent job (background "
            "without timeout_s): nothing would own it after the subagent ends. "
            "Pass timeout_s for a finite background job, or ask the parent to "
            "start the process."
        )

    workspace = Path(ctx.workspace)
    if not workspace.is_dir():
        return _error(f"shell: workspace does not exist: {workspace}")
    registry = _jobs.registry_for(ctx)
    try:
        session_id = _jobs.require_session_id(ctx.session_id)
    except _jobs.JobRegistryError as exc:
        return _error(f"shell: {exc}")

    def resolve_cwd() -> Path:
        return _resolve_dir(workspace, session_id, args.get("cwd"), label="cwd")

    try:
        # Resolve here and immediately before spawn; never pass an untrusted
        # relative path to the subprocess layer.
        cwd = resolve_cwd()
    except ValueError as exc:
        return _error(f"shell: {exc}")

    per_job_max, _total, _days = _jobs.log_settings(ctx.config)
    await _prune_logs(ctx, registry)
    try:
        job = await registry.spawn(
            command,
            session_id=session_id,
            cwd=cwd,
            env=env,
            shell=shell,
            cwd_check=resolve_cwd,
            log_dir=_shell_logs.session_dir(workspace, session_id),
            log_max_bytes=per_job_max,
            background=background,
            persistent=persistent,
            persistent_limits=_jobs.persistent_limits(ctx.config),
        )
    except _jobs.PersistentJobRefused as exc:
        return _persistent_refusal(exc, workspace)
    except (OSError, RuntimeError, ValueError, _jobs.JobRegistryError) as exc:
        return _error(f"shell: failed to start command: {exc}")

    max_s = _jobs.max_runtime(ctx.config)
    limit = timeout if timeout is not None else max_s
    if background:
        if not persistent:
            _jobs.enforce_max_runtime(job, limit)
        _jobs.mark_read(job)
        if persistent:
            return ToolExecutionResult.text(
                _with_log_line(job, _jobs.format_job_output(job, show_offsets=True)),
                display=f"started persistent {job.job_id} (pid {job.pid})",
                context_note=(
                    f"[shell {command!r} started as persistent job {job.job_id}; "
                    "it runs until stopped. Use action=wait until=match "
                    "pattern=... for readiness, status/log to inspect, stop to "
                    "end it]"
                ),
            )
        return ToolExecutionResult.text(
            _with_log_line(job, _jobs.format_job_output(job, show_offsets=True)),
            display=(
                f"started {job.job_id} in background "
                f"(pid {job.pid})"
            ),
            context_note=(
                f"[shell {command!r} started in background as {job.job_id}; "
                "use action=wait (returns when it exits) or status/stop/log "
                "with this job_id]"
            ),
        )

    yield_s = _jobs.yield_window(ctx.config)
    # A hard limit inside the yield window keeps today's behavior: block, then
    # kill. Otherwise block for the yield window and hand the job over to the
    # background, where a watchdog still enforces the hard limit.
    yielding = limit > yield_s
    if yielding:
        _jobs.enforce_max_runtime(job, limit)
    outcome = await _jobs.await_job(
        job,
        timeout=None if yielding else limit,
        yield_after=yield_s if yielding else None,
        cancel_token=ctx.cancel_token,
        tick=bash_output._progress_tick(job, ctx),
        background_signal=True,
    )
    # A job that exited in the same tick it was due to be backgrounded stays a
    # foreground result: ``move_to_background`` refuses, so the exit is reported
    # once (here and in ``shell.job_exited``) and no ``job_yielded`` is recorded.
    user_requested = job.request_background_seen
    if outcome == "yielded" and job.move_to_background(
        "user" if user_requested else "yield_window"
    ):
        if not yielding:
            # Backgrounded inside the hard-limit window: the limit still
            # counts from the start, now enforced by the watchdog.
            _jobs.enforce_max_runtime(
                job, max(limit - (time.time() - job.created_at), 0.001)
            )
        text = _jobs.format_job_output(job, show_offsets=True)
        _jobs.mark_read(job)
        if user_requested:
            lead = (
                "[still running: moved to background at the user's request; "
                f"call shell action=wait job_id={job.job_id} to block until it "
                "exits]\n"
            )
            display = f"moved {job.job_id} to background by the user"
            note = (
                f"[shell {command!r} still running; the user moved it to the "
                f"background as {job.job_id}. Call action=wait with this "
                "job_id; do not poll.]"
            )
        else:
            lead = (
                f"[still running: moved to background after {yield_s:g}s; call "
                f"shell action=wait job_id={job.job_id} to block until it exits]\n"
            )
            display = f"moved {job.job_id} to background after {yield_s:g}s"
            note = (
                f"[shell {command!r} still running after {yield_s:g}s; moved to "
                f"background as {job.job_id}. Call action=wait with this "
                "job_id; do not poll.]"
            )
        return ToolExecutionResult.text(
            lead + _with_log_line(job, text), display=display, context_note=note
        )
    if outcome == "timeout" or job.status is _jobs.JobStatus.TIMED_OUT:
        return ToolExecutionResult.text(
            f"[timed out after {limit:g}s; process group terminated]\n"
            + _jobs.format_job_output(job),
            is_error=True,
            context_note=(
                f"[shell {command!r} timed out after {limit:g}s and was "
                "terminated; re-run with a longer timeout only if safe]"
            ),
        )
    return ToolExecutionResult.text(
        _jobs.format_job_output(job),
        is_error=job.exit_code != 0,
        context_note=(
            f"[shell {command!r} exited {job.exit_code}; output evicted. Re-run "
            "the command only if it is safe to repeat.]"
        ),
    )


def _persistent_refusal(
    exc: _jobs.PersistentJobRefused, workspace: Path
) -> ToolExecutionResult:
    """The model-visible error for a duplicate or over-limit persistent job.

    A duplicate is an **error that names the running job** (``job_id:`` first, as
    in every job result) rather than a success with that id: nothing new was
    started, so the model must not believe it owns a fresh job, but it has
    everything it needs to ``wait``/``status`` it or ``stop`` it and start again.
    Only this session's jobs are ever listed.
    """
    lines = [f"shell: persistent job not started: {exc}"]
    if exc.reason == "duplicate":
        existing = exc.jobs[0]
        lines = [
            f"job_id: {existing.job_id}",
            "status: running",
            "kind: persistent",
            f"cwd: {existing.cwd}",
            f"shell: persistent job not started: {exc}. Use action=wait "
            f"(until=match pattern=...), status or log with job_id="
            f"{existing.job_id}, or action=stop first to start it again.",
        ]
    else:
        for job in exc.jobs:
            command = job.command if len(job.command) <= 80 else job.command[:80] + "…"
            lines.append(
                f"- {job.job_id}: {command!r} in "
                f"{_display_path(workspace, job.cwd)}"
            )
        if exc.jobs:
            lines.append("Stop one with action=stop job_id=<id> to free a slot.")
    return _error("\n".join(lines))


def action_bound_s(args: dict[str, Any], max_runtime_s: float) -> float:
    """Longest a ``shell`` call with these arguments may legitimately block (seconds).

    The tool manager's backstop is this plus a few seconds, instead of the
    one-hour ``shell_max_s`` that only a foreground run needs.
    """
    action = args.get("action", "run")
    if action == "run":
        timeout = args.get("timeout_s")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
            return min(float(timeout), max_runtime_s)
        return max_runtime_s
    if action == "wait":
        until = args.get("until", "exit")
        wait_s = args.get("wait_s")
        valid = (
            isinstance(wait_s, (int, float))
            and not isinstance(wait_s, bool)
            and math.isfinite(wait_s)
            and wait_s > 0
        )
        if until == "match":
            # The pattern scan has its own deadline (``tools.grep_timeout_s``).
            return (min(float(wait_s), _jobs.MATCH_WAIT_MAX_S) if valid else _jobs.MATCH_WAIT_MAX_S) + _MATCH_SCAN_ALLOWANCE_S
        if until == "output":
            return min(float(wait_s), _MAX_WAIT_S) if valid else _MAX_WAIT_S
        return min(float(wait_s), max_runtime_s) if valid else max_runtime_s
    return _QUICK_ACTION_BOUND_S


def _with_log_line(job: _jobs.ShellJob, text: str) -> str:
    """Name the job's cwd and full log right after the status line."""
    extra = [f"cwd: {job.cwd}"]
    if job.persistent:
        extra.append("timeout: none (runs until stopped)")
    if job.log.path is not None:
        extra.append(f"log: {job.log.path}")
    lines = text.split("\n")
    # Insert after ``job_id`` and ``status`` so the header stays labelled lines.
    return "\n".join(lines[:2] + extra + lines[2:])
