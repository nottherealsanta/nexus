"""``bash``: run commands and control background jobs in the workspace.

The submitted command string is the tool's permission key: the engine matches
allow/deny/ask rules against it. That is **policy matching, not sandboxing** —
it does not constrain what the command may read or write.

Foreground commands return their captured output and exit code; a nonzero exit
is a normal, model-visible result. A foreground command still running after the
yield window (``tools.bash_yield_s``) is **yielded** to the background instead
of killed: the result names its ``job_id`` and the model calls ``wait``, which
returns when the job exits (``until="exit"``, the default) with only output not
returned before (a per-job read cursor). Background jobs can be read, waited
on, and stopped through action-specific requests addressed by registry-owned
job IDs. ``timeout_s`` is a hard kill limit capped at ``tools.bash_max_s``;
timeouts and cancellation SIGTERM the whole process group, grace, then SIGKILL,
and always reap the direct child. A cancelled ``wait`` leaves its job running.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from . import _jobs, bash_output, kill_shell

__all__ = ["SPEC", "run"]

_COMMAND = {
    "type": "string",
    "description": (
        "The shell command to execute. Runs with `-c` and stdin closed; "
        "defaults to `/bin/sh` and the workspace directory."
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
_WORKDIR = {
    "type": "string",
    "description": (
        "Optional working directory, relative to the workspace or an absolute "
        "path inside it. Symlink and traversal escapes are rejected."
    ),
}
_TIMEOUT = {
    "type": "number",
    "description": (
        "Optional hard limit in seconds: the process group is killed when it "
        "elapses (capped at tools.bash_max_s). Omit it for long commands; they "
        "are moved to the background after the yield window instead."
    ),
}
_BACKGROUND = {
    "type": "boolean",
    "description": (
        "Start the command in the background and return its job_id "
        "immediately."
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
    "enum": ["run", "status", "wait", "stop"],
    "default": "run",
    "description": "Run a command or control a background job.",
}
_JOB_ID = {
    "type": "string",
    "description": "A job_id previously returned by bash.",
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
        "the job's remaining lifetime; with until=output it is at most 30."
    ),
}
_UNTIL = {
    "type": "string",
    "enum": ["exit", "output"],
    "default": "exit",
    "description": (
        "wait: return when the job exits (default) or as soon as it prints "
        "new output."
    ),
}
_RUN_FIELDS = {
    "command", "timeout_s", "run_in_background", "env", "shell", "workdir"
}
_JOB_FIELDS = {"job_id"}
_OUTPUT_FIELDS = {"stdout_offset", "stderr_offset"}
_WAIT_FIELDS = {"wait_s", "until"}
_MAX_WAIT_S = 30.0

SPEC = ToolSpec(
    name="bash",
    group="bash",
    description=(
        "Run a non-interactive shell command, or inspect, wait for, or stop a "
        "background job started in this session. Commands default to /bin/sh "
        "and the workspace directory; run actions may select an allowlisted "
        "shell executable and a checked in-workspace directory. Use it to run "
        "tests, builds, and programs; prefer the dedicated read, search, and "
        "edit tools for file work when they are available. Run long commands "
        "(test suites, builds) in the foreground. If one is still running "
        "after the yield window you get a job_id; call action=wait once, and "
        "it returns when the job exits. Don't poll."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": _ACTION,
            "command": _COMMAND,
            "shell": _SHELL,
            "workdir": _WORKDIR,
            "job_id": _JOB_ID,
            "timeout_s": _TIMEOUT,
            "run_in_background": _BACKGROUND,
            "env": _ENV,
            "stdout_offset": _STDOUT_OFFSET,
            "stderr_offset": _STDERR_OFFSET,
            "wait_s": _WAIT,
            "until": _UNTIL,
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
    """Keep command permissions stable and qualify each job action."""
    action = data.get("action", "run")
    if action == "run":
        return str(data.get("command", ""))
    return f"{action}:{data.get('job_id', '')}"


def _validate_shape(
    args: dict[str, Any], max_runtime_s: float = 3600.0
) -> tuple[str, ToolExecutionResult | None]:
    action = args.get("action", "run")
    if not isinstance(action, str) or action not in {
        "run", "status", "wait", "stop"
    }:
        return "", _error("bash: action must be one of run, status, wait, stop")

    if action == "run":
        allowed = _RUN_FIELDS
        required = {"command"}
    elif action in {"status", "wait"}:
        allowed = _JOB_FIELDS | _OUTPUT_FIELDS
        required = {"job_id"}
        if action == "wait":
            allowed = allowed | _WAIT_FIELDS
    else:
        allowed = _JOB_FIELDS
        required = _JOB_FIELDS

    extras = set(args) - allowed - {"action"}
    if extras:
        return "", _error(
            f"bash: {action} does not accept: {', '.join(sorted(extras))}"
        )
    missing = required - set(args)
    if missing:
        return "", _error(
            f"bash: {action} requires: {', '.join(sorted(missing))}"
        )
    if action in {"status", "wait", "stop"} and (
        not isinstance(args.get("job_id"), str) or not args["job_id"]
    ):
        return "", _error("bash: 'job_id' must be a non-empty string")

    if action in {"status", "wait"}:
        for key in _OUTPUT_FIELDS:
            value = args.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return "", _error(f"bash: {key} must be a non-negative integer")
    if action == "wait" and "wait_s" in args:
        wait_s = args.get("wait_s")
        if (
            isinstance(wait_s, bool)
            or not isinstance(wait_s, (int, float))
            or wait_s <= 0
            or not math.isfinite(wait_s)
        ):
            return "", _error("bash: wait_s must be a positive finite number")
        cap = max_runtime_s if args.get("until", "exit") == "exit" else _MAX_WAIT_S
        if wait_s > cap:
            return "", _error(f"bash: wait_s must not exceed {cap:g} seconds")
    if action == "wait" and args.get("until", "exit") not in ("exit", "output"):
        return "", _error("bash: until must be 'exit' or 'output'")
    return action, None


def _resolve_workdir(workspace: Path, raw: object = None) -> Path:
    """Resolve a run cwd and require a real directory inside the workspace."""
    try:
        root = workspace.resolve(strict=True)
        if raw is None:
            candidate = root
        else:
            if not isinstance(raw, str) or not raw or not raw.strip():
                raise ValueError("workdir must be a non-empty path string")
            if "\x00" in raw or raw.startswith("~"):
                raise ValueError("workdir must not contain NUL or use home expansion")
            supplied = Path(raw)
            candidate = (supplied if supplied.is_absolute() else root / supplied).resolve(
                strict=True
            )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"invalid workdir: {exc}") from exc
    if not candidate.is_relative_to(root):
        raise ValueError("workdir must resolve inside the workspace")
    if not candidate.is_dir():
        raise ValueError(f"workdir is not a directory: {raw!r}")
    return candidate


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


async def run(
    args: dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    """Run a command or perform a validated action on an owned background job."""
    if not isinstance(args, dict):
        return _error("bash: arguments must be an object")
    action, invalid = _validate_shape(args, _jobs.max_runtime(ctx.config))
    if invalid is not None:
        return invalid

    if action == "status":
        return await bash_output._read_job_output(
            args, ctx, tool_name="bash", unified=True
        )
    if action == "wait":
        return await bash_output._read_job_output(
            args, ctx, tool_name="bash", max_wait_s=_MAX_WAIT_S, unified=True
        )
    if action == "stop":
        return await kill_shell._stop_job(
            args.get("job_id"), ctx, tool_name="bash"
        )

    command = args.get("command") if isinstance(args, dict) else None
    if not isinstance(command, str) or not command.strip():
        return _error("bash: 'command' must be a non-empty string")

    try:
        timeout = _jobs.resolve_timeout(args, ctx.config)
        env = _jobs.resolve_env(args)
        shell = _resolve_shell(args.get("shell"))
    except (TypeError, ValueError) as exc:
        return _error(f"bash: {exc}")

    workspace = Path(ctx.workspace)
    if not workspace.is_dir():
        return _error(f"bash: workspace does not exist: {workspace}")
    try:
        # Resolve here and immediately before spawn; never pass an untrusted
        # relative path to the subprocess layer.
        cwd = _resolve_workdir(workspace, args.get("workdir"))
    except ValueError as exc:
        return _error(f"bash: {exc}")

    registry = _jobs.registry_for(ctx)
    try:
        session_id = _jobs.require_session_id(ctx.session_id)
    except _jobs.JobRegistryError as exc:
        return _error(f"bash: {exc}")
    try:
        cwd = _resolve_workdir(workspace, args.get("workdir"))
        job = await registry.spawn(
            command,
            session_id=session_id,
            cwd=cwd,
            env=env,
            shell=shell,
            cwd_check=lambda: _resolve_workdir(workspace, args.get("workdir")),
        )
    except (OSError, RuntimeError, ValueError, _jobs.JobRegistryError) as exc:
        return _error(f"bash: failed to start command: {exc}")

    max_s = _jobs.max_runtime(ctx.config)
    limit = timeout if timeout is not None else max_s
    background = bool(args.get("run_in_background", False))
    if background:
        job.background = True
        _jobs.enforce_max_runtime(job, limit)
        job.read_stdout = len(job.stdout)
        job.read_stderr = len(job.stderr)
        return ToolExecutionResult.text(
            _jobs.format_job_output(job, show_offsets=True),
            display=(
                f"started {job.job_id} in background "
                f"(pid {job.pid})"
            ),
            context_note=(
                f"[bash {command!r} started in background as {job.job_id}; "
                "use action=wait (returns when it exits) or status/stop with "
                "this job_id]"
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
    )
    if outcome == "yielded":
        job.background = True
        text = _jobs.format_job_output(job, show_offsets=True)
        job.read_stdout = len(job.stdout)
        job.read_stderr = len(job.stderr)
        return ToolExecutionResult.text(
            f"[still running: moved to background after {yield_s:g}s; call "
            f"bash action=wait job_id={job.job_id} to block until it exits]\n"
            + text,
            display=f"moved {job.job_id} to background after {yield_s:g}s",
            context_note=(
                f"[Bash {command!r} still running after {yield_s:g}s; moved to "
                f"background as {job.job_id}. Call action=wait with this "
                "job_id; do not poll.]"
            ),
        )
    if outcome == "timeout":
        return ToolExecutionResult.text(
            f"[timed out after {limit:g}s; process group terminated]\n"
            + _jobs.format_job_output(job),
            is_error=True,
            context_note=(
                f"[Bash {command!r} timed out after {limit:g}s and was "
                "terminated; re-run with a longer timeout only if safe]"
            ),
        )
    if job.status is _jobs.JobStatus.TIMED_OUT:
        return ToolExecutionResult.text(
            f"[timed out after {limit:g}s; process group terminated]\n"
            + _jobs.format_job_output(job),
            is_error=True,
            context_note=(
                f"[Bash {command!r} timed out after {limit:g}s and was "
                "terminated]"
            ),
        )
    return ToolExecutionResult.text(
        _jobs.format_job_output(job),
        is_error=job.exit_code != 0,
        context_note=(
            f"[Bash {command!r} exited {job.exit_code}; output evicted. Re-run "
            "the command only if it is safe to repeat.]"
        ),
    )
