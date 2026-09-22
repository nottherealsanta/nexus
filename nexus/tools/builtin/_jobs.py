"""Shell job registry and process-group lifecycle management.

This is the internal service behind the ``shell`` bundle tools (``Bash``,
``BashOutput``, ``KillShell``). It owns every subprocess the harness starts:
creation, concurrent stdout/stderr draining, bounded capture with explicit
truncation, timeouts, process-group termination, and one cleanup seam for
Runtime/ToolManager shutdown.

Injection seam
--------------
:class:`~nexus.tools.spec.ToolContext` exposes an explicit ``job_registry``
field, so :func:`registry_for` resolves, in order:

1. ``ctx.job_registry`` when the tool manager injected one,
2. the :mod:`contextvars` binding installed by :func:`bind_registry`,
3. a process-wide default :class:`JobRegistry`.

Runtime/ToolManager should construct one :class:`JobRegistry` per runtime, pass
it explicitly (or bind it), and ``await registry.aclose()`` on shutdown.

Safety notes
------------
* Jobs are addressed only by opaque registry-owned ids. There is no code path
  that accepts a raw PID from a model.
* ``Bash``'s declared ``permission_key`` is the submitted command string; that
  is policy matching, not sandboxing. Nothing here constrains a command to the
  workspace.
* Subprocess environment is inherited (or overlaid) but is never rendered into
  results, events, or ``repr``.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import os
import secrets
import signal
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ...errors import NexusError, OperationCancelled

__all__ = [
    "DEFAULT_GRACE_S",
    "DEFAULT_KILL_WAIT_S",
    "DEFAULT_MAX_COMPLETED_JOBS",
    "DEFAULT_MAX_RESULT_CHARS",
    "DEFAULT_MAX_RETAINED_BYTES",
    "DEFAULT_OUTPUT_LIMIT",
    "JobRegistry",
    "JobRegistryError",
    "JobStatus",
    "KillResult",
    "OutputBuffer",
    "ShellJob",
    "await_job",
    "bind_registry",
    "close_default_registry",
    "format_job_output",
    "get_default_registry",
    "registry_for",
    "reset_registry",
    "set_default_registry",
    "use_registry",
]

#: Bytes captured per stream before truncation kicks in.
DEFAULT_OUTPUT_LIMIT = 1 << 20
#: Seconds a SIGTERM'd process group gets before SIGKILL.
DEFAULT_GRACE_S = 2.0
#: Extra seconds to await the group after SIGKILL.
DEFAULT_KILL_WAIT_S = 2.0
#: Model-facing text cap for a rendered result.
DEFAULT_MAX_RESULT_CHARS = 200_000
#: Retain at most this many completed jobs before evicting the oldest. Each job
#: can hold up to two output buffers (2 MiB by default), so an unbounded registry
#: would leak that per job for the lifetime of the runtime.
DEFAULT_MAX_COMPLETED_JOBS = 32
#: Combined retained stdout+stderr bytes across completed jobs.
DEFAULT_MAX_RETAINED_BYTES = 64 * 1024 * 1024
#: Pipe read size while draining.
_READ_CHUNK = 65536


class JobRegistryError(NexusError):
    """The registry was used in an unsupported way."""


class JobStatus(StrEnum):
    """Lifecycle of a shell job. ``RUNNING`` is the only non-terminal state."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    KILLED = "killed"


class OutputBuffer:
    """A bounded prefix-capture buffer with explicit truncation accounting.

    The captured bytes are a prefix of everything the stream produced: once the
    limit is reached the buffer keeps draining (so the child never blocks) but
    discards new bytes and records how many were dropped. Offsets index the
    captured prefix, so incremental reads stay monotonic and unambiguous.
    """

    __slots__ = ("_buffer", "_limit", "dropped", "total", "truncated")

    def __init__(self, limit: int = DEFAULT_OUTPUT_LIMIT) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("output limit must be a positive integer")
        self._buffer = bytearray()
        self._limit = limit
        self.total = 0
        self.dropped = 0
        self.truncated = False

    @property
    def limit(self) -> int:
        return self._limit

    def __len__(self) -> int:
        return len(self._buffer)

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total += len(chunk)
        room = self._limit - len(self._buffer)
        if room <= 0:
            self.dropped += len(chunk)
            self.truncated = True
            return
        if len(chunk) > room:
            self._buffer.extend(chunk[:room])
            self.dropped += len(chunk) - room
            self.truncated = True
        else:
            self._buffer.extend(chunk)

    def read(self, offset: int = 0) -> tuple[bytes, int]:
        """Return ``(bytes_from_offset, next_offset)``; offsets clamp safely."""
        size = len(self._buffer)
        start = offset if isinstance(offset, int) and offset > 0 else 0
        start = min(start, size)
        return bytes(self._buffer[start:]), size

    def text(self, offset: int = 0) -> tuple[str, int]:
        data, next_offset = self.read(offset)
        return data.decode("utf-8", "replace"), next_offset

    def has_new(self, offset: int) -> bool:
        return len(self._buffer) > max(offset, 0)

    def snapshot(self) -> bytes:
        return bytes(self._buffer)


