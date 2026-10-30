"""Claude sign-in through the user's own Claude Code.

Nexus does not ship Claude Code. The Agent SDK drives the `claude` the user installed, and when there is none the
Claude provider fails with `NOT_INSTALLED`; nothing else is affected. The CLI owns the credential store and Nexus never
reads token files: it asks `claude auth status --json` whether it is signed in with a claude.ai subscription, and signs
in by running `claude auth login`, handing the printed URL to the client.

`cli_path()` finds `$NEXUS10_CLAUDE_CLI`, else `claude` on `PATH`, else the places Claude Code installs itself.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .errors import AuthError

STATUS_TTL_S = 30.0
LOGIN_TIMEOUT_S = 900.0
_URL = re.compile(r"https://[^\s\x1b]{1,4096}")
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")
_cache: dict[str, tuple[float, bool]] = {}


NOT_INSTALLED = ("Claude Code is not installed, so the Claude provider cannot run. Install Claude Code, sign in with "
                 "`claude auth login`, and try again (a different `claude` can be named in $NEXUS10_CLAUDE_CLI).")


def cli_path() -> str | None:
    override = os.environ.get("NEXUS10_CLAUDE_CLI")
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    for path in (Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude", Path("/usr/local/bin/claude"), Path("/opt/homebrew/bin/claude")):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def child_env() -> dict[str, str]:
    keep = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "CLAUDE_CONFIG_DIR",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE")
    return {k: os.environ[k] for k in keep if k in os.environ}


def status() -> dict | None:
    """The CLI's `auth status --json` object, or None when it cannot be read."""
    cli = cli_path()
    if cli is None:
        return None
    try:
        ran = subprocess.run([cli, "auth", "status", "--json"], env=child_env(), stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=5, check=False)
        value = json.loads(ran.stdout[:16384]) if ran.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return value if isinstance(value, dict) else None


class ClaudeAuth:
    methods = ("cli",)

    def logged_in(self) -> bool:
        cached = _cache.get("status")
        if cached and time.monotonic() - cached[0] < STATUS_TTL_S:
            return cached[1]
        value = status()
        ok = bool(value) and value.get("loggedIn") is True and value.get("authMethod") == "claude.ai"
        _cache["status"] = (time.monotonic(), ok)
        return ok

    def logout(self) -> None:
        cli = cli_path()
        _cache.clear()
        if cli is None:
            return
        try:
            subprocess.run([cli, "auth", "logout"], env=child_env(), stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass

    async def headers(self) -> dict[str, str]:
        return {}

    async def login(self, method: str = "cli", notify: Callable[[str], None] = print) -> None:
        cli = cli_path()
        if cli is None:
            raise AuthError("claude: " + NOT_INSTALLED)
        process = await asyncio.create_subprocess_exec(
            cli, "auth", "login", "--claudeai", env=child_env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=os.name == "posix")
        told = False
        try:
            async with asyncio.timeout(LOGIN_TIMEOUT_S):
                while chunk := await process.stdout.read(4096):
                    found = _URL.search(_ANSI.sub("", chunk.decode("utf-8", "replace")))
                    if found and not told:
                        told = True
                        notify(f"Open {found.group(0)} to sign in to Claude.")
                code = await process.wait()
        except TimeoutError:
            raise AuthError("claude: sign-in timed out") from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        _cache.clear()
        if code != 0 or not self.logged_in():
            raise AuthError("claude: sign-in did not complete")
