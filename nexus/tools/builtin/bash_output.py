"""``BashOutput``: read buffered output and status for a registry-owned job.

Only opaque job ids returned by ``Bash`` are accepted. The tool cannot be used
to inspect an arbitrary process or PID. Offsets are indexes into the job's
captured stdout/stderr, so callers can read the full buffer or only what has
arrived since a previous call.
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from . import _jobs

__all__ = ["SPEC", "run"]

_JOB_ID = {
    "type": "string",
    "description": "A job_id previously returned by Bash.",
}
_STDOUT_OFFSET = {
    "type": "integer",
    "description": (
        "Byte offset into captured stdout. Use the stdout_offset returned by "
        "the previous call to read only new output; omit for the full buffer."
    ),
}
_STDERR_OFFSET = {
    "type": "integer",
    "description": (
        "Byte offset into captured stderr. Use the stderr_offset returned by "
        "the previous call to read only new output; omit for the full buffer."
    ),
}
_WAIT = {
    "type": "number",
    "description": (
        "Optional seconds to wait for new output or completion before "
        "returning. Bounded and non-blocking for the harness."
    ),
}

SPEC = ToolSpec(
    name="BashOutput",
    description=(
        "Read stdout, stderr, and status for a background shell job started "
        "by Bash. Pass the returned offsets back to poll incrementally."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "job_id": _JOB_ID,
            "stdout_offset": _STDOUT_OFFSET,
            "stderr_offset": _STDERR_OFFSET,
            "wait_s": _WAIT,
        },
        "required": ["job_id"],
        "additionalProperties": False,
    },
    bundle="shell",
    mutates=False,
    concurrency="parallel",
    permission_key=lambda data: str(data.get("job_id", "")),
)


def _error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult.text(message, is_error=True)


def _offset(args: dict[str, Any], key: str) -> int:
    value = args.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


async def run(
    args: dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    """Return buffered output from a registry-owned job."""
    job_id = args.get("job_id") if isinstance(args, dict) else None
    if not isinstance(job_id, str) or not job_id:
        return _error("BashOutput: 'job_id' must be a non-empty string")

    registry = _jobs.registry_for(ctx)
    try:
        session_id = _jobs.require_session_id(ctx.session_id)
    except _jobs.JobRegistryError as exc:
        return _error(f"BashOutput: {exc}")
    job = registry.job(job_id, session_id=session_id)
    if job is None:
        return _error(
            f"BashOutput: unknown job_id {job_id!r}; only jobs started by "
            "Bash in this session are valid"
        )

    try:
        stdout_offset = _offset(args, "stdout_offset")
        stderr_offset = _offset(args, "stderr_offset")
        wait_s = args.get("wait_s")
    except ValueError as exc:
        return _error(f"BashOutput: {exc}")

    if ctx.cancel_token is not None:
        ctx.cancel_token.raise_if_cancelled()

    if wait_s is not None:
        if (
            isinstance(wait_s, bool)
            or not isinstance(wait_s, (int, float))
            or not math.isfinite(wait_s)
            or wait_s <= 0
        ):
            return _error("BashOutput: wait_s must be a positive number")
        await _wait_for_output(
            job, stdout_offset, stderr_offset, float(wait_s), ctx
        )

    return ToolExecutionResult.text(
        _jobs.format_job_output(
            job,
            stdout_offset=stdout_offset,
            stderr_offset=stderr_offset,
            show_offsets=True,
        ),
        context_note=(
            f"[BashOutput {job_id}: output evicted; call BashOutput again with "
            "the recorded offsets to re-read it]"
        ),
    )


async def _wait_for_output(
    job: _jobs.ShellJob,
    stdout_offset: int,
    stderr_offset: int,
    wait_s: float,
    ctx: ToolContext,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s

    def ready() -> bool:
        return (
            job.done
            or job.stdout.has_new(stdout_offset)
            or job.stderr.has_new(stderr_offset)
        )

    while not ready():
        if ctx.cancel_token is not None and ctx.cancel_token.cancelled:
            ctx.cancel_token.raise_if_cancelled()
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        await job.wait_for_output(ready, min(remaining, 0.2))
