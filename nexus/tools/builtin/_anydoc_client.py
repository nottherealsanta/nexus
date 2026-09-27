"""Bounded subprocess client for the private AnyDoc conversion worker."""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import sys
from pathlib import Path

from ...errors import OperationCancelled

MAX_MARKDOWN_BYTES = 4 * 1024 * 1024
MAX_STDOUT_BYTES = len(b"NEXUS-ANYDOC-v1\n") + 4 + MAX_MARKDOWN_BYTES
MAX_STDERR_BYTES = 4096
WORKER_TIMEOUT_SECONDS = 20.0
SUCCESS_MAGIC = b"NEXUS-ANYDOC-v1\n"
SUCCESS_HEADER_BYTES = len(SUCCESS_MAGIC) + 4


class AnyDocError(Exception):
    """A safe, content-free worker failure suitable for display to the caller."""


_ERROR_MESSAGES = {
    "ANYDOC_UNAVAILABLE": "Firecrawl AnyDoc is unavailable; install nexus-harness[documents].",
    "UNSUPPORTED_FORMAT": "document format is unsupported",
    "BAD_INPUT": "document input is invalid",
    "INPUT_TOO_LARGE": "document exceeds the conversion size limit",
    "OCR_REQUIRED": "document requires OCR; hosted OCR is disabled",
    "OUTPUT_TOO_LARGE": "converted Markdown exceeds the output limit",
    "CONVERSION_FAILED": "document conversion failed",
    "BAD_PROTOCOL": "document worker protocol error",
    "WORKER_FAILED": "document worker failed",
}
_ERROR_LINE = re.compile(rb"ERROR\[([A-Z_]{1,40})\]:[^\r\n]*\Z")


def _worker_environment() -> dict[str, str]:
    """Return only interpreter/process settings needed by the worker."""
    env = {
        "PATH": os.defpath,
        "PYTHONSAFEPATH": "1",
    }
    if os.name == "nt" and (system_root := os.environ.get("SystemRoot")):
        env["SystemRoot"] = system_root
    return env


async def _spawn_worker(extension: str) -> asyncio.subprocess.Process:
    worker_path = Path(__file__).resolve().with_name("_anydoc_worker.py")
    return await asyncio.create_subprocess_exec(
        str(Path(sys.executable).absolute()),
        "-I",
        str(worker_path),
        "--protocol",
        "v1",
        "--extension",
        extension,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_worker_environment(),
        cwd=Path(sys.executable).resolve().anchor,
        start_new_session=True,
    )


async def _kill_worker(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    pid = proc.pid
    if pid is not None and hasattr(os, "killpg"):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGKILL)
    else:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()


async def _write_stdin(proc: asyncio.subprocess.Process, payload: bytes) -> None:
    if proc.stdin is None:
        raise AnyDocError("document worker input pipe is unavailable")
    proc.stdin.write(payload)
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        await proc.stdin.drain()
    proc.stdin.close()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        await proc.stdin.wait_closed()


async def _read_bounded(reader: asyncio.StreamReader | None, limit: int, label: str) -> bytes:
    if reader is None:
        raise AnyDocError(f"document worker {label} pipe is unavailable")
    collected = bytearray()
    while chunk := await reader.read(min(64 * 1024, limit - len(collected) + 1)):
        if len(collected) + len(chunk) > limit:
            if label == "output":
                raise AnyDocError("document converter protocol error (output limit exceeded)")
            raise AnyDocError(f"document worker exceeded the {label} limit")
        collected.extend(chunk)
    return bytes(collected)


