"""Bounded official CLI subscription status, usage and sign-in, never token files (plan section 8).

Every helper runs the official ``claude`` CLI (the SDK's bundled copy unless an
``executable`` is configured) with the OS-only child environment. The CLI owns
the credential store; Nexus reads only what the CLI prints: ``auth status
--json`` for the login, ``-p /usage`` for plan limits (a local command that
makes no model request), and the sign-in URL printed by ``auth login``.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from .opencode import build_child_env

_MAX_OUTPUT = 16_384
_MAX_USAGE_OUTPUT = 256 * 1024
_USAGE_TIMEOUT = 30.0
_LOGIN_TIMEOUT = 900.0
#: ``Current session: 59% used · resets Oct 1 at 12:19pm (Asia/Calcutta)``
_USAGE_LINE = re.compile(r"^\s*(?P<label>[A-Za-z][^:\n]{0,79}):\s*(?P<used>\d{1,3}(?:\.\d+)?)%\s+used(?:\s*·\s*(?P<reset>[^\n]{1,120}))?\s*$")
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")
_URL = re.compile(r"https://[^\s\x1b]{1,4096}")


def cli_path(executable: str | None = None) -> str | None:
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec is None or spec.origin is None:
        return None
    if executable:
        return executable
    bundled = Path(spec.origin).parent / "_bundled" / ("claude.exe" if os.name == "nt" else "claude")
    return str(bundled) if bundled.is_file() else None


async def _run(argv: list[str], environ, *, timeout: float, limit: int, cwd: str | None = None) -> tuple[int, bytes] | None:
    """Run the CLI with no stdin; ``None`` on timeout, OS error or oversize output."""
    process = None
    try:
        async with asyncio.timeout(timeout):
            process = await asyncio.create_subprocess_exec(
                *argv, env=build_child_env(environ), cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            output = bytearray()
            while chunk := await process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > limit:
                    return None
            return await process.wait(), bytes(output)
    except (OSError, TimeoutError):
        return None
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def auth_status(executable: str | None = None, environ=None) -> dict | None:
    """The CLI's ``auth status --json`` object, or ``None``."""
    executable = cli_path(executable)
    if executable is None:
        return None
    ran = await _run([executable, "auth", "status", "--json"], environ, timeout=3, limit=_MAX_OUTPUT)
    if ran is None or ran[0] != 0:
        return None
    try:
        value = json.loads(ran[1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def subscription_connected(executable: str | None = None, environ=None) -> bool:
    value = await auth_status(executable, environ)
    return bool(value) and value.get("loggedIn") is True and value.get("authMethod") == "claude.ai"


def parse_usage(text: str) -> list[dict]:
    """``Label: N% used · resets …`` lines from ``/usage``, in printed order."""
    rows: list[dict] = []
    for line in text.splitlines()[:200]:
        match = _USAGE_LINE.match(line)
        if match is None:
            continue
        used = min(float(match["used"]), 100.0)
        reset = (match["reset"] or "").strip()
        rows.append({"label": match["label"].strip(), "used_percent": used,
                     "reset_text": reset.removeprefix("resets ").strip() if reset.startswith("resets") else reset})
        if len(rows) >= 16:
            break
    return rows


async def usage(executable: str | None = None, environ=None, cwd: str | None = None) -> str:
    """The text ``/usage`` prints; raises ``RuntimeError`` with a safe message."""
    executable = cli_path(executable)
    if executable is None:
        raise RuntimeError("Claude Agent SDK is not installed")
    argv = [executable, "-p", "/usage", "--output-format", "json", "--setting-sources", "",
            "--no-session-persistence", "--strict-mcp-config"]
    ran = await _run(argv, environ, timeout=_USAGE_TIMEOUT, limit=_MAX_USAGE_OUTPUT, cwd=cwd)
    if ran is None:
        raise RuntimeError("the Claude CLI did not answer /usage in time")
    try:
        value = json.loads(ran[1])
    except ValueError:
        raise RuntimeError("the Claude CLI returned unreadable /usage output") from None
    text = value.get("result") if isinstance(value, dict) else None
    if ran[0] != 0 or not isinstance(text, str) or value.get("is_error"):
        raise RuntimeError("the Claude CLI could not read usage; run claude auth login")
    return text[:_MAX_USAGE_OUTPUT]


def login_url(text: str) -> str:
    """The first sign-in URL in the CLI's output, with terminal escapes removed."""
    match = _URL.search(_ANSI.sub("", text))
    return match.group(0) if match else ""


async def login(
    executable: str | None, environ, *, on_url: Callable[[str], None],
    code: Callable[[], Awaitable[str]], cancel: asyncio.Event,
) -> None:
    """Run ``claude auth login --claudeai`` headless.

    ``BROWSER`` is set to a no-op so the daemon never opens a browser; the CLI
    then prints a URL whose page shows a one-time code. ``on_url`` receives the
    URL, ``code()`` waits for the code a client pastes, and the code is written
    to the CLI's stdin. Raises ``RuntimeError`` with a safe message on failure.
    """
    executable = cli_path(executable)
    if executable is None:
        raise RuntimeError("Claude Agent SDK is not installed")
    env = build_child_env(environ, env={"BROWSER": "true"})
    process = await asyncio.create_subprocess_exec(
        executable, "auth", "login", "--claudeai", env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output = bytearray()

    async def read() -> None:
        sent = False
        while chunk := await process.stdout.read(4096):
            if len(output) < _MAX_USAGE_OUTPUT:
                output.extend(chunk)
            if not sent and (url := login_url(output.decode("utf-8", "replace"))):
                sent = True
                on_url(url)

    async def answer() -> None:
        value = (await code()).strip()
        process.stdin.write(value.encode() + b"\n")
        await process.stdin.drain()

    reader = asyncio.create_task(read())
    writer = asyncio.create_task(answer())
    stopper = asyncio.create_task(cancel.wait())
    try:
        async with asyncio.timeout(_LOGIN_TIMEOUT):
            waiter = asyncio.create_task(process.wait())
            done, _ = await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if stopper in done:
                raise asyncio.CancelledError
        await reader
        if process.returncode != 0:
            raise RuntimeError("Claude sign-in did not complete; check the code and try again")
    finally:
        for task in (reader, writer, stopper):
            task.cancel()
        if process.returncode is None:
            process.kill()
            await process.wait()


class ClaudeCliAuth:
    """The provider-auth manager shape (``status``/``logout``) over the official CLI."""

    def __init__(self, *, executable: str | None = None, environ: Mapping[str, str] | None = None) -> None:
        self._executable, self._environ = executable, environ

    async def status(self) -> bool:
        return await subscription_connected(self._executable, self._environ)

    async def plan(self) -> str:
        """The subscription type the CLI reports (``max``, ``pro``), or ``""``."""
        value = (await auth_status(self._executable, self._environ) or {}).get("subscriptionType")
        return value[:40] if isinstance(value, str) else ""

    async def usage(self, cwd: str | None = None) -> str:
        return await usage(self._executable, self._environ, cwd)

    async def browser_login(self, *, on_url: Callable[[str], None], code: Callable[[], Awaitable[str]], cancel: asyncio.Event) -> None:
        await login(self._executable, self._environ, on_url=on_url, code=code, cancel=cancel)

    async def logout(self) -> None:
        # The CLI login is shared with Claude Code; Nexus never signs it out.
        raise RuntimeError("Claude's login is shared with Claude Code; run `claude auth logout` in a terminal to sign out")


__all__ = ["ClaudeCliAuth", "auth_status", "cli_path", "login", "login_url", "parse_usage", "subscription_connected", "usage"]
