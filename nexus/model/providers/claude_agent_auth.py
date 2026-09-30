"""Bounded official CLI subscription status, never token files (plan section 8)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path

from .opencode import build_child_env


def cli_path(executable: str | None = None) -> str | None:
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec is None or spec.origin is None:
        return None
    if executable:
        return executable
    bundled = Path(spec.origin).parent / "_bundled" / ("claude.exe" if os.name == "nt" else "claude")
    return str(bundled) if bundled.is_file() else None


async def subscription_connected(executable: str | None = None, environ=None) -> bool:
    executable = cli_path(executable)
    if executable is None:
        return False
    process = None
    try:
        async with asyncio.timeout(3):
            process = await asyncio.create_subprocess_exec(executable, "auth", "status", "--json",
                        env=build_child_env(environ), stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL)
            output = bytearray()
            while chunk := await process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > 16_384:
                    return False
            await process.wait()
            value = json.loads(output)
            return (process.returncode == 0 and isinstance(value, dict) and
                    value.get("loggedIn") is True and value.get("authMethod") == "claude.ai")
    except (OSError, ValueError, TimeoutError):
        return False
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
