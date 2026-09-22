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
    bundle="shell",
    mutates=True,
    concurrency="exclusive",
    permission_key=lambda data: str(data.get("job_id", "")),
)


async def run(
    args: dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    """Terminate one job owned by this runtime's registry."""
    job_id = args.get("job_id") if isinstance(args, dict) else None
    if not isinstance(job_id, str) or not job_id:
        return ToolExecutionResult.text(
            "KillShell: 'job_id' must be a non-empty string", is_error=True
        )

    registry = _jobs.registry_for(ctx)
    result = await registry.kill(job_id)
    if result.outcome == "unknown":
        return ToolExecutionResult.text(
            f"KillShell: unknown job_id {job_id!r}; only jobs started by "
            "Bash in this runtime can be terminated",
            is_error=True,
        )
    if result.outcome == "finished":
        return ToolExecutionResult.text(
            f"KillShell: job {job_id} was already finished "
            f"(exit_code={result.exit_code})"
        )
    return ToolExecutionResult.text(
        f"KillShell: job {job_id} terminated "
        f"(exit_code={result.exit_code}, signal={result.signal})"
    )
