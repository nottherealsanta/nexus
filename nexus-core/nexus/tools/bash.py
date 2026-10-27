"""`bash`: run a command in the workspace with a timeout; output is bounded."""
from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, clip

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command must be a non-empty string")
    timeout = min(int(args.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
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
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = await proc.communicate()
        status = f"killed after {timeout}s timeout"
    text = out.decode("utf-8", "replace")
    return clip(f"[{status}]\n{text}" if text else f"[{status}, no output]")


TOOL = Tool(
    ToolSpec(
        name="bash",
        description="Run a bash command in the workspace directory. stdout and stderr are combined. No stdin.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": f"Seconds. Default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}."},
            },
            "required": ["command"],
        },
    ),
    run,
)
