"""Host-side view of, and controls for, the ``shell`` tool's jobs (SHELL_PLAN P4).

The four host commands ``SessionJobs``, ``SessionJobLog``, ``SessionJobStop`` and
``SessionJobBackground`` are answered here, from the runtime's ``JobRegistry``.
A client never reads the registry or a log file itself and never signals a
process: it asks the host, which

- answers from **the named session's registry partition only** (another session's
  job id resolves to ``unknown``, exactly as it does for the model's own tools);
- accepts only registry-owned job ids (``job_`` plus 12 hex digits), never a path
  or a PID, and builds the log path itself from ``(workspace, session, job)``;
- bounds every reply: at most ``MAX_JOBS`` rows, 2,000 tail lines, 50 KiB of log
  text, 500 characters of command, and no environment values.

Jobs that outlive their registry entry (evicted from memory) stay readable
through their log file, addressed the same way ``shell action=log`` does.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..session.ids import validate_session_id
from ..tools.builtin import _jobs, _shell_logs
from ..tools.builtin import shell as shell_tool
from ..tools.builtin._output_limit import MAX_BYTES

if TYPE_CHECKING:
    from ..host import protocol as p

# ``nexus.host.protocol`` is imported inside the functions: ``nexus.host`` imports
# the facade, which imports this module, so a top-level import would be circular
# whenever this module is imported first.

__all__ = [
    "DEFAULT_LOG_BYTES",
    "DEFAULT_TAIL_LINES",
    "MAX_COMMAND_CHARS",
    "MAX_JOBS",
    "MAX_TAIL_LINES",
    "SessionJobControl",
]

#: Rows one ``SessionJobs`` reply may carry.
MAX_JOBS = 64
#: Longest command text in a row.
MAX_COMMAND_CHARS = 500
DEFAULT_TAIL_LINES = 200
MAX_TAIL_LINES = 2000
DEFAULT_LOG_BYTES = 32 * 1024


def _clip(text: str) -> str:
    return text if len(text) <= MAX_COMMAND_CHARS else text[:MAX_COMMAND_CHARS] + "…"


def _job_id(value: object) -> str:
    if not isinstance(value, str) or not _shell_logs.JOB_ID_RE.fullmatch(value):
        raise ValueError("job_id must be a job id returned by the shell tool")
    return value


def _protocol() -> Any:
    from ..host import protocol

    return protocol


def summarize(job: _jobs.ShellJob) -> p.JobSummary:
    """The wire row for one job: ids, status and counts, never output."""
    p = _protocol()
    return p.JobSummary(
        job_id=job.job_id,
        command=_clip(job.command),
        cwd=str(job.cwd),
        status=job.status.value,
        background=job.background,
        pid=job.pid,
        exit_code=job.exit_code,
        signal=job.signal,
        started_at=job.created_at,
        finished_at=job.finished_at,
        log_bytes=job.log.total,
        log_lines=job.log.lines,
        stderr_bytes=job.log.stderr_bytes,
        log_available=job.log.path is not None and job.log.saved > 0,
        can_background=job.foreground_awaited and not job.background and not job.done,
        source="tool" if job.announced else "user",
        persistent=job.persistent,
    )


class SessionJobControl:
    """Answers the job host commands for one runtime."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def _registry(self) -> _jobs.JobRegistry | None:
        registry = getattr(self._runtime, "job_registry", None)
        return registry if isinstance(registry, _jobs.JobRegistry) else None

    def jobs(self, session_id: str) -> p.SessionJobsResult:
        p = _protocol()
        session_id = validate_session_id(session_id)
        registry = self._registry()
        rows = [] if registry is None else list(registry.jobs(session_id=session_id))
        # Running first, then newest first.
        rows.sort(key=lambda job: (job.done, -job.created_at, job.job_id))
        shown = rows[:MAX_JOBS]
        return p.SessionJobsResult(
            session=session_id,
            jobs=[summarize(job) for job in shown],
            running=sum(1 for job in rows if not job.done),
            truncated=len(rows) > len(shown),
        )

    async def log(
        self,
        session_id: str,
        job_id: str,
        *,
        tail_lines: int = DEFAULT_TAIL_LINES,
        offset: int | None = None,
        limit: int = DEFAULT_LOG_BYTES,
    ) -> p.SessionJobLogResult:
        p = _protocol()
        session_id = validate_session_id(session_id)
        job_id = _job_id(job_id)
        if type(tail_lines) is not int or not 1 <= tail_lines <= MAX_TAIL_LINES:
            raise ValueError(f"tail_lines must be between 1 and {MAX_TAIL_LINES}")
        if offset is not None and (type(offset) is not int or offset < 0):
            raise ValueError("offset must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= MAX_BYTES:
            raise ValueError(f"limit must be between 1 and {MAX_BYTES}")

        registry = self._registry()
        job = None if registry is None else registry.job(job_id, session_id=session_id)
        path: Path | None
        if job is not None:
            path = job.log.path
        else:
            # Evicted from memory: addressed by (session, job) only, so another
            # session's id can never resolve to this session's directory.
            workspace = getattr(self._runtime, "workspace", None)
            try:
                path = (
                    None
                    if workspace is None
                    else _shell_logs.log_path(Path(workspace), session_id, job_id)
                )
            except ValueError:
                path = None
        if path is None or not path.is_file():
            if job is None:
                return p.SessionJobLogResult(
                    session=session_id, job_id=job_id, status="unknown",
                    message="No such job in this session, or its log is gone.",
                )
            return p.SessionJobLogResult(
                session=session_id, job_id=job_id, status="unavailable",
                message="This job has no readable log (none was written, or it was removed).",
            )

        try:
            if offset is None:
                data, start, size = await asyncio.to_thread(
                    shell_tool._read_tail, path, tail_lines
                )
                data = shell_tool._clip_tail(data)
                start = size - len(data)  # the kept lines always run to EOF
                end = size
                next_offset = None
            else:
                data, size = await asyncio.to_thread(
                    shell_tool._read_range, path, offset, limit
                )
                start = min(offset, size)
                end = start + len(data)
                next_offset = end if end < size else None
        except OSError as exc:
            return p.SessionJobLogResult(
                session=session_id, job_id=job_id, status="unavailable",
                message=f"Cannot read the log: {exc.strerror or exc}",
            )
        return p.SessionJobLogResult(
            session=session_id, job_id=job_id, status="ok",
            text=data.decode("utf-8", "replace"),
            start=start, end=end, size=size, next_offset=next_offset,
        )

    async def stop(self, session_id: str, job_id: str) -> p.SessionJobStopResult:
        p = _protocol()
        session_id = validate_session_id(session_id)
        job_id = _job_id(job_id)
        registry = self._registry()
        if registry is None:
            return p.SessionJobStopResult(session=session_id, job_id=job_id, outcome="unknown")
        # The user stopped it themselves: no completion notification (or wake).
        result = await registry.kill(job_id, session_id=session_id, quiet=True)
        return p.SessionJobStopResult(
            session=session_id, job_id=job_id, outcome=result.outcome,
            exit_code=result.exit_code, signal=result.signal,
        )

    def background(self, session_id: str, job_id: str) -> p.SessionJobBackgroundResult:
        """Ask the tool call blocking on the job to hand it to the background.

        The move itself, and ``shell.job_yielded``, happens in that tool call when
        it wakes; this only records the request.
        """
        p = _protocol()
        session_id = validate_session_id(session_id)
        job_id = _job_id(job_id)
        registry = self._registry()
        job = None if registry is None else registry.job(job_id, session_id=session_id)
        if job is None:
            return p.SessionJobBackgroundResult(
                session=session_id, job_id=job_id, outcome="unknown"
            )
        outcome = job.request_background()
        return p.SessionJobBackgroundResult(
            session=session_id, job_id=job_id, outcome=outcome,
            requested=outcome == "requested",
        )
