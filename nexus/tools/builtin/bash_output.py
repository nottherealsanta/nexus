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

_MAX_WAIT_S = 30.0

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
    "maximum": _MAX_WAIT_S,
    "description": (
        "Optional seconds to wait for new output or completion before "
        f"returning. Must be no greater than {_MAX_WAIT_S:g} seconds."
    ),
}

SPEC = ToolSpec(
    name="BashOutput",
    group="bash",
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
    bundle="legacy_shell",
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


async def _read_job_output(
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    tool_name: str = "BashOutput",
    max_wait_s: float | None = None,
) -> ToolExecutionResult:
    """Return buffered output from a registry-owned job.

    ``max_wait_s`` lets the unified ``bash`` action apply the same limit; the
    legacy ``BashOutput`` entry point is bounded by ``_MAX_WAIT_S`` directly.
    """
    job_id = args.get("job_id") if isinstance(args, dict) else None
    if not isinstance(job_id, str) or not job_id:
        return _error(f"{tool_name}: 'job_id' must be a non-empty string")

    registry = _jobs.registry_for(ctx)
    try:
        session_id = _jobs.require_session_id(ctx.session_id)
    except _jobs.JobRegistryError as exc:
        return _error(f"{tool_name}: {exc}")
    job = registry.job(job_id, session_id=session_id)
    if job is None:
        return _error(
            f"{tool_name}: unknown job_id {job_id!r}; only jobs started by "
            "Bash in this session are valid"
        )

    try:
        stdout_offset = _offset(args, "stdout_offset")
        stderr_offset = _offset(args, "stderr_offset")
        wait_s = args.get("wait_s")
    except ValueError as exc:
        return _error(f"{tool_name}: {exc}")

    if ctx.cancel_token is not None:
        ctx.cancel_token.raise_if_cancelled()

    if wait_s is not None:
        if (
            isinstance(wait_s, bool)
            or not isinstance(wait_s, (int, float))
            or (isinstance(wait_s, float) and not math.isfinite(wait_s))
            or wait_s <= 0
        ):
            return _error(f"{tool_name}: wait_s must be a positive finite number")
        wait_limit = max_wait_s if max_wait_s is not None else _MAX_WAIT_S
        if wait_s > wait_limit:
            return _error(
                f"{tool_name}: wait_s must not exceed {wait_limit:g} seconds"
            )
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
            f"[{tool_name} {job_id}: output evicted; call {tool_name} again with "
            "the recorded offsets to re-read it]"
        ),
    )


async def run(
    args: dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    """Legacy entry point for reading buffered output."""
    return await _read_job_output(args, ctx)


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