async def _communicate_bounded(
    proc: asyncio.subprocess.Process, payload: bytes
) -> tuple[bytes, bytes]:
    # Small process doubles used by callers/tests may expose only communicate().
    # The real subprocess path always has the three pipes and is streamed below.
    if not all(hasattr(proc, name) for name in ("stdin", "stdout", "stderr", "wait")):
        stdout, stderr = await proc.communicate(payload)
        if len(stdout) > MAX_STDOUT_BYTES:
            raise AnyDocError("document worker exceeded the output limit")
        if len(stderr) > MAX_STDERR_BYTES:
            raise AnyDocError("document worker exceeded the diagnostic limit")
        return stdout, stderr
    tasks = (
        asyncio.create_task(_write_stdin(proc, payload)),
        asyncio.create_task(_read_bounded(proc.stdout, MAX_STDOUT_BYTES, "output")),
        asyncio.create_task(_read_bounded(proc.stderr, MAX_STDERR_BYTES, "diagnostic")),
    )
    try:
        _, stdout, stderr = await asyncio.gather(*tasks)
        await proc.wait()
        return stdout, stderr
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _error_message(stderr: bytes) -> str:
    lines = stderr.splitlines()
    final_line = next((line for line in reversed(lines) if line), b"")
    match = _ERROR_LINE.fullmatch(final_line)
    code = match.group(1).decode("ascii") if match else ""
    return _ERROR_MESSAGES.get(code, "document conversion failed")


def _decode_success_frame(stdout: bytes) -> bytes:
    if len(stdout) < SUCCESS_HEADER_BYTES or not stdout.startswith(SUCCESS_MAGIC):
        raise AnyDocError("document converter protocol error")
    size = int.from_bytes(stdout[len(SUCCESS_MAGIC) : SUCCESS_HEADER_BYTES], "big")
    if size > MAX_MARKDOWN_BYTES:
        raise AnyDocError("document converter protocol error")
    payload = stdout[SUCCESS_HEADER_BYTES:]
    if len(payload) != size:
        raise AnyDocError("document converter protocol error")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AnyDocError("document worker returned non-UTF-8 Markdown") from exc
    return payload


async def convert_document(
    payload: bytes,
    extension: str,
    cancel_token: object | None,
) -> bytes:
    """Convert bytes with a fixed deadline and terminate the worker on abort."""
    proc: asyncio.subprocess.Process | None = None
    communication: asyncio.Future[tuple[bytes, bytes]] | None = None
    cancel_task: asyncio.Future[object] | None = None
    try:
        try:
            proc = await _spawn_worker(extension)
        except OSError as exc:
            raise AnyDocError("document worker could not be started") from exc

        # communicate() is intentional: the worker reads stdin to EOF and this
        # drains both output pipes without a pipe-buffer deadlock.
        communication = asyncio.ensure_future(_communicate_bounded(proc, payload))
        waiters: set[asyncio.Future[object]] = {communication}  # type: ignore[arg-type]
        if cancel_token is not None:
            wait = getattr(cancel_token, "wait", None)
            if callable(wait):
                cancel_task = asyncio.ensure_future(wait())
                waiters.add(cancel_task)
        done, _ = await asyncio.wait(
            waiters,
            timeout=WORKER_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if communication not in done:
            if cancel_task is not None and cancel_task in done:
                if cancel_token is not None:
                    raise_if_cancelled = getattr(cancel_token, "raise_if_cancelled", None)
                    if callable(raise_if_cancelled):
                        raise_if_cancelled()
                raise OperationCancelled("cancelled")
            raise TimeoutError

        stdout, stderr = communication.result()
        if proc.returncode != 0:
            raise AnyDocError(_error_message(stderr))
        return _decode_success_frame(stdout)
    except TimeoutError as exc:
        raise AnyDocError("document conversion timed out") from exc
    except OSError as exc:
        raise AnyDocError("document worker failed") from exc
    finally:
        if cancel_task is not None:
            if not cancel_task.done():
                cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cancel_task
        if proc is not None and proc.returncode is None:
            await _kill_worker(proc)
        if communication is not None:
            # On timeout, token cancellation, or caller cancellation, killing
            # the process group closes the pipes; finish communicate() to reap
            # the process and collect its bounded remaining output.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(communication)
