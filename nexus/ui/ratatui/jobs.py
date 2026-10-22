"""The Jobs tab of the native details sidebar: the session's shell jobs and one bounded log tail.

Everything comes from the existing host commands (``SessionJobs``, ``SessionJobLog``,
``SessionJobStop``, ``SessionJobBackground``) through the client; this module never signals a
process or reads a log file. It polls only while the sidebar is drawn on the Jobs tab, and every
reply is dropped when the session or generation changed while it was in flight.
"""
from __future__ import annotations

import time

from ...ui_support.text import escape_controls, sanitize

#: Tail lines asked for and shown, and the width one shown line is clipped to.
LOG_LINES = 120
LINE_WIDTH = 400
COMMAND_WIDTH = 120
POLL_SECONDS = 1.0

_STATUS = {"running": "running", "completed": "done", "failed": "failed",
           "timed_out": "timed out", "killed": "stopped"}


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KB"
    return f"{count / 1024 / 1024:.1f} MB"


def job_row(job, *, now: float) -> dict:
    """One job as the sidebar shows it: no working directory, pid or file path."""
    running = job.status == "running"
    end = job.finished_at if job.finished_at is not None else now
    facts = [_duration(end - job.started_at) if job.started_at else ""]
    if not running and job.exit_code is not None:
        facts.append(f"exit {job.exit_code}")
    if job.signal:
        facts.append(f"signal {job.signal}")
    if job.log_bytes:
        facts.append(f"{_size(job.log_bytes)} log")
    return {
        "id": sanitize(job.job_id, 64),
        "command": sanitize(job.command, COMMAND_WIDTH) or "(no command)",
        "status": job.status,
        "label": _STATUS.get(job.status, sanitize(job.status, 16)),
        "source": "you" if job.source == "user" else "agent",
        "mode": (
            "persistent"
            if getattr(job, "persistent", False)
            else "background" if job.background else "foreground" if running else ""
        ),
        "facts": " · ".join(fact for fact in facts if fact),
        "log": bool(job.log_available),
        "stop": running,
        "background": bool(running and job.can_background),
    }


class Jobs:
    def __init__(self, shell):
        self.shell = shell
        self.visible = False  # the native client draws the sidebar's Jobs tab
        self.rows: list[dict] = []
        self.running = 0
        self.truncated = False
        self.loaded = False
        self.error = ""
        self.notice = ""
        self.selected = ""
        self.log: dict | None = None  # {"id", "lines", "status", "message", "clipped"}
        self.due = 0.0
        self._key = self._owner()

    def _owner(self):
        return (getattr(self.shell.controller, "session", None), self.shell.generation)

    def _reset(self) -> None:
        self.rows, self.running, self.truncated, self.loaded = [], 0, False, False
        self.error = self.notice = self.selected = ""
        self.log = None
        self.due = 0.0
        self._key = self._owner()

    def _current(self) -> bool:
        """Drop everything shown for another session; true when state belongs to this one."""
        if self._key != self._owner():
            self._reset()
            return False
        return True

    @property
    def active(self) -> bool:
        values = self.shell.preferences.values
        return self.visible and values["details_sidebar"] and values["details_tab"] == "Jobs"

    def _row(self, job_id: str) -> dict | None:
        return next((row for row in self.rows if row["id"] == job_id), None)

    async def poll(self, *, force: bool = False) -> None:
        self._current()
        if not self.active or (not force and time.monotonic() < self.due):
            return
        self.due = time.monotonic() + POLL_SECONDS
        owner, session = self._owner(), self._owner()[0]
        try:
            result = await self.shell.client.session_jobs(session)
            if owner != self._owner():
                return
            now = time.time()
            self.rows = [job_row(job, now=now) for job in result.jobs]
            self.running, self.truncated, self.loaded, self.error = result.running, bool(result.truncated), True, ""
            if not self._row(self.selected):
                self.selected = self.rows[0]["id"] if self.rows else ""
            if self.log and not self._row(self.log["id"]):
                self.log = None  # the job left the list; its tail goes with it
            if self.log:
                await self._read_log(self.log["id"], owner)
        except Exception as exc:  # advisory: keep the last rows and say why they may be stale
            if owner == self._owner():
                self.error = sanitize(str(exc), 160)

    async def _read_log(self, job_id: str, owner) -> None:
        result = await self.shell.client.session_job_log(owner[0], job_id, tail_lines=LOG_LINES)
        if owner != self._owner() or (self.log or {}).get("id") != job_id:
            return
        lines = [sanitize(line, LINE_WIDTH) for line in result.text.splitlines()[-LOG_LINES:]]
        self.log = {"id": job_id, "lines": lines, "status": result.status,
                    "message": sanitize(result.message, 160), "clipped": bool(result.start > 0)}

    async def select(self, job_id: str) -> None:
        if self._current() and self._row(job_id):
            self.selected = job_id

    async def open_log(self, job_id: str) -> None:
        row = self._current() and self._row(job_id)
        if not row:
            return
        self.selected = job_id
        if not row["log"]:
            self.notice = "This job has no log to show."
            return
        self.notice = ""
        self.log = {"id": job_id, "lines": [], "status": "loading", "message": "", "clipped": False}
        owner = self._owner()
        try:
            await self._read_log(job_id, owner)
        except Exception as exc:
            if owner == self._owner() and self.log:
                self.log = {**self.log, "status": "unavailable", "message": sanitize(str(exc), 160)}

    async def close_log(self) -> None:
        if self._current():
            self.log = None

    async def stop(self, job_id: str) -> None:
        row = self._current() and self._row(job_id)
        if not row or not row["stop"]:
            return
        owner = self._owner()
        try:
            result = await self.shell.client.session_job_stop(owner[0], job_id)
            if owner != self._owner():
                return
            self.notice = {"killed": "Stopped the job.", "finished": "The job had already finished.",
                           "unknown": "The job is no longer known."}.get(result.outcome, sanitize(result.outcome, 40))
        except Exception as exc:
            if owner == self._owner():
                self.notice = "Stop failed: " + sanitize(str(exc), 120)
        await self.poll(force=True)

    async def background(self, job_id: str) -> None:
        row = self._current() and self._row(job_id)
        if not row or not row["background"]:
            return
        owner = self._owner()
        try:
            result = await self.shell.client.session_job_background(owner[0], job_id)
            if owner != self._owner():
                return
            self.notice = {"requested": "Asked to continue in the background.",
                           "finished": "The job had already finished.",
                           "already_background": "The job already runs in the background.",
                           "not_foreground": "No tool call is waiting on this job.",
                           "unknown": "The job is no longer known."}.get(result.outcome, sanitize(result.outcome, 40))
        except Exception as exc:
            if owner == self._owner():
                self.notice = "Background failed: " + sanitize(str(exc), 120)
        await self.poll(force=True)

    def projection(self) -> dict:
        self._current()
        return {
            "rows": [{**row, "selected": row["id"] == self.selected} for row in self.rows],
            "running": self.running, "truncated": self.truncated, "loaded": self.loaded,
            "error": self.error, "notice": self.notice,
            "log": ({**self.log, "lines": [escape_controls(line) for line in self.log["lines"]]}
                    if self.log else None),
        }
