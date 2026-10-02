"""Bounded explicit desktop clipboard/export operations for the native client."""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


async def copy_text(text):
    if len(text.encode()) > 16 * 1024 * 1024:
        raise ValueError("Clipboard content exceeds 16 MiB")
    if os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"):
        raise ValueError("Local clipboard unavailable over SSH; use session export")
    command = ["pbcopy"] if sys.platform == "darwin" else ["wl-copy"] if shutil.which("wl-copy") else ["xclip", "-selection", "clipboard"] if shutil.which("xclip") else None
    if not command:
        raise ValueError("No local clipboard command available")
    process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.communicate(text.encode()), 5)
        if process.returncode:
            raise ValueError("Clipboard copy failed")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
