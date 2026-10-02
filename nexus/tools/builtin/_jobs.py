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

Session scoping
---------------
One runtime may run several sessions concurrently, so a single flat job map
would let session A's ``BashOutput``/``KillShell`` reach session B's process.
The registry is therefore **partitioned by ``session_id``**: :meth:`spawn`
places a job in its session's partition and every lookup (``job``/``kill``/…) is
scoped to one session. A raw job id that belongs to another session resolves to
``unknown`` rather than crossing the boundary. Job ids are generated unique
within a session and inserted into their partition before the process is
started, so concurrent ``spawn`` calls cannot race. Because every lookup is
session-scoped, two sessions may coincidentally (or, in tests, deliberately)
hold the same raw id without ever reaching each other's process.

Passing ``session_id=None`` to a lookup scopes it to the registry's default
partition; it never widens across sessions. Cross-session enumeration and lookup
is **admin-only** and must be requested explicitly with ``admin=True``. The shell
tools always pass ``ctx.session_id`` (and reject a missing/empty one) and never
set ``admin``, so a tool can never reach another session's process.

Safety notes
-----------
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
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ...errors import NexusError, OperationCancelled

__all__ = [
    "DEFAULT_GRACE_S",
    "DEFAULT_KILL_WAIT_S",
    "DEFAULT_MAX_COMPLETED_JOBS",
    "DEFAULT_MAX_RESULT_CHARS",
    "DEFAULT_MAX_RETAINED_BYTES",
    "DEFAULT_MAX_TOTAL_COMPLETED_JOBS",
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
    "require_session_id",
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
#: Retain at most this many completed jobs **per session** before evicting the
#: oldest in that session. Each job can hold up to two output buffers (2 MiB by
#: default), so an unbounded partition would leak that per job for the lifetime
#: of the runtime.
DEFAULT_MAX_COMPLETED_JOBS = 32
#: Combined retained stdout+stderr bytes across **all** sessions' completed jobs.
DEFAULT_MAX_RETAINED_BYTES = 64 * 1024 * 1024
#: Total completed jobs retained across **all** sessions. The per-session count
#: bounds one busy session; this bounds many idle sessions that each stay under
#: the per-session count but whose output is small enough not to trip the byte
#: cap.
DEFAULT_MAX_TOTAL_COMPLETED_JOBS = 256
#: Default hard runtime limit for a job when config cannot be read.
DEFAULT_MAX_RUNTIME_S = 3600.0
#: Seconds between ``tool.progress`` events while ``bash`` blocks.
PROGRESS_INTERVAL_S = 2.0
#: Progress events emitted per blocking ``bash`` call.
MAX_PROGRESS_EVENTS = 300
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
        shell: str | None = None,
        *,
        cwd_check: Callable[[], Path] | None = None,
        base_env: Mapping[str, str] | None = None,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
        grace_s: float = DEFAULT_GRACE_S,
        kill_wait_s: float = DEFAULT_KILL_WAIT_S,
        on_finish: Callable[[ShellJob], None] | None = None,
    ) -> None:
        self.job_id = job_id
        self.command = command
        self.cwd = cwd
        self.shell = shell
        self._cwd_check = cwd_check
        self.status = JobStatus.RUNNING
        self.pid: int | None = None
        self.pgid: int | None = None
        self.exit_code: int | None = None
        self.signal: int | None = None
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.stdout = OutputBuffer(output_limit)
        self.stderr = OutputBuffer(output_limit)
        self._base_env = dict(os.environ if base_env is None else base_env)
        self._env = None if env is None else dict(env)
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
        #: Per-job read cursor: bytes of stdout/stderr already returned to the
        #: model by ``bash`` ``status``/``wait`` (explicit offsets override it).
        self.read_stdout = 0
        self.read_stderr = 0
        #: True once a foreground run yielded, or the job started in background.
        self.background = False
        #: Hard-limit watchdog; held so the task is not garbage collected.
        self.watchdog: asyncio.Task[None] | None = None

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
        if self._final is not None:
            # Terminated before it could start (for example its session was
            # released while the registry was still between insert and start).
            # Never spawn a process that nothing tracks.
            return
        # Inherit the owning runtime's captured environment, never the
        # daemon's current environment. Per-call overrides stay local to this job.
        process_env = self._base_env.copy()
        if self._env:
            process_env.update(self._env)
        if self._cwd_check is not None:
            self.cwd = self._cwd_check()
        self._process = await asyncio.create_subprocess_shell(
            self.command,
            cwd=str(self.cwd),
            executable=self.shell,
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


@dataclass
class _Partition:
    """One session's jobs plus its completion order (retention/LRU oldest first)."""

    jobs: dict[str, ShellJob] = field(default_factory=dict)
    completed: deque[str] = field(default_factory=deque)


class JobRegistry:
    """Owns every shell job for one Runtime/ToolManager, partitioned by session.

    The registry is the only way to reach a job: ``job``/``kill`` take opaque
    string ids, never PIDs, and a completed job remains readable until the
    registry is closed, the session is released, or the job is explicitly
    discarded. Lookups are scoped to one ``session_id`` so concurrent sessions
    sharing one registry cannot read, enumerate, or kill each other's jobs.

    Retention is bounded at two levels: ``max_completed_jobs`` per session and
    ``max_total_completed_jobs`` / ``max_retained_bytes`` across all sessions.
    Active jobs are never evicted. Empty session partitions are pruned, so the
    registry does not accumulate one dict per session forever.
    """

    def __init__(
        self,
        *,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
        grace_s: float = DEFAULT_GRACE_S,
        kill_wait_s: float = DEFAULT_KILL_WAIT_S,
        environ: Mapping[str, str] | None = None,
        max_completed_jobs: int = DEFAULT_MAX_COMPLETED_JOBS,
        max_retained_bytes: int = DEFAULT_MAX_RETAINED_BYTES,
        max_total_completed_jobs: int = DEFAULT_MAX_TOTAL_COMPLETED_JOBS,
    ) -> None:
        for name, value in (
            ("max_completed_jobs", max_completed_jobs),
            ("max_retained_bytes", max_retained_bytes),
            ("max_total_completed_jobs", max_total_completed_jobs),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        # Copy even an empty explicit mapping: it must not inherit host secrets.
        self._environ = dict(os.environ if environ is None else environ)
        self._partitions: dict[str, _Partition] = {}
        #: ``(session_id, job_id)`` in completion order across all sessions.
        self._completed_global: deque[tuple[str, str]] = deque()
        self.output_limit = output_limit
        self.grace_s = grace_s
        self.kill_wait_s = kill_wait_s
        self.max_completed_jobs = max_completed_jobs
        self.max_retained_bytes = max_retained_bytes
        self.max_total_completed_jobs = max_total_completed_jobs
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        return sum(len(partition.jobs) for partition in self._partitions.values())

    def jobs(
        self, *, session_id: str | None = None, admin: bool = False
    ) -> tuple[ShellJob, ...]:
        """One session's jobs, or every job when ``admin=True``.

        ``admin=True`` is the explicit, admin-only cross-session escape hatch:
        it ignores ``session_id`` and returns every partition. Otherwise the
        result is scoped to ``session_id`` (``None`` = the default partition), so
        a missed session id can never widen a lookup.
        """
        self._check_scope(session_id, admin)
        if admin:
            return tuple(
                job
                for partition in self._partitions.values()
                for job in partition.jobs.values()
            )
        partition = self._partitions.get(self._session_key(session_id))
        return () if partition is None else tuple(partition.jobs.values())

    def active(
        self, *, session_id: str | None = None, admin: bool = False
    ) -> tuple[ShellJob, ...]:
        return tuple(
            job for job in self.jobs(session_id=session_id, admin=admin) if not job.done
        )

    def sessions(self, *, admin: bool = False) -> tuple[str, ...]:
        """Session ids that currently own at least one (possibly finished) job.

        Enumerating sessions is inherently cross-session, so it is admin-only.
        """
        if not admin:
            raise JobRegistryError("sessions() is admin-only; pass admin=True")
        return tuple(
            key for key, partition in self._partitions.items() if partition.jobs
        )

    def job(
        self,
        job_id: object,
        *,
        session_id: str | None = None,
        admin: bool = False,
    ) -> ShellJob | None:
        """Resolve `job_id`, scoped to ``session_id`` unless ``admin=True``.

        Without ``admin`` the search is confined to one partition
        (``session_id=None`` = the default partition); an id that belongs to a
        different session is reported as unknown. ``admin=True`` is the explicit,
        admin-only cross-session search the shell tools never use.
        """
        self._check_scope(session_id, admin)
        if not isinstance(job_id, str):
            return None
        if admin:
            for partition in self._partitions.values():
                found = partition.jobs.get(job_id)
                if found is not None:
                    return found
            return None
        partition = self._partitions.get(self._session_key(session_id))
        return None if partition is None else partition.jobs.get(job_id)

    async def spawn(
        self,
        command: str,
        *,
        session_id: str | None = None,
        cwd: str | Path,
        env: dict[str, str] | None = None,
        shell: str | None = None,
        cwd_check: Callable[[], Path] | None = None,
        output_limit: int | None = None,
    ) -> ShellJob:
        if self._closed:
            raise JobRegistryError("JobRegistry is closed")
        if not isinstance(command, str) or not command:
            raise JobRegistryError("command must be a non-empty string")
        key = self._session_key(session_id)
        partition = self._partitions.get(key)
        if partition is None:
            partition = _Partition()
            self._partitions[key] = partition
        # Generate the id and insert the job *before* the first await, so two
        # concurrent spawns cannot claim the same id or race on the partition.
        job_id = self._new_id(partition)
        job = ShellJob(
            job_id,
            command,
            Path(cwd),
            env,
            shell,
            cwd_check=cwd_check,
            base_env=self._environ,
            output_limit=(
                self.output_limit if output_limit is None else output_limit
            ),
            grace_s=self.grace_s,
            kill_wait_s=self.kill_wait_s,
            on_finish=lambda finished, _key=key: self._note_finished(
                _key, finished
            ),
        )
        partition.jobs[job_id] = job
        try:
            await job.start()
        except BaseException:
            partition.jobs.pop(job_id, None)
            self._prune_partition(key)
            raise
        if (
            self._closed
            or self._partitions.get(key) is not partition
            or partition.jobs.get(job_id) is not job
        ):
            # The registry was closed, or this session was released, while we
            # awaited the process start. Do not leave a live job behind.
            await job.terminate(reason=JobStatus.KILLED)
            partition.jobs.pop(job_id, None)
            self._prune_partition(key)
            reason = (
                "JobRegistry is closed"
                if self._closed
                else "session was released"
            )
            raise JobRegistryError(reason)
        self._evict()
        return job

    # -- session lifecycle -------------------------------------------------

    async def aclose_session(self, session_id: str) -> bool:
        """Terminate and reap every job owned by one session.

        Returns ``True`` when the session had a partition (even an empty one).
        Safe to call for an unknown session. Other sessions are untouched.
        """
        key = self._session_key(session_id)
        partition = self._partitions.pop(key, None)
        if partition is None:
            return False
        self._purge_global_for(key)
        jobs = list(partition.jobs.values())
        await asyncio.gather(
            *(job.terminate(reason=JobStatus.KILLED) for job in jobs),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(job.aclose() for job in jobs), return_exceptions=True
        )
        return True

    # -- retention ---------------------------------------------------------

    def _note_finished(self, session_key: str, job: ShellJob) -> None:
        """Record a completed job for retention/eviction (oldest evicted first)."""
        partition = self._partitions.get(session_key)
        if partition is not None and job.job_id in partition.jobs:
            partition.completed.append(job.job_id)
            self._completed_global.append((session_key, job.job_id))
        self._evict()

    def _retained_bytes(self) -> int:
        return sum(
            len(job.stdout) + len(job.stderr)
            for partition in self._partitions.values()
            for job in partition.jobs.values()
        )

    def _completed_count(self) -> int:
        return sum(len(partition.completed) for partition in self._partitions.values())

    def _evict(self) -> None:
        """Enforce the per-session count and the global count/byte limits.

        Active jobs are never evicted, so ``active()`` and any lookup of a
        running job are unaffected. Recently completed jobs stay readable until
        the limits are exceeded. Empty partitions are pruned afterwards.
        """
        for key, partition in list(self._partitions.items()):
            while len(partition.completed) > self.max_completed_jobs:
                if not self._drop_oldest_in(partition):
                    break
            self._prune_partition(key)
        while (
            self._completed_count() > self.max_total_completed_jobs
            or self._retained_bytes() > self.max_retained_bytes
        ):
            if not self._drop_oldest_global():
                break

    @staticmethod
    def _drop_oldest_in(partition: _Partition) -> bool:
        while partition.completed:
            job_id = partition.completed.popleft()
            job = partition.jobs.get(job_id)
            if job is None:
                continue
            if not job.done:  # pragma: no cover - defensive; ids are done-on-add
                partition.completed.append(job_id)
                return False
            del partition.jobs[job_id]
            return True
        return False

    def _drop_oldest_global(self) -> bool:
        while self._completed_global:
            key, job_id = self._completed_global.popleft()
            partition = self._partitions.get(key)
            if partition is None:
                continue
            job = partition.jobs.get(job_id)
            if job is None:
                continue
            if not job.done:  # pragma: no cover - defensive; ids are done-on-add
                self._completed_global.append((key, job_id))
                return False
            del partition.jobs[job_id]
            with contextlib.suppress(ValueError):
                partition.completed.remove(job_id)
            self._prune_partition(key)
            return True
        return False

    def _prune_partition(self, key: str) -> None:
        partition = self._partitions.get(key)
        if partition is not None and not partition.jobs:
            del self._partitions[key]

    def _purge_global_for(self, key: str) -> None:
        self._completed_global = deque(
            (session, job_id)
            for session, job_id in self._completed_global
            if session != key
        )

    def _new_id(self, partition: _Partition) -> str:
        """A fresh id unique **within this session's partition**.

        Session-scoped lookups make cross-session id reuse harmless; only the
        partition the job will live in must be collision-free.
        """
        while True:
            candidate = f"job_{secrets.token_hex(6)}"
            if candidate not in partition.jobs:
                return candidate

    @staticmethod
    def _session_key(session_id: object) -> str:
        if session_id is None:
            return ""
        if not isinstance(session_id, str):
            raise JobRegistryError("session_id must be a string or None")
        return session_id

    @staticmethod
    def _check_scope(session_id: object, admin: bool) -> None:
        """Reject an ambiguous scope: ``admin`` and a session id are exclusive."""
        if admin and session_id is not None:
            raise JobRegistryError(
                "pass either session_id or admin=True, not both"
            )

    async def kill(
        self,
        job_id: object,
        *,
        session_id: str | None = None,
        admin: bool = False,
    ) -> KillResult:
        if not isinstance(job_id, str):
            return KillResult("unknown", "")
        job = self.job(job_id, session_id=session_id, admin=admin)
        if job is None:
            return KillResult("unknown", job_id)
        if job.done:
            return KillResult(
                "finished", job_id, job.exit_code, job.signal
            )
        await job.terminate(reason=JobStatus.KILLED)
        return KillResult("killed", job_id, job.exit_code, job.signal)

    def discard(
        self,
        job_id: object,
        *,
        session_id: str | None = None,
        admin: bool = False,
    ) -> bool:
        self._check_scope(session_id, admin)
        if not isinstance(job_id, str):
            return False
        if admin:
            for key, partition in self._partitions.items():
                if job_id in partition.jobs:
                    return self._discard_from(key, partition, job_id)
            return False
        key = self._session_key(session_id)
        partition = self._partitions.get(key)
        if partition is None:
            return False
        return self._discard_from(key, partition, job_id)

    def _discard_from(
        self, key: str, partition: _Partition, job_id: str
    ) -> bool:
        job = partition.jobs.get(job_id)
        if job is None or not job.done:
            return False
        del partition.jobs[job_id]
        with contextlib.suppress(ValueError):
            partition.completed.remove(job_id)
        with contextlib.suppress(ValueError):
            self._completed_global.remove((key, job_id))
        self._prune_partition(key)
        return True

    async def aclose(self) -> None:
        """Terminate and reap every owned job in every session. Idempotent."""
        self._closed = True
        jobs = [
            job
            for partition in self._partitions.values()
            for job in partition.jobs.values()
        ]
        # Detach bookkeeping first so in-flight finish callbacks cannot mutate a
        # partition we are tearing down.
        self._partitions.clear()
        self._completed_global.clear()
        await asyncio.gather(
            *(job.terminate(reason=JobStatus.KILLED) for job in jobs),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(job.aclose() for job in jobs), return_exceptions=True
        )



async def await_job(
    job: ShellJob,
    *,
    timeout: float | None = None,
    yield_after: float | None = None,
    cancel_token: object | None = None,
    kill_on_cancel: bool = True,
    tick: Callable[[], Awaitable[None]] | None = None,
    tick_s: float = PROGRESS_INTERVAL_S,
) -> str:
    """Await completion, a yield/timeout window, or cooperative cancellation.

    Returns ``"completed"``, ``"yielded"`` (``yield_after`` elapsed; the job is
    left running) or ``"timeout"`` (``timeout`` elapsed; the process group is
    terminated). With ``kill_on_cancel`` (foreground runs) cooperative and task
    cancellation terminate the process group and then raise; without it (a
    ``wait`` on a background job) the job is left running. ``tick`` is called
    about every ``tick_s`` seconds while waiting, for progress reporting.
    """
    limit = yield_after if yield_after is not None else timeout
    completion = asyncio.ensure_future(job.wait())
    waiters: set[asyncio.Future[object]] = {completion}
    cancel_waiter: asyncio.Future[object] | None = None
    if cancel_token is not None:
        wait = getattr(cancel_token, "wait", None)
        if wait is not None:
            cancel_waiter = asyncio.ensure_future(wait())
            waiters.add(cancel_waiter)
    loop = asyncio.get_running_loop()
    deadline = None if limit is None else loop.time() + limit
    try:
        while True:
            slice_s = tick_s if tick is not None else None
            if deadline is not None:
                remaining = max(deadline - loop.time(), 0.0)
                slice_s = remaining if slice_s is None else min(slice_s, remaining)
            done, _ = await asyncio.wait(
                waiters, timeout=slice_s, return_when=asyncio.FIRST_COMPLETED
            )
            if completion in done:
                return "completed"
            if cancel_waiter is not None and cancel_waiter in done:
                if kill_on_cancel:
                    await job.terminate(reason=JobStatus.KILLED)
                reason = getattr(cancel_token, "reason", None)
                raise OperationCancelled(reason or "cancelled")
            if deadline is not None and loop.time() >= deadline:
                if yield_after is not None:
                    return "yielded"
                await job.terminate(reason=JobStatus.TIMED_OUT)
                return "timeout"
            if tick is not None:
                await tick()
    except asyncio.CancelledError:
        if kill_on_cancel:
            await asyncio.shield(job.terminate(reason=JobStatus.KILLED))
        raise
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


def enforce_max_runtime(job: ShellJob, limit: float) -> None:
    """Terminate ``job`` (status ``timed_out``) once it has run for ``limit`` s."""

    async def _watch() -> None:
        try:
            await job.wait(limit)
        except TimeoutError:
            await job.terminate(reason=JobStatus.TIMED_OUT)

    job.watchdog = asyncio.ensure_future(_watch())


def last_output_line(job: ShellJob) -> str:
    """The last non-empty line of recent output (stdout, else stderr)."""
    for buffer in (job.stdout, job.stderr):
        tail = buffer.snapshot()[-2048:].decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            if line.strip():
                return line.strip()[:200]
    return ""


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
        # Head + tail: test runners and builds print their summary last.
        head = budget * 2 // 5
        tail = budget - head
        omitted = len(body) - budget
        body = (
            body[:head]
            + f"\n[... {omitted} chars omitted ...]\n"
            + body[len(body) - tail :]
        )
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


def _tools_number(config: object, name: str) -> float | None:
    v2 = getattr(config, "v2", None)
    tools = getattr(v2, "tools", None) if v2 is not None else None
    value = getattr(tools, name, None)
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    ):
        return float(value)
    return None


def yield_window(config: object) -> float:
    """Seconds a foreground run blocks before yielding to the background."""
    value = _tools_number(config, "bash_yield_window_s")
    return value if value is not None else _default_timeout(config)


def max_runtime(config: object) -> float:
    """Hard runtime limit for any job (``tools.bash_max_s``)."""
    value = _tools_number(config, "bash_max_s")
    return value if value is not None else DEFAULT_MAX_RUNTIME_S


def resolve_timeout(args: dict[str, object], config: object) -> float | None:
    """Per-call ``timeout_s`` hard-kill limit, capped at ``bash_max_s``.

    ``None`` means the caller did not ask for one: only the yield window and
    ``bash_max_s`` apply.
    """
    raw = args.get("timeout_s") if isinstance(args, dict) else None
    if raw is None:
        return None
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or raw <= 0
    ):
        raise ValueError("timeout_s must be a positive finite number")
    return min(float(raw), max_runtime(config))


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


def require_session_id(session_id: object) -> str:
    """Return a usable, non-empty session id or fail closed.

    The shell tools call this before any registry lookup. A missing or empty
    session id must never be silently treated as "all sessions": it is rejected
    so a tool can only ever reach its own session's jobs.
    """
    if not isinstance(session_id, str) or not session_id:
        raise JobRegistryError(
            "a non-empty session_id is required for shell job scoping"
        )
    return session_id
