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
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .store import new_id
from .tools.base import ToolError

if TYPE_CHECKING:
    from .terminals import Terminal, TerminalService

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
    error: str | None = None
    task: asyncio.Task[None] | None = None
    watchdog: asyncio.Task[None] | None = None
    silent: bool = False  # no finish notice (session/project deletion, daemon shutdown)
    terminal: Terminal | None = None
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
        data = {"id": self.id, "session_id": self.session_id, "root": self.root, "command": self.command,
                "directory": self.directory, "started": self.started, "finished": self.finished,
                "duration_s": end - self.started, "status": self.status, "exit_code": self.exit_code,
                "timeout": self.timeout, "output_chars": self.size, "dropped_chars": self.dropped}
        if self.error:
            data["error"] = self.error
        if self.terminal is not None:
            data["terminal_id"] = self.terminal.id
        if output:
            data["output"] = self.output
        return data


class Jobs:
    def __init__(self) -> None:
        self.items: dict[str, Job] = {}
        self.closing = False
        self.start_lock = asyncio.Lock()
        self.terminal_service: TerminalService | None = None

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
                    timeout: float | None, on_finish: OnFinish, project_id: str | None = None) -> Job:
        async with self.start_lock:
            return await self._start(session_id=session_id, root=root, command=command,
                                     directory=directory, timeout=timeout, on_finish=on_finish, project_id=project_id)

    async def _start(self, *, session_id: str, root: str, command: str, directory: Path,
                     timeout: float | None, on_finish: OnFinish, project_id: str | None) -> Job:
        if self.closing:
            raise ToolError("the daemon is shutting down")
        if sum(1 for job in self.items.values() if job.finished is None) >= MAX_LIVE:
            raise ToolError(f"live background bash job limit ({MAX_LIVE}) reached; kill one first")
        self._trim()
        if self.terminal_service is not None and project_id is not None:
            job_id = new_id("job")
            job = Job(job_id, session_id, root, command, str(directory), None, time.time(), timeout)
            term = self.terminal_service.create(project_id=project_id, workspace=str(directory), creator="background bash",
                                                session_id=session_id, rows=24, cols=80, command=command,
                                                background_job_id=job_id, output_listener=job.retain)
            job.process, job.terminal = term.process, term
            label = " ".join("".join(c if not unicodedata.category(c).startswith("C") else " "
                                     for c in command).split())[:80] or "Background bash"
            self.terminal_service.rename(term.id, label)
            self.items[job.id] = job
            job.task = asyncio.create_task(self._run(job, on_finish))
            if timeout is not None:
                job.watchdog = asyncio.create_task(self._watch(job))
            return job
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
        job = Job(new_id("job"), session_id, root, command, str(directory), process, time.time(), timeout)
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
        if job.terminal is not None:
            await asyncio.sleep(1)
            if job.finished is None:
                asyncio.create_task(job.terminal.close())

    async def _run(self, job: Job, on_finish: OnFinish) -> None:
        try:
            if job.terminal is not None:
                term = job.terminal
                await term.output_done.wait()
                await term.waiter
                term.output_listener = None
                if term.status == "closed" and not job.timed_out:
                    job.killed = True
            else:
                assert job.process.stdout is not None
                while chunk := await job.process.stdout.read(8192):
                    job.retain(chunk)
            job.retain(b"", final=True)
            if job.terminal is None:
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
                if job.terminal is not None:
                    await job.terminal.waiter
                else:
                    await job.process.wait()
            job.exit_code = job.process.returncode
            job.finished = time.time()
            if not job.silent and not self.closing:
                with contextlib.suppress(Exception):
                    on_finish(job)

    async def kill(self, job_id: str, *, root: str | None = None) -> Job:
        job = self.get(job_id, root=root)
        if job.finished is None:
            job.killed = True
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
            await asyncio.gather(*(job.terminal.close() for job in jobs
                                   if job.task in pending and job.terminal is not None), return_exceptions=True)
            for job in jobs:
                if job.task in pending and job.terminal is None:
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