@dataclass(frozen=True)
class KillResult:
    """Outcome of a registry-owned kill request (never a raw PID)."""

    outcome: str
    job_id: str
    exit_code: int | None = None
    signal: int | None = None


class ShellJob:
    """One shell subprocess and its captured output.

    The process is started in a new session/process group so the whole group
    (the command and its descendants) can be signalled together. Construction
    is cheap and side-effect free; :meth:`start` does the work.
    """

    def __init__(
        self,
        job_id: str,
        command: str,
        cwd: Path,
        env: dict[str, str] | None,
        *,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
        grace_s: float = DEFAULT_GRACE_S,
        kill_wait_s: float = DEFAULT_KILL_WAIT_S,
        on_finish: Callable[[ShellJob], None] | None = None,
    ) -> None:
        self.job_id = job_id
        self.command = command
        self.cwd = cwd
        self.status = JobStatus.RUNNING
        self.pid: int | None = None
        self.pgid: int | None = None
        self.exit_code: int | None = None
        self.signal: int | None = None
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.stdout = OutputBuffer(output_limit)
        self.stderr = OutputBuffer(output_limit)
        self._env = env
        self._grace_s = grace_s
        self._kill_wait_s = kill_wait_s
        self._on_finish = on_finish
        self._process: asyncio.subprocess.Process | None = None
        self._runner: asyncio.Task[None] | None = None
        self._drain_tasks: list[asyncio.Task[None]] = []
        self._done = asyncio.Event()
        self._condition = asyncio.Condition()
        self._term_lock = asyncio.Lock()
        self._drain_lock = asyncio.Lock()
        self._finish_lock = asyncio.Lock()
        self._final: JobStatus | None = None
        self._terminating = False

    def __repr__(self) -> str:
        return (
            f"ShellJob(job_id={self.job_id!r}, status={self.status.value!r}, "
            f"pid={self.pid!r}, exit_code={self.exit_code!r})"
        )

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def success(self) -> bool:
        return self.status is JobStatus.COMPLETED

    async def start(self) -> None:
        """Spawn the process group and begin draining both pipes."""
        # Always inherit the harness's environment; an explicit overlay adds to
        # (and may override) it. Never fall back to only the overlay, which
        # would drop PATH and every other inherited variable.
        process_env = os.environ.copy()
        if self._env:
            process_env.update(self._env)
        self._process = await asyncio.create_subprocess_shell(
            self.command,
            cwd=str(self.cwd),
            env=process_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.pid = self._process.pid
        self.pgid = self._process.pid
        self._drain_tasks = [
            asyncio.ensure_future(
                self._drain(self._process.stdout, self.stdout)
            ),
            asyncio.ensure_future(
                self._drain(self._process.stderr, self.stderr)
            ),
        ]
        self._runner = asyncio.ensure_future(self._supervise())

    async def _drain(
        self, stream: asyncio.StreamReader | None, buffer: OutputBuffer
    ) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(_READ_CHUNK)
                if not chunk:
                    return
                async with self._condition:
                    buffer.append(chunk)
                    self._condition.notify_all()
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError):
            return

    async def _supervise(self) -> None:
        try:
            code = await self._process.wait()  # type: ignore[union-attr]
        except asyncio.CancelledError:
            self._signal(signal.SIGKILL)
            await asyncio.shield(self._finish_drains())
            await asyncio.shield(self._finalize(JobStatus.KILLED, None))
            raise
        await self._finish_drains()
        if self._terminating:
            await self._done.wait()
            return
        await self._finalize(
            JobStatus.COMPLETED if code == 0 else JobStatus.FAILED, code
        )

    async def _finalize(
        self, status: JobStatus, exit_code: int | None, sig: int | None = None
    ) -> None:
        async with self._finish_lock:
            if self._final is not None:
                return
            self.status = status
            self.exit_code = exit_code
            self.signal = sig
            self.finished_at = time.time()
            self._final = status
        self._done.set()
        async with self._condition:
            self._condition.notify_all()
        if self._on_finish is not None:
            try:
                self._on_finish(self)
            except Exception:  # noqa: BLE001, S110 - retention must never break a job
                pass

    def _signal(self, sig: int) -> None:
        if not hasattr(os, "killpg"):
            return
        for target in (self.pgid, self.pid):
            if target is None:
                continue
            try:
                os.killpg(target, sig)
                return
            except ProcessLookupError:
                continue
            except (PermissionError, OSError):
                return

    async def _await_exit(self, timeout: float) -> int | None:
        assert self._process is not None
        try:
            await asyncio.wait_for(self._process.wait(), timeout)
        except TimeoutError:
            return None
        return self._process.returncode

    async def _finish_drains(self) -> None:
        async with self._drain_lock:
            tasks, self._drain_tasks = self._drain_tasks, []
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), self._grace_s
            )
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._done.wait()
        else:
            await asyncio.wait_for(self._done.wait(), timeout)

    async def wait_for_output(
        self, predicate: Callable[[], bool], timeout: float
    ) -> bool:
        """Wait until ``predicate`` holds, a notify arrives, or ``timeout``."""
        try:
            async with asyncio.timeout(timeout):
                async with self._condition:
                    while not predicate():
                        await self._condition.wait()
            return True
        except TimeoutError:
            return False

    async def terminate(
        self,
        *,
        reason: JobStatus = JobStatus.KILLED,
        grace: float | None = None,
    ) -> bool:
        """SIGTERM the whole group, then SIGKILL; await reaping. Idempotent."""
        if self._process is None:
            await self._finalize(JobStatus.FAILED, None)
            return False
        async with self._term_lock:
            if self.done:
                return False
            self._terminating = True
            self._signal(signal.SIGTERM)
            code = await self._await_exit(
                self._grace_s if grace is None else grace
            )
            if code is None:
                self._signal(signal.SIGKILL)
                code = await self._await_exit(self._kill_wait_s)
            self._signal(signal.SIGKILL)
            await self._finish_drains()
            await self._finalize(reason, code)
            return True

    async def aclose(self) -> None:
        """Await the supervisor and drain tasks; used by registry cleanup."""
        if self._runner is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
        await self._finish_drains()


