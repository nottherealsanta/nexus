"""`bash`, plus the companion tools for background jobs.

A foreground command runs in the workspace with no default time limit: pass
`timeout` to bound it. Cancelling the call (an interrupted turn, or a code cell's
timeout) kills the command's process group.

`background: true` is bound by the runtime to its job registry: it starts the
command and returns a job id at once. `bash_output` reads a job's status and
output; `bash_kill` stops one.
"""
from __future__ import annotations

import asyncio
import math
import os
import signal
from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, clip

JOB_ID_HINT = "Use bash_output(job_id=…) to read it; a notice arrives when it finishes."


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # it exited between the timeout/cancel and the signal


def timeout_value(value: Any) -> float | None:
    """An optional explicit timeout; omitted means run until exit or cancellation."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ToolError("timeout must be a number of seconds greater than 0")
    return float(value)


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command must be a non-empty string")
    background = args.get("background", False)
    if not isinstance(background, bool):
        raise ToolError("background must be a boolean")
    if background:
        raise ToolError("background bash requires the daemon's job registry")
    timeout = timeout_value(args.get("timeout"))
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-c", command,
        cwd=ctx.workspace, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        status = f"exit code {proc.returncode}"
    except TimeoutError:
        _kill(proc)
        out, _ = await proc.communicate()
        status = f"killed after {timeout:g}s timeout"
    except asyncio.CancelledError:
        # A cancelled call must stop its command too; otherwise it keeps changing the workspace.
        _kill(proc)
        raise
    text = out.decode("utf-8", "replace")
    prefix = f"[cwd: {ctx.workspace}]\n" if ctx.cwd_changed else ""
    body = f"[{status}]\n{text}" if text else f"[{status}, no output]"
    return clip(prefix + body)


async def unbound_output(args: dict[str, Any], ctx: ToolContext) -> str:
    raise ToolError("bash_output requires the daemon's job registry")


async def unbound_kill(args: dict[str, Any], ctx: ToolContext) -> str:
    raise ToolError("bash_kill requires the daemon's job registry")


def started(job: Any) -> str:
    return (f"started background job {job.id} (pid {job.process.pid}) in {job.directory}\n{JOB_ID_HINT}")


def _page(args: dict[str, Any]) -> tuple[int, int]:
    offset = args.get("offset", 0)
    limit = args.get("limit", 30_000)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ToolError("offset must be an integer of 0 or more")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30_000:
        raise ToolError("limit must be an integer from 1 to 30000")
    return offset, limit


def report(job: Any, args: dict[str, Any]) -> str:
    offset, limit = _page(args)
    output = job.output
    status = job.status
    header = f"job {job.id} · {status}"
    if job.exit_code is not None:
        header += f" · exit code {job.exit_code}"
    if job.timeout is not None:
        header += f" · timeout {job.timeout:g}s"
    header += f" · {job.json()['duration_s']:.1f}s · {len(output)} chars"
    if job.dropped:
        header += f" · {job.dropped} chars dropped (output limit reached)"
    lines = [header, f"command: {clip(job.command, 500)}", f"directory: {job.directory}"]
    if job.error:
        lines.append(f"error: {job.error}")
    if not output:
        lines.append("(no output yet)" if status == "running" else "(no output)")
        return "\n".join(lines)
    part = output[offset:offset + limit]
    if offset or len(part) < len(output):
        part = f"[showing {offset}–{offset + len(part)} of {len(output)} characters]\n{part}"
        remaining = len(output) - (offset + limit)
        if remaining > 0:
            part += f"\n[… {remaining} more characters; call bash_output with offset={offset + limit} …]"
    lines.append(part)
    return clip("\n".join(lines))


def killed(job: Any) -> str:
    if job.status == "running":  # the process was signalled, but its reader has not finished yet
        return f"stopping job {job.id} (pid {job.process.pid}); its status will become 'killed'"
    return (f"job {job.id} · {job.status}"
            + (f" · exit code {job.exit_code}" if job.exit_code is not None else "")
            + f"\ncommand: {clip(job.command, 500)}")


TOOL = Tool(
    ToolSpec(
        name="bash",
        description=(
            "Run a bash command in the workspace directory. stdout and stderr are combined. No stdin. "
            "There is no default time limit; pass timeout (seconds) to bound it. "
            "background=true returns a job id immediately; read it with bash_output and stop it with bash_kill."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number", "description": "Optional seconds; omitted means no time limit."},
                "background": {"type": "boolean", "description": "Return a job id at once instead of waiting."},
            },
            "required": ["command"],
        },
    ),
    run,
    guidance="Run a shell command in the workspace (stdout and stderr combined, no stdin). There is no default time "
             "limit; pass a timeout to bound it, or use background=true for a long command and read it with "
             "bash_output. Use it to inspect the project (ls, rg, git) and to check your work with its tests, "
             "linters and builds.",
)

OUTPUT_TOOL = Tool(
    ToolSpec(
        name="bash_output",
        description=(
            "Read a background bash job: status, exit code, duration and output. "
            "Use offset and limit (at most 30000 characters) to page through long output."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "offset": {"type": "integer", "description": "Start character (default 0)."},
                "limit": {"type": "integer", "description": "Characters to return (default and maximum 30000)."},
            },
            "required": ["job_id"],
        },
    ),
    unbound_output,
    guidance="Read a background bash job started with bash background=true: status, exit code and output. "
             "Page with offset and limit.",
)

KILL_TOOL = Tool(
    ToolSpec(
        name="bash_kill",
        description="Stop a background bash job started with background=true.",
        parameters={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    ),
    unbound_kill,
    guidance="Stop a background bash job started with bash background=true.",
)
