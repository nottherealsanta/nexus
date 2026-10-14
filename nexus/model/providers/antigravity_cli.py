"""Bounded probes of the Antigravity ``agy`` CLI; Nexus never signs in or reads its credentials.

``agy`` owns interactive sign-in and its credential store, and has no ``auth``
subcommand. Nexus runs local probes — ``agy --version`` and ``agy models`` — and the
built-in ``/usage`` command for an active session. The probes are non-model;
``/usage`` is handled by the agent during a turn and is distinct from a model
prompt. Nexus can prove *that the CLI is present*, never *that it is signed in*. :meth:`AntigravityCli.status` is consequently ``False`` and
:meth:`AntigravityCli.status_view` reports ``auth_verified: False`` until a
proof exists that exposes no token.

Every child runs without a shell, with no stdin, in its own process group (the
whole group is killed on timeout, overflow or cancellation), under the OS-only
environment plus ``AGY_CLI_DISABLE_AUTO_UPDATE=true``. Output is size-capped;
stderr is untrusted and only ever surfaces ANSI-stripped, redacted and truncated.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ...util import redact_secrets
from .opencode import build_child_env

EXECUTABLE_NAME = "agy"
LOGIN_INSTRUCTION = "Run `agy` in a terminal and sign in there. Nexus never signs in to Antigravity for you."
_MAX_OUTPUT = 64 * 1024
_MAX_MODELS = 128
_PROBE_TIMEOUT = 10.0
_MAX_STDERR = 300
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_VERSION = re.compile(r"\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.\-]{1,32})?")
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class AntigravityError(RuntimeError):
    """A probe failed; the message is already safe to show (redacted and bounded)."""


@dataclass(frozen=True)
class _Ran:
    returncode: int
    stdout: str
    stderr: str


def clean_text(value: bytes | str, limit: int = _MAX_STDERR) -> str:
    """Untrusted CLI text → ANSI/control-free, secret-redacted, single bounded string."""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    text = _CONTROL.sub("", _ANSI.sub("", text))
    return " ".join(redact_secrets(text).split())[:limit]


def resolve_executable(executable: str | None = None, environ: Mapping[str, str] | None = None) -> str | None:
    """The ``agy`` binary to run, or ``None``.

    Without an override the name ``agy`` is looked up on the child ``PATH``. An
    override must be an absolute path to an executable file with no ``..``
    component or control character; anything else (a relative path, a bare
    command name other than ``agy``) is refused rather than searched for.
    """
    source = os.environ if environ is None else environ
    if executable in (None, ""):
        return shutil.which(EXECUTABLE_NAME, path=source.get("PATH"))
    if not isinstance(executable, str) or len(executable) > 4096 or re.search(r"[\x00-\x1f\x7f]", executable):
        return None
    if executable == EXECUTABLE_NAME:
        return shutil.which(EXECUTABLE_NAME, path=source.get("PATH"))
    path = Path(executable)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def _kill_group(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - POSIX is the tested platform
            process.kill()


async def _drain(stream: asyncio.StreamReader, limit: int) -> bytes:
    """Everything up to ``limit`` bytes; raises when the stream exceeds it."""
    data = bytearray()
    while chunk := await stream.read(4096):
        data.extend(chunk)
        if len(data) > limit:
            raise AntigravityError("agy printed too much output")
    return bytes(data)


async def _run(argv: list[str], environ: Mapping[str, str] | None, *, timeout: float, limit: int = _MAX_OUTPUT) -> _Ran:
    env = build_child_env(environ, env={"AGY_CLI_DISABLE_AUTO_UPDATE": "true"})
    process = None
    try:
        async with asyncio.timeout(timeout):
            process = await asyncio.create_subprocess_exec(
                *argv, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True)
            out, err = await asyncio.gather(_drain(process.stdout, limit), _drain(process.stderr, limit))
            return _Ran(await process.wait(), out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
    except TimeoutError:
        raise AntigravityError("agy did not answer in time") from None
    except OSError:
        raise AntigravityError("could not run agy") from None
    finally:
        if process is not None and process.returncode is None:
            _kill_group(process)
            with contextlib.suppress(Exception):
                await asyncio.shield(process.wait())


def _note(value: str) -> str:
    return clean_text(value, 160)


def parse_usage(text: str) -> dict[str, Any]:
    """Parse verified ``agy -p /usage`` TSV into provider-usage windows.

    Observed agy 1.3.4 prints strict four-column TSV rows:
    group, ``Weekly Limit Remaining`` or ``Five Hour Limit Remaining``, remaining
    percent, ISO reset. Remaining is inverted to used percent. Unexpected rows
    become bounded redacted notes only; no counters are fabricated.
    """
    windows: list[dict[str, Any]] = []
    notes: list[str] = []
    for line in text.splitlines()[:128]:
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            notes.append(f"Unexpected usage row: {_note(line)}")
            continue
        group, label, percent_text, reset_text = [part.strip() for part in parts]
        if label == "Weekly Limit Remaining":
            period, suffix = 604_800, "Weekly"
        elif label == "Five Hour Limit Remaining":
            period, suffix = 18_000, "5-hour"
        else:
            notes.append(f"Unexpected usage label: {_note(label)}")
            continue
        try:
            remaining = float(percent_text[:-1]) if percent_text.endswith("%") else float("nan")
        except ValueError:
            remaining = float("nan")
        if not (0.0 <= remaining <= 100.0):
            notes.append(f"Unexpected usage percent: {_note(percent_text)}")
            continue
        try:
            resets_at = int(datetime.fromisoformat(reset_text.replace("Z", "+00:00")).timestamp())
        except ValueError:
            notes.append(f"Unexpected usage reset: {_note(reset_text)}")
            continue
        windows.append({"label": f"{_note(group) or 'Limit'} · {suffix}", "used_percent": round(100.0 - remaining, 2),
                        "resets_at": resets_at, "reset_text": reset_text, "detail": "", "period_seconds": period})
    if not windows and not notes:
        raise AntigravityError("agy printed no usage limits")
    return {"windows": windows, "notes": notes}


async def _usage_probe(cli: "AntigravityCli") -> str:
    return (await cli._probe("-p", "/usage")).stdout


def parse_models(text: str) -> list[tuple[str, str]]:
    """``(id, label)`` for each line that is exactly one model id; other lines are ignored.

    ``agy models`` was observed (1.3.4) printing one id per line; the id is its
    own label because no separate display name is known.
    """
    models: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in text.splitlines()[:512]:
        token = _CONTROL.sub("", _ANSI.sub("", line)).strip()
        if _MODEL_ID.fullmatch(token) and token not in seen:
            seen.add(token)
            models.append((token, token))
            if len(models) >= _MAX_MODELS:
                break
    return models


class AntigravityCli:
    """Safe, non-interactive view of the local ``agy`` CLI."""

    def __init__(self, executable: str | None = None, environ: Mapping[str, str] | None = None) -> None:
        self._executable, self._environ = executable, environ

    def path(self) -> str | None:
        return resolve_executable(self._executable, self._environ)

    async def _probe(self, *args: str) -> _Ran:
        path = self.path()
        if path is None:
            raise AntigravityError("the agy CLI was not found; install Antigravity CLI and run `agy`")
        ran = await _run([path, *args], self._environ, timeout=_PROBE_TIMEOUT)
        if ran.returncode != 0:
            detail = clean_text(ran.stderr)
            raise AntigravityError(f"agy {args[0]} failed" + (f": {detail}" if detail else ""))
        return ran

    async def version(self) -> str:
        """The version number ``agy --version`` prints, or ``""`` when it has no recognisable one."""
        match = _VERSION.search(clean_text((await self._probe("--version")).stdout, 200))
        return match.group(0) if match else ""

    async def models(self) -> list[tuple[str, str]]:
        """``(id, label)`` pairs from ``agy models``; raises :class:`AntigravityError`."""
        return parse_models((await self._probe("models")).stdout)

    async def usage(self) -> dict[str, Any]:
        """Quota windows from the built-in ``agy -p /usage`` command."""
        return parse_usage(await _usage_probe(self))

    async def status(self) -> bool:
        """Whether sign-in is *proven*. agy exposes no token-free check, so this is always ``False``."""
        return False

    async def status_view(self) -> dict[str, object]:
        """Honest, token-free status: CLI presence and sign-in verification are separate facts."""
        if self.path() is None:
            return {"cli_present": False, "version": "", "auth_verified": False,
                    "message": "agy was not found. Install Antigravity CLI, then run `agy` to sign in."}
        version = ""
        try:
            version = await self.version()
        except AntigravityError:
            pass
        return {"cli_present": True, "version": version, "auth_verified": False,
                "message": "agy is installed; sign-in cannot be verified from Nexus. " + LOGIN_INSTRUCTION}

    async def logout(self) -> None:
        raise RuntimeError("Antigravity's login belongs to the agy CLI; Nexus never signs it out")


__all__ = ["AntigravityCli", "AntigravityError", "LOGIN_INSTRUCTION", "clean_text", "parse_models", "parse_usage", "resolve_executable"]