class JobRegistry:
    """Owns every shell job for one Runtime/ToolManager.

    The registry is the only way to reach a job: ``job``/``kill`` take opaque
    string ids, never PIDs, and a completed job remains readable until the
    registry is closed or the job is explicitly discarded.
    """

    def __init__(
        self,
        *,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
        grace_s: float = DEFAULT_GRACE_S,
        kill_wait_s: float = DEFAULT_KILL_WAIT_S,
        max_completed_jobs: int = DEFAULT_MAX_COMPLETED_JOBS,
        max_retained_bytes: int = DEFAULT_MAX_RETAINED_BYTES,
    ) -> None:
        if (
            isinstance(max_completed_jobs, bool)
            or not isinstance(max_completed_jobs, int)
            or max_completed_jobs < 1
        ):
            raise ValueError("max_completed_jobs must be a positive integer")
        if (
            isinstance(max_retained_bytes, bool)
            or not isinstance(max_retained_bytes, int)
            or max_retained_bytes < 1
        ):
            raise ValueError("max_retained_bytes must be a positive integer")
        self._jobs: dict[str, ShellJob] = {}
        #: Completed job ids in completion order, oldest first (retention/LRU).
        self._completed: deque[str] = deque()
        self.output_limit = output_limit
        self.grace_s = grace_s
        self.kill_wait_s = kill_wait_s
        self.max_completed_jobs = max_completed_jobs
        self.max_retained_bytes = max_retained_bytes
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        return len(self._jobs)

    def jobs(self) -> tuple[ShellJob, ...]:
        return tuple(self._jobs.values())

    def active(self) -> tuple[ShellJob, ...]:
        return tuple(job for job in self._jobs.values() if not job.done)

    def job(self, job_id: object) -> ShellJob | None:
        if not isinstance(job_id, str):
            return None
        return self._jobs.get(job_id)

    async def spawn(
        self,
        command: str,
        *,
        cwd: str | Path,
        env: dict[str, str] | None = None,
        output_limit: int | None = None,
    ) -> ShellJob:
        if self._closed:
            raise JobRegistryError("JobRegistry is closed")
        if not isinstance(command, str) or not command:
            raise JobRegistryError("command must be a non-empty string")
        job_id = self._new_id()
        job = ShellJob(
            job_id,
            command,
            Path(cwd),
            env,
            output_limit=(
                self.output_limit if output_limit is None else output_limit
            ),
            grace_s=self.grace_s,
            kill_wait_s=self.kill_wait_s,
            on_finish=self._note_finished,
        )
        await job.start()
        self._jobs[job_id] = job
        self._evict()
        return job

    # -- retention ---------------------------------------------------------

    def _note_finished(self, job: ShellJob) -> None:
        """Record a completed job for retention/eviction (oldest evicted first)."""
        if job.job_id in self._jobs:
            self._completed.append(job.job_id)
        self._evict()

    def _retained_bytes(self) -> int:
        return sum(
            len(job.stdout) + len(job.stderr) for job in self._jobs.values()
        )

    def _evict(self) -> None:
        """Discard oldest completed jobs past the count/byte retention limits.

        Active jobs are never evicted, so ``active()`` and any lookup of a
        running job are unaffected. Recently completed jobs stay readable until
        the limits are exceeded.
        """
        while self._completed:
            over_count = len(self._completed) > self.max_completed_jobs
            over_bytes = self._retained_bytes() > self.max_retained_bytes
            if not over_count and not over_bytes:
                return
            job_id = self._completed.popleft()
            job = self._jobs.get(job_id)
            if job is None:
                continue
            if not job.done:  # pragma: no cover - defensive; ids are done-on-add
                self._completed.append(job_id)
                return
            del self._jobs[job_id]

    def _new_id(self) -> str:
        while True:
            candidate = f"job_{secrets.token_hex(6)}"
            if candidate not in self._jobs:
                return candidate

    async def kill(self, job_id: object) -> KillResult:
        if not isinstance(job_id, str):
            return KillResult("unknown", "")
        job = self._jobs.get(job_id)
        if job is None:
            return KillResult("unknown", job_id)
        if job.done:
            return KillResult(
                "finished", job_id, job.exit_code, job.signal
            )
        await job.terminate(reason=JobStatus.KILLED)
        return KillResult("killed", job_id, job.exit_code, job.signal)

    def discard(self, job_id: object) -> bool:
        if not isinstance(job_id, str):
            return False
        job = self._jobs.get(job_id)
        if job is None or not job.done:
            return False
        del self._jobs[job_id]
        try:
            self._completed.remove(job_id)
        except ValueError:
            pass
        return True

    async def aclose(self) -> None:
        """Terminate and reap every owned job. Safe to call more than once."""
        self._closed = True
        jobs = list(self._jobs.values())
        await asyncio.gather(
            *(job.terminate(reason=JobStatus.KILLED) for job in jobs),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(job.aclose() for job in jobs), return_exceptions=True
        )
        self._jobs.clear()
        self._completed.clear()


