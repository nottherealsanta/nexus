"""Bounded explicit desktop clipboard/export operations for the native client."""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


OSC52_LIMIT = 100_000  # bytes; terminals commonly drop larger OSC 52 payloads


def copy_via_terminal(text):
    """Ask the user's terminal to set its clipboard (OSC 52); it works over SSH.

    The native client shares the controlling terminal, so the sequence is written
    to it directly. Raises ValueError when there is no terminal or it is too large.
    """
    import base64
    data = text.encode()
    if len(data) > OSC52_LIMIT:
        raise ValueError("Content is too large for the terminal clipboard")
    sequence = "\x1b]52;c;" + base64.b64encode(data).decode() + "\x07"
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(sequence)
            tty.flush()
    except OSError:
        raise ValueError("No terminal available for clipboard copy") from None


async def copy_text_anywhere(text):
    """Copy to the desktop clipboard, falling back to the terminal's; returns "desktop" or "terminal"."""
    try:
        await copy_text(text)
        return "desktop"
    except ValueError as local:
        try:
            copy_via_terminal(text)
        except ValueError as terminal:
            raise ValueError(f"{local}; {terminal}") from None
        return "terminal"


async def copy_text(text):
    if len(text.encode()) > 16 * 1024 * 1024:
        raise ValueError("Clipboard content exceeds 16 MiB")
    if os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"):
        raise ValueError("Local clipboard unavailable over SSH; use session export")
    command = ["pbcopy"] if sys.platform == "darwin" else ["wl-copy"] if shutil.which("wl-copy") else ["xclip", "-selection", "clipboard"] if shutil.which("xclip") else None
    if not command:
        raise ValueError("No local clipboard command available")
    try:
        process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        raise ValueError("No local clipboard command available") from None
    try:
        await asyncio.wait_for(process.communicate(text.encode()), 5)
        if process.returncode:
            raise ValueError("Clipboard copy failed")
    except asyncio.TimeoutError:
        raise ValueError("Clipboard copy timed out") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
