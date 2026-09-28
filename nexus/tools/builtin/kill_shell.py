"""``KillShell``: terminate a registry-owned background shell job.

Accepts only opaque job ids returned by ``Bash``. Raw PIDs are never accepted,
and a job owned by a different registry is reported as unknown. The call is
idempotent: terminating an already-finished job is a clear, non-error result.
"""
from __future__ import annotations

from typing import Any

from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from . import _jobs

__all__ = ["SPEC", "run"]

SPEC = ToolSpec(
    name="KillShell",
    group="bash",
    description=(
        "Terminate a running background job started by Bash. The whole "
        "process group is stopped (SIGTERM, then SIGKILL)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "A job_id previously returned by Bash.",
            }
        },
        "required": ["job_id"],
        "additionalProperties": False,
    },
    bundle="legacy_shell",
    mutates=True,
    concurrency="exclusive",
    permission_key=lambda data: str(data.get("job_id", "")),
)


async def _stop_job(
    job_id: object,
    ctx: ToolContext,
    *,
    tool_name: str = "KillShell",
) -> ToolExecutionResult:
    """Terminate one session-owned registry job, idempotently."""
    if not isinstance(job_id, str) or not job_id:
        return ToolExecutionResult.text(
            f"{tool_name}: 'job_id' must be a non-empty string", is_error=True
        )

    registry = _jobs.registry_for(ctx)
    try:
        session_id = _jobs.require_session_id(ctx.session_id)
    except _jobs.JobRegistryError as exc:
        return ToolExecutionResult.text(f"{tool_name}: {exc}", is_error=True)
    result = await registry.kill(job_id, session_id=session_id)
    if result.outcome == "unknown":
        return ToolExecutionResult.text(
            f"{tool_name}: unknown job_id {job_id!r}; only jobs started by "
            "Bash in this session can be terminated",
            is_error=True,
        )
    if result.outcome == "finished":
        return ToolExecutionResult.text(
            f"{tool_name}: job {job_id} was already finished "
            f"(exit_code={result.exit_code})"
        )
    return ToolExecutionResult.text(
        f"{tool_name}: job {job_id} terminated "
        f"(exit_code={result.exit_code}, signal={result.signal})"
    )


async def run(
    args: dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    """Legacy entry point for stopping a registry-owned job."""
    job_id = args.get("job_id") if isinstance(args, dict) else None
    return await _stop_job(job_id, ctx)
