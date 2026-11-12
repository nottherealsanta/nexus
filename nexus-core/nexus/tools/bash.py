"""`bash`, plus the companion tools for background jobs.

A foreground command runs in the workspace with a 120-second default and
600-second maximum timeout. Cancelling the call (an interrupted turn, or a code cell's
timeout) kills the command's process group.

`background: true` is bound by the runtime to its job registry: it starts the
command and returns a job id at once. `bash_output` reads a job's status and
output; `bash_kill` stops one.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import signal
import tempfile
from pathlib import Path
from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, clip

JOB_ID_HINT = "Use bash_output(job_id=…) to read it; a notice arrives when it finishes."
DEFAULT_FOREGROUND_TIMEOUT = 120.0
MAX_FOREGROUND_TIMEOUT = 600.0


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # it exited between the timeout/cancel and the signal


def timeout_value(value: Any, *, foreground: bool = False) -> float | None:
    """Foreground work is bounded; background jobs may run until stopped."""
    if value is None:
        return DEFAULT_FOREGROUND_TIMEOUT if foreground else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ToolError("timeout must be a number of seconds greater than 0")
    if foreground and value > MAX_FOREGROUND_TIMEOUT:
        raise ToolError(f"foreground timeout must be at most {MAX_FOREGROUND_TIMEOUT:g}s; use background=true for longer work")
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
    timeout = timeout_value(args.get("timeout"), foreground=True)
    fd, status_file = tempfile.mkstemp(prefix="nexus-pipestatus-")
    os.close(fd)
    try:
        proc = await asyncio.create_subprocess_exec(
            # Same line as the command, so bash's "line N" in error messages is unchanged.
            "/bin/bash", "-c", PIPESTATUS_TRAP + command,
            cwd=ctx.workspace, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True, env={**os.environ, "NEXUS_PIPESTATUS_FILE": status_file},
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
        if proc.returncode == 0:
            pipestatus = await asyncio.to_thread(Path(status_file).read_text, encoding="utf-8", errors="replace")
            status += hidden_failures(pipestatus, text)
    finally:
        os.unlink(status_file)
    prefix = f"[cwd: {ctx.workspace}]\n" if ctx.cwd_changed else ""
    body = f"[{status}]\n{text}" if text else f"[{status}, no output]"
    return clip(prefix + body)


# Records the exit codes of the command's last pipeline, so `cmd 2>&1 | tail` cannot report a failing
# `cmd` as plain "exit code 0" (280 such test-failure outputs in one week of sessions, 2026-11).
PIPESTATUS_TRAP = "trap 'printf \"%s\" \"${PIPESTATUS[*]}\" > \"$NEXUS_PIPESTATUS_FILE\" 2>/dev/null' EXIT; "
_NOT_FOUND = re.compile(r"^/bin/bash: (?:line \d+: )?([^\s:]+): command not found$", re.MULTILINE)


def hidden_failures(pipestatus: str, output: str) -> str:
    """A note for a successful exit that hides a failure: a failing earlier stage of the last
    pipeline, or a command bash could not find anywhere in the list. 141 (SIGPIPE, from a reader
    such as `head` closing early) is normal and not reported."""
    notes = []
    codes = pipestatus.split()
    if len(codes) > 1 and any(code not in ("0", "141") for code in codes[:-1]):
        notes.append(f"pipeline exit codes {' '.join(codes)}")
    missing = list(dict.fromkeys(_NOT_FOUND.findall(output)))
    if missing:
        notes.append("command not found: " + ", ".join(missing[:5]))
    return "".join(f" · {note}" for note in notes)


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
            "Run a bash command. Every call starts fresh in the workspace directory (a `cd` does not carry over "
            "to the next call), so never `cd` to the workspace itself; `cd sub && …` is fine. stdout and "
            "stderr are combined; commands have no stdin. "
            "If an earlier stage of a pipeline fails (`cmd | tail`), the status line says so. "
            "Do not wrap commands in `timeout` (missing on macOS); use the timeout parameter. "
            "Use it for git, tests, linters and builds, and for "
            "anything the other tools cannot do; use `read`, `grep`, `glob`, `ls`, `edit` and `patch` rather than "
            "cat, sed, rg, find or redirects. Foreground timeout defaults to 120 seconds, maximum 600. "
            "Use background=true for servers or longer work; it returns a job id immediately. "
            "Read it with bash_output and stop it with bash_kill. Do not wait with long sleep commands."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number", "description": "Seconds; foreground default 120, maximum 600. Background jobs have no default timeout."},
                "background": {"type": "boolean", "description": "Return a job id at once instead of waiting."},
            },
            "required": ["command"],
        },
    ),
    run,
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
)

KILL_TOOL = Tool(
    ToolSpec(
        name="bash_kill",
        description="Stop a background bash job started with background=true.",
        parameters={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    ),
    unbound_kill,
)
