"""Automatic archive of session trees idle longer than the configured threshold.

`[sessions] auto_archive_idle_seconds` (default one hour, `0` disables) is
re-read on every sweep, so a config edit applies without restarting the
daemon. A sweep archives whole root trees through the ordinary archive
transition: one `session.archived` event per tree and no interruption, stopped
job or lost context. Idleness is the newest `session.updated` in the tree, which
message inserts and configuration changes set; reads, listings and status polls
never advance it.

A tree stays unarchived while any session in it is running a turn, waiting for
an approval or question, holding pending input or running a background job.
Those are work in progress, not idleness.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .config import ConfigFile

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)

DEFAULT_IDLE_SECONDS = 3600
SWEEP_INTERVAL_SECONDS = 60.0


def configured_idle_seconds(config: ConfigFile | None = None) -> float:
    """The live `[sessions] auto_archive_idle_seconds`; an invalid file keeps the default."""
    try:
        value = (config or ConfigFile()).read()["values"]["sessions"]["auto_archive_idle_seconds"]
    except (KeyError, OSError, TypeError, ValueError):
        return DEFAULT_IDLE_SECONDS
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_IDLE_SECONDS
    return float(value)


class AutoArchive:
    def __init__(self, runtime: Runtime, *, interval_seconds: float = SWEEP_INTERVAL_SECONDS) -> None:
        self.runtime = runtime
        self.interval_seconds = max(1.0, interval_seconds)
        self._task: asyncio.Task[None] | None = None
        self.last_sweep: float | None = None
        self.last_archived: list[str] = []
        self.last_error: str | None = None

    def sweep(self, *, now: float | None = None, older_than: float | None = None) -> list[str]:
        """Run one bounded sweep; safe to call from tests."""
        threshold = configured_idle_seconds() if older_than is None else older_than
        archived = self.runtime.archive_stale_sessions(older_than=threshold, now=now)
        if archived:
            log.info("auto-archived %d idle session tree(s)", len(archived))
        self.last_sweep = time.time() if now is None else now
        self.last_archived = archived
        return archived

    async def run_forever(self) -> None:
        while True:
            try:
                self.sweep()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # cleanup must never block the daemon
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("automatic session archive sweep failed")
            await asyncio.sleep(self.interval_seconds)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="nexus-auto-archive")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
