"""``Bash``: run a non-interactive shell command in the workspace.

The submitted command string is the tool's permission key: the engine matches
allow/deny/ask rules against it. That is **policy matching, not sandboxing** —
it does not constrain what the command may read or write.

Foreground commands return their captured output and exit code; a nonzero exit
is a normal, model-visible result. ``run_in_background=true`` starts the job and
returns its registry-owned ``job_id`` immediately for later ``BashOutput`` /
``KillShell`` calls. Timeouts and cancellation SIGTERM the whole process group,
grace, then SIGKILL, and always reap the direct child.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from . import _jobs

__all__ = ["SPEC", "run"]

_COMMAND = {
    "type": "string",
    "description": (
        "The shell command to execute. Runs via `/bin/sh -c` with the "
        "workspace as the working directory and stdin closed."
    ),
}
_TIMEOUT = {
    "type": "number",
    "description": (
        "Wall-clock timeout in seconds before the process group is "
        "terminated. Defaults to the configured tools.bash_timeout_s."
    ),
}
_BACKGROUND = {
    "type": "boolean",
    "description": (
        "Start the command in the background and return its job_id "
        "immediately. Poll with BashOutput and stop it with KillShell."
    ),
}
_ENV = {
    "type": "object",
    "description": (
        "Extra environment variables overlaid on the inherited environment."
    ),
    "additionalProperties": {"type": "string"},
}

SPEC = ToolSpec(
    name="Bash",
    description=(
        "Execute a non-interactive shell command in the workspace. Returns "
        "stdout, stderr, and the exit code. Use run_in_background for "
        "long-running commands."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": _COMMAND,
            "timeout_s": _TIMEOUT,
            "run_in_background": _BACKGROUND,
            "env": _ENV,
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    bundle="shell",
    mutates=True,
    concurrency="exclusive",
    permission_key=lambda data: str(data.get("command", "")),
)


def _error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult.text(message, is_error=True)


async def run(
    args: dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    """Spawn the command and either await it or register it in the background."""
    command = args.get("command") if isinstance(args, dict) else None
    if not isinstance(command, str) or not command.strip():
        return _error("Bash: 'command' must be a non-empty string")

    try:
        timeout = _jobs.resolve_timeout(args, ctx.config)
        env = _jobs.resolve_env(args)
    except (TypeError, ValueError) as exc:
        return _error(f"Bash: {exc}")

    workspace = Path(ctx.workspace)
    if not workspace.is_dir():
        return _error(f"Bash: workspace does not exist: {workspace}")

    registry = _jobs.registry_for(ctx)
    try:
        job = await registry.spawn(command, cwd=workspace, env=env)
    except (OSError, ValueError, _jobs.JobRegistryError) as exc:
        return _error(f"Bash: failed to start command: {exc}")

    background = bool(args.get("run_in_background", False))
    if background:
        return ToolExecutionResult.text(
            _jobs.format_job_output(job, show_offsets=True),
            display=(
                f"started {job.job_id} in background "
                f"(pid {job.pid}); poll with BashOutput"
            ),
            context_note=(
                f"[Bash {command!r} started in background as {job.job_id}; poll "
                "with BashOutput to read its output]"
            ),
        )

    outcome = await _jobs.await_job(
        job, timeout=timeout, cancel_token=ctx.cancel_token
    )
    if outcome == "timeout":
        return ToolExecutionResult.text(
            f"[timed out after {timeout:g}s; process group terminated]\n"
            + _jobs.format_job_output(job),
            is_error=True,
            context_note=(
                f"[Bash {command!r} timed out after {timeout:g}s and was "
                "terminated; re-run with a longer timeout only if safe]"
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
