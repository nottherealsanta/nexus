"""Background bash jobs owned by the daemon (PLAN.md step 9a).

A background command runs in the session's directory, in its own process group.
Its combined stdout/stderr is kept in memory (bounded, with dropped characters
counted) so `bash_output` can read it after the launch returns. A job belongs to
the session tree that started it; the tools can only see their own tree, while
the daemon API can see every job.

Jobs are not tied to a turn: interrupting the turn or finishing/aborting a code
cell does not stop them. Deleting their session (or the daemon shutting down)
does. A finished job calls `on_finish` once, which the runtime turns into a
`bash.finished` event and a notice in the starting session's context.
"""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import new_id
from .tools.base import ToolError

MAX_LIVE = 16  # running at once
MAX_JOBS = 64  # running + retained for output
MAX_OUTPUT = 1_000_000  # characters kept per job
NOTICE_CHARS = 4_000  # output tail in the finish notice

OnFinish = Callable[["Job"], None]


def kill_process(process: Any) -> None:
    """SIGKILL the command's process group; ignore a process that already exited."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def process_identity(pid: int) -> dict[str, Any] | None:
    """What `ps` says about a pid now: `{"start": epoch seconds, "pgid": int}`, or None when no such process.

    A stored pid alone proves nothing (pids are reused). A job's command runs as its own process-group leader, so a
    live process is the job only if its group is its pid and it started within seconds of the job's start time.
    """
    import subprocess
    import time as _time
    try:
        out = subprocess.run(["ps", "-o", "pgid=,lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                             timeout=5, check=False, env={"LC_ALL": "C", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"unknown": True}
    out = out.strip()
    if not out:
        return None
    try:
        pgid, _, started = out.partition(" ")
        return {"pgid": int(pgid), "start": _time.mktime(_time.strptime(" ".join(started.split()), "%a %b %d %H:%M:%S %Y"))}
    except ValueError:
        return {"unknown": True}


def probe(pid: Any, started: Any) -> str:
    """`running` (verified to be this job's process group), `gone`, or `unknown`. Reads only; never signals."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1 or not isinstance(started, int | float):
        return "unknown"
    found = process_identity(pid)
    if found is None:
        return "gone"
    if found.get("unknown"):
        return "unknown"
    if found["pgid"] == pid and abs(found["start"] - float(started)) <= 10:
        return "running"
    return "gone"  # the pid now belongs to something else, so the job's process is not there


@dataclass
class Job:
    id: str
    session_id: str
    root: str
    command: str
    directory: str
    process: Any
    started: float
    timeout: float | None = None
    parts: list[str] = field(default_factory=list)
    size: int = 0
    dropped: int = 0
    finished: float | None = None
    exit_code: int | None = None
    timed_out: bool = False
    killed: bool = False
    user_stopped: bool = False  # stopped from a client: the notice must not wake the model
    error: str | None = None
    task: asyncio.Task[None] | None = None
    watchdog: asyncio.Task[None] | None = None
    silent: bool = False  # no finish notice (session/project deletion, daemon shutdown)
    turn_id: str | None = None  # the turn and tool call that started the job (inspector `job` lane)
    tool_call_id: str | None = None
    decoder: Any = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"))

    def retain(self, data: bytes, *, final: bool = False) -> None:
        text = self.decoder.decode(data, final=final)
        room = max(0, MAX_OUTPUT - self.size)
        if room:
            self.parts.append(text[:room])
            self.size += min(len(text), room)
        self.dropped += max(0, len(text) - room)

    @property
    def output(self) -> str:
        return "".join(self.parts)

    @property
    def status(self) -> str:
        if self.finished is None:
            return "running"
        if self.timed_out:
            return "timeout"
        if self.killed:
            return "killed"
        if self.error:
            return "error"
        return "done"

    def json(self, *, output: bool = False) -> dict[str, Any]:
        end = self.finished or time.time()
        data = {"pid": getattr(self.process, "pid", None), "id": self.id, "session_id": self.session_id, "root": self.root, "command": self.command,
                "directory": self.directory, "started": self.started, "finished": self.finished,
                "duration_s": end - self.started, "status": self.status, "exit_code": self.exit_code,
                "timeout": self.timeout, "output_chars": self.size, "dropped_chars": self.dropped}
        if self.error:
            data["error"] = self.error
        if self.turn_id:
            data["turn_id"] = self.turn_id
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        if output:
            data["output"] = self.output
        return data


class Jobs:
    def __init__(self) -> None:
        self.items: dict[str, Job] = {}
        self.closing = False
        self.start_lock = asyncio.Lock()

    def get(self, job_id: str, *, root: str | None = None) -> Job:
        job = self.items.get(job_id)
        if job is None or (root is not None and job.root != root):
            raise ToolError(f"no bash job {job_id}")
        return job

    def list(self, *, root: str | None = None) -> list[Job]:
        return sorted((job for job in self.items.values() if root is None or job.root == root),
                      key=lambda job: job.started, reverse=True)

    def _trim(self) -> None:
        finished = sorted((job for job in self.items.values() if job.finished is not None),
                          key=lambda job: job.finished or 0)
        while len(self.items) >= MAX_JOBS and finished:
            self.items.pop(finished.pop(0).id, None)
        if len(self.items) >= MAX_JOBS:
            raise ToolError(f"background bash job limit ({MAX_JOBS}) reached; kill one or wait for output to be reaped")

    async def start(self, *, session_id: str, root: str, command: str, directory: Path,
                    timeout: float | None, on_finish: OnFinish,
                    turn_id: str | None = None, tool_call_id: str | None = None) -> Job:
        async with self.start_lock:
            return await self._start(session_id=session_id, root=root, command=command,
                                     directory=directory, timeout=timeout, on_finish=on_finish,
                                     turn_id=turn_id, tool_call_id=tool_call_id)

    async def _start(self, *, session_id: str, root: str, command: str, directory: Path,
                     timeout: float | None, on_finish: OnFinish,
                     turn_id: str | None, tool_call_id: str | None) -> Job:
        if self.closing:
            raise ToolError("the daemon is shutting down")
        if sum(1 for job in self.items.values() if job.finished is None) >= MAX_LIVE:
            raise ToolError(f"live background bash job limit ({MAX_LIVE}) reached; kill one first")
        self._trim()
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            "/bin/bash", "-c", command, cwd=directory, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
        ))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # The launch was cancelled but the process may exist; do not leave it running.
            process = await spawn
            kill_process(process)
            await process.communicate()
            raise
        job = Job(new_id("job"), session_id, root, command, str(directory), process, time.time(), timeout,
                  turn_id=turn_id, tool_call_id=tool_call_id)
        self.items[job.id] = job
        job.task = asyncio.create_task(self._run(job, on_finish))
        if timeout is not None:
            job.watchdog = asyncio.create_task(self._watch(job))
        return job

    async def _watch(self, job: Job) -> None:
        try:
            await asyncio.sleep(job.timeout or 0)
        except asyncio.CancelledError:
            return
        if job.finished is not None:  # a child may still hold stdout after the shell exits
            return
        job.timed_out = True
        kill_process(job.process)

    async def _run(self, job: Job, on_finish: OnFinish) -> None:
        try:
            assert job.process.stdout is not None
            while chunk := await job.process.stdout.read(8192):
                job.retain(chunk)
            job.retain(b"", final=True)
            await job.process.wait()
        except asyncio.CancelledError:
            job.killed = True
            kill_process(job.process)
            raise
        except Exception as exc:  # noqa: BLE001 - a broken pipe or read error must still finish the job visibly
            job.error = f"{type(exc).__name__}: {exc}"
            kill_process(job.process)
        finally:
            if job.watchdog is not None:
                job.watchdog.cancel()
            with contextlib.suppress(Exception):
                await job.process.wait()
            job.exit_code = job.process.returncode
            job.finished = time.time()
            if not job.silent and not self.closing:
                with contextlib.suppress(Exception):
                    on_finish(job)

    async def kill(self, job_id: str, *, root: str | None = None, user: bool = False) -> Job:
        job = self.get(job_id, root=root)
        if job.finished is None:
            job.killed = True
            job.user_stopped = user
            kill_process(job.process)
            if job.task is not None:
                await self._settle([job])
        return job

    async def _settle(self, jobs: list[Job]) -> None:
        tasks = [job.task for job in jobs if job.task is not None]
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=1)
        if pending:
            for job in jobs:
                if job.task in pending:
                    job.task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def cancel_for(self, session_ids: set[str], *, silent: bool = True) -> None:
        running = []
        for job in self.items.values():
            if job.finished is None and job.session_id in session_ids:
                job.silent = silent
                job.killed = True
                kill_process(job.process)
                if job.task is not None:
                    running.append(job)
        if running:
            await self._settle(running)

    async def shutdown(self) -> None:
        self.closing = True
        # Wait for an already-starting process to be registered before collecting jobs.
        async with self.start_lock:
            pass
        running = []
        for job in self.items.values():
            if job.finished is None:
                job.silent = True
                job.killed = True
                kill_process(job.process)
                if job.task is not None:
                    running.append(job)
        if running:
            await self._settle(running)
        self.items.clear()