async def await_job(
    job: ShellJob,
    *,
    timeout: float | None = None,
    cancel_token: object | None = None,
) -> str:
    """Await foreground completion, timeout, or cooperative cancellation.

    Returns ``"completed"`` or ``"timeout"``. Cooperative cancellation and
    task cancellation both terminate the process group and then raise, so no
    descendants survive the cancelled path.
    """
    completion = asyncio.ensure_future(job.wait())
    waiters: set[asyncio.Future[object]] = {completion}
    cancel_waiter: asyncio.Future[object] | None = None
    if cancel_token is not None:
        wait = getattr(cancel_token, "wait", None)
        if wait is not None:
            cancel_waiter = asyncio.ensure_future(wait())
            waiters.add(cancel_waiter)
    try:
        done, _ = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if completion in done:
            return "completed"
        if cancel_waiter is not None and cancel_waiter in done:
            await job.terminate(reason=JobStatus.KILLED)
            reason = getattr(cancel_token, "reason", None)
            raise OperationCancelled(reason or "cancelled")
        await job.terminate(reason=JobStatus.TIMED_OUT)
        return "timeout"
    except asyncio.CancelledError:
        await asyncio.shield(job.terminate(reason=JobStatus.KILLED))
        raise
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


def format_job_output(
    job: ShellJob,
    *,
    stdout_offset: int = 0,
    stderr_offset: int = 0,
    show_offsets: bool = False,
    max_chars: int = DEFAULT_MAX_RESULT_CHARS,
) -> str:
    """Render a job's status plus captured output deterministically.

    Output is always presented stdout-then-stderr, so the same buffers render
    identically regardless of arrival interleaving.
    """
    header = [f"job_id: {job.job_id}", f"status: {job.status.value}"]
    if job.done:
        header.append(f"exit_code: {job.exit_code}")
        if job.signal is not None:
            header.append(f"signal: {job.signal}")
    out_bytes, out_next = job.stdout.read(stdout_offset)
    err_bytes, err_next = job.stderr.read(stderr_offset)
    if show_offsets:
        header.append(
            f"stdout_offset: {max(stdout_offset, 0)} -> {out_next}"
        )
        header.append(
            f"stderr_offset: {max(stderr_offset, 0)} -> {err_next}"
        )
    notices: list[str] = []
    if job.stdout.truncated:
        notices.append(
            f"[stdout capture truncated: {job.stdout.dropped} bytes dropped]"
        )
    if job.stderr.truncated:
        notices.append(
            f"[stderr capture truncated: {job.stderr.dropped} bytes dropped]"
        )
    body_parts: list[str] = []
    if out_bytes:
        body_parts.append("stdout:\n" + out_bytes.decode("utf-8", "replace"))
    if err_bytes:
        body_parts.append("stderr:\n" + err_bytes.decode("utf-8", "replace"))
    if not body_parts:
        body_parts.append("(no new output)" if show_offsets else "(no output)")
    body = "\n".join(body_parts)
    reserved = len("\n".join(header)) + sum(len(n) + 1 for n in notices)
    budget = max(max_chars - reserved, 256)
    if len(body) > budget:
        body = body[:budget] + f"\n[output truncated at {budget} chars]"
    parts = header + notices + [body]
    return "\n".join(parts)


