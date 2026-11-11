"""Shared, runtime-independent ZMX transport helpers, ported from nexus01 workers."""
from __future__ import annotations

import asyncio
import os
import re
import signal
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

MAX_PROMPT_BYTES = 64 * 1024
MAX_DIAGNOSTIC_BYTES = 16 * 1024
MAX_DIAGNOSTIC_CHARS = 1500
MAX_RESULT_CHARS = 64 * 1024
PROBE_TIMEOUT_SECONDS = 5.0
TERM_GRACE_SECONDS = 2.0
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_SECRET = re.compile(
    r"(?i)(\bauthorization\s*:\s*bearer\s+|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token)[\"']?\s*[=:]\s*[\"']?)[^\s,;\"']+")
_TOKEN = re.compile(r"\b(?:sk|gh[pousr]|xox[bap])[-_][A-Za-z0-9_-]{16,}")
_CURSOR_FORWARD = re.compile(r"\x1b\[(\d*)C")


class WorkerError(Exception):
    """A transport or launch failure, separate from Nexus provider failures."""


@dataclass(frozen=True)
class WorkerPolicy:
    permission_mode: Literal["auto", "accept-edits", "plan"] = "auto"

    def __post_init__(self) -> None:
        if self.permission_mode not in ("auto", "accept-edits", "plan"):
            raise ValueError("ZMX permission mode must be auto, accept-edits or plan; bypass is not supported")

    def authority(self, workspace: Path, *, vendor: str) -> str:
        return (f"ZMX · {vendor} ran its own tools in {workspace} (--mode {self.permission_mode}); "
                "the CLI's settings, MCP servers and plugins are inherited. Nexus permissions, tool restrictions "
                "and step limits do not apply; no sandbox is requested. The CLI adds context Nexus cannot see.")


@dataclass(frozen=True)
class WorkerEvent:
    """Internal transport event, not a persisted event or a second UI protocol.

    A wire event precedes every interpretation, including malformed/unknown lines.
    The future ExternalAgent adapter must map these into ordinary LoopEvents.
    """

    kind: str
    data: dict = field(default_factory=dict)
    raw: bytes | None = None


@dataclass(frozen=True)
class WorkerResult:
    status: Literal["completed", "failed", "needs_attention"]
    text: str
    authority: str
    session_id: str | None = None
    usage: dict[str, int] | None = None
    error: str | None = None
    diagnostics: str = ""
    notes: tuple[str, ...] = ()
    denied_actions: tuple[str, ...] = ()
    transcript_path: str | None = None
    policy_fingerprint: str | None = None


def child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    keep = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE")
    return {key: source[key] for key in keep if key in source}


def diagnostic_text(raw: bytes) -> str:
    def cursor_space(match: re.Match) -> str:
        digits = match[1]
        return " " * (200 if len(digits) > 3 else min(int(digits or "1"), 200))

    text = _CURSOR_FORWARD.sub(cursor_space, raw.decode("utf-8", "replace"))
    text = _ANSI.sub("", text).replace("\r", "\n")
    text = "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) <= 159))
    return _TOKEN.sub("[redacted]", _SECRET.sub(r"\1[redacted]", text)).strip()


def clip(text: str, limit: int) -> tuple[str, str | None]:
    if len(text) <= limit:
        return text, None
    return text[:limit], f"clipped {len(text) - limit} characters"


async def wait_for_exit(process: asyncio.subprocess.Process) -> int:
    # Process.wait() can wait on inherited pipes after the leader has been reaped.
    while process.returncode is None:
        await asyncio.sleep(.01)
    return process.returncode


async def protected_cleanup(action: Awaitable[None]) -> None:
    task = asyncio.ensure_future(action)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    await task
    if cancelled:
        raise asyncio.CancelledError


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """Stop an owned live group and close owned pipes, never signal a reaped PID."""
    if process.returncode is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(wait_for_exit(process), TERM_GRACE_SECONDS)
        except TimeoutError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await asyncio.wait_for(wait_for_exit(process), TERM_GRACE_SECONDS)
    # asyncio exposes no public close-pipes method. Only close after reaping so
    # transport.close() cannot signal a reused PID; detached descendants may remain.
    process._transport.close()
    await process.wait()


async def bounded_lines(stream: asyncio.StreamReader, *, line_limit: int, total_limit: int) -> AsyncIterator[bytes]:
    """Fail rather than dropping a record and later claiming a complete answer."""
    pending = bytearray()
    total = 0
    while chunk := await stream.read(65536):
        total += len(chunk)
        if total > total_limit:
            raise WorkerError("stdout limit reached; later activity was not recorded")
        pending.extend(chunk)
        while (cut := pending.find(b"\n")) >= 0:
            if cut > line_limit:
                raise WorkerError("stream line limit reached; oversized record was not interpreted")
            line = bytes(pending[:cut])
            del pending[:cut + 1]
            if line.strip():
                yield line
        if len(pending) > line_limit:
            raise WorkerError("stream line limit reached; oversized record was not interpreted")
    if pending.strip():
        yield bytes(pending)


async def probe(executable: str, flag: str, *, env: dict[str, str], cwd: Path) -> str:
    process = await asyncio.create_subprocess_exec(
        executable, flag, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)

    async def collect() -> str:
        output = bytearray()
        while chunk := await process.stdout.read(4096):
            output.extend(chunk)
            if len(output) > MAX_PROMPT_BYTES:
                raise WorkerError(f"CLI {flag} probe output exceeds limit")
        if await process.wait() != 0:
            raise WorkerError(f"CLI {flag} probe failed")
        return diagnostic_text(bytes(output))

    try:
        return await asyncio.wait_for(collect(), PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        raise WorkerError(f"CLI {flag} probe timed out") from None
    finally:
        await protected_cleanup(stop_process(process))
