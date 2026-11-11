"""Read subscription usage without prompting a model or rotating CLI credentials."""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import os
import signal
import struct
import tempfile
import time
import uuid
from pathlib import Path

import pyte


def oauth_credentials(env: dict[str, str]) -> tuple[str, str] | None:
    home = Path(env.get("HOME", str(Path.home())))
    if "CLAUDE_SECURESTORAGE_CONFIG_DIR" in env:
        root = Path(env["CLAUDE_SECURESTORAGE_CONFIG_DIR"]) if env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] else home / ".claude"
    else:
        root = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else home / ".claude"
    try:
        with (root / ".credentials.json").open("rb") as file:
            raw = file.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            return None
        value = json.loads(raw)
        credential = value.get("claudeAiOauth") if isinstance(value, dict) else None
        if not isinstance(credential, dict):
            return None
        token = credential.get("accessToken")
        expiry = credential.get("expiresAt")
        if not isinstance(token, str) or not token or len(token) > 16384 or any(c.isspace() for c in token):
            return None
        if isinstance(expiry, (int, float)) and expiry / 1000 <= time.time() + 30:
            return None
        plan = credential.get("subscriptionType")
        return token, plan if isinstance(plan, str) else ""
    except (OSError, ValueError):
        return None


async def interactive_usage(executable: str, env: dict[str, str], timeout: float) -> str | None:
    if os.name != "posix":
        return None
    import fcntl
    import pty
    import termios

    master, slave = pty.openpty()
    process = None
    probe_directory = None
    session_id = str(uuid.uuid4())
    env = dict(env)
    for key in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        if env.get(key):
            env[key] = str(Path(env[key]).absolute())
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 200, 160, 0, 0))
        os.set_blocking(master, False)
        screen = pyte.Screen(160, 200)
        stream = pyte.Stream(screen)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        # All trust acceptance is confined to this empty, probe-owned directory.
        probe_directory = tempfile.TemporaryDirectory(prefix="nexus-claude-usage-")
        with contextlib.nullcontext(probe_directory.name) as directory:
            argv = [executable, "--allowed-tools", "", "--strict-mcp-config", "--setting-sources", "",
                    "--settings", '{"remoteControlAtStartup":false,"disableAllHooks":true}',
                    "--session-id", session_id]
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=directory, env={**env, "TERM": "xterm-256color", "DISABLE_AUTOUPDATER": "1"},
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            slave = -1
            started = last_output = time.monotonic()
            ready = started + 2
            sent = confirmed = False
            trust_keys = total = 0
            latest = None
            async with asyncio.timeout(timeout):
                while process.returncode is None:
                    try:
                        chunk = os.read(master, 4096)
                    except BlockingIOError:
                        chunk = b""
                    except OSError:
                        break
                    now = time.monotonic()
                    if chunk:
                        total += len(chunk)
                        if total > 256 * 1024:
                            return None
                        text = decoder.decode(chunk)
                        stream.feed(text)
                        if "\x1b[6n" in text:
                            os.write(master, b"\x1b[1;1R")
                        last_output = now
                    panel = "\n".join(screen.display)
                    if now - last_output >= .2:
                        trust = "Yes, I trust this folder" in panel
                        if not sent and trust and trust_keys < 4:
                            lines = panel.splitlines()
                            yes = next(i for i, line in enumerate(lines) if "Yes, I trust this folder" in line)
                            selected = next((i for i, line in enumerate(lines) if "❯" in line and abs(i - yes) <= 3), None)
                            if selected is not None:
                                os.write(master, b"\r" if selected == yes else b"\x1b[A" if selected > yes else b"\x1b[B")
                                trust_keys += 1
                                ready = now + 2
                                last_output = now
                        elif not sent and not trust and now >= ready:
                            # Never type into login, onboarding or unknown confirmation dialogs.
                            if not any(line.strip() == "❯" or line.strip().startswith('❯\u00a0Try "')
                                       or line.strip().startswith('❯ Try "') for line in panel.splitlines()) or any(word in panel for word in (
                                    "Choose the text style", "Press Enter to continue", "Select login method", "Do you trust")):
                                return None
                            os.write(master, b"/usage")
                            sent = True
                            screen.reset()
                        elif sent:
                            selected_usage = any(line.strip().startswith("❯ /usage ") and
                                                 ("Show plan" in line or "Show session cost, plan usage" in line)
                                                 for line in panel.splitlines())
                            if selected_usage and not confirmed:
                                os.write(master, b"\r")
                                confirmed = True
                                last_output = now
                            elif "Loading" not in panel and "Refreshing" not in panel:
                                from .provider_usage import UsageError, parse_claude
                                try:
                                    parse_claude(panel)
                                    latest = panel
                                    if now - last_output >= 2:
                                        return latest
                                except UsageError:
                                    latest = None
                    await asyncio.sleep(.06)
            return latest
    except (OSError, TimeoutError):
        return None
    finally:
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        os.close(master)
        if slave >= 0:
            os.close(slave)
        if probe_directory is not None:
            config = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else Path(env.get("HOME", str(Path.home()))) / ".claude"
            # Remove only this probe's exact transcript, never another session's history.
            for transcript in (config / "projects").glob(f"*/{session_id}.jsonl"):
                try:
                    transcript.unlink()
                except OSError:
                    pass
            probe_directory.cleanup()