def _default_timeout(config: object) -> float:
    v2 = getattr(config, "v2", None)
    tools = getattr(v2, "tools", None) if v2 is not None else None
    value = getattr(tools, "bash_timeout_s", None)
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    ):
        return float(value)
    return 120.0


def resolve_timeout(args: dict[str, object], config: object) -> float:
    """Per-call ``timeout_s`` override, else the configured default."""
    raw = args.get("timeout_s") if isinstance(args, dict) else None
    if raw is None:
        return _default_timeout(config)
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or raw <= 0
    ):
        raise ValueError("timeout_s must be a positive finite number")
    return float(raw)


def resolve_env(args: dict[str, object]) -> dict[str, str] | None:
    """Build the explicit environment overlay, or ``None`` to inherit."""
    raw = args.get("env") if isinstance(args, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError("env must be an object of string values")
    overlay: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("env must be an object of string values")
        if "\x00" in key or "=" in key or not key:
            raise ValueError("env keys must be non-empty names without '='")
        if "\x00" in value:
            raise ValueError("env values must not contain a NUL byte")
        overlay[key] = value
    return overlay


_default_registry: JobRegistry | None = None
_current_registry: ContextVar[JobRegistry | None] = ContextVar(
    "nexus_job_registry", default=None
)


def get_default_registry() -> JobRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = JobRegistry()
    return _default_registry


def set_default_registry(registry: JobRegistry | None) -> None:
    """Install the process-wide registry (``None`` restores lazy creation)."""
    global _default_registry
    _default_registry = registry


async def close_default_registry() -> None:
    """Cleanup seam for Runtime/ToolManager: terminate all default-owned jobs."""
    global _default_registry
    registry, _default_registry = _default_registry, None
    if registry is not None:
        await registry.aclose()


def bind_registry(registry: JobRegistry) -> Token[JobRegistry | None]:
    """Bind a registry for the current context (returns a reset token)."""
    if not isinstance(registry, JobRegistry):
        raise TypeError("registry must be a JobRegistry")
    return _current_registry.set(registry)


def reset_registry(token: Token[JobRegistry | None]) -> None:
    _current_registry.reset(token)


@contextmanager
def use_registry(registry: JobRegistry) -> Iterator[JobRegistry]:
    """Context manager binding ``registry`` for the enclosed block."""
    token = bind_registry(registry)
    try:
        yield registry
    finally:
        reset_registry(token)


def registry_for(ctx: object) -> JobRegistry:
    """Resolve the registry for a ``ToolContext`` (see module docstring)."""
    explicit = getattr(ctx, "job_registry", None)
    if isinstance(explicit, JobRegistry):
        return explicit
    bound = _current_registry.get()
    if bound is not None:
        return bound
    return get_default_registry()
