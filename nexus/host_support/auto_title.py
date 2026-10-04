"""Start, bound and cancel the background title call for a new session.

Contract (plans/SESSION_TITLE_PLAN.md, Part 2): when a *root* session receives
its first message and ``[sessions] auto_title`` is on, one background task asks
the title model for a short title and stores it with
``SessionManager.set_auto_title``. The turn never waits for it. At most one task
runs per session and two run at once; a task is cancelled when its session is
deleted or the host shuts down. Clients pick the new title up on their normal
session-list refresh.

Forks and subagent sessions never start here: a fork has a parent, and
subagents are not started through the host's ``SessionStart``.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..errors import ConfigError, SessionError
from ..session.title import generate_title

__all__ = ["AutoTitler", "MAX_CONCURRENT_TITLES"]

#: Title calls running at once across every session.
MAX_CONCURRENT_TITLES = 2
#: How long to wait for the first message to land in the log before giving up.
_STORE_ATTEMPTS = 40
_STORE_DELAY_S = 0.25


class AutoTitler:
    """Per-host owner of the background title tasks."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_TITLES)

    def _settings(self) -> tuple[bool, str]:
        loader = getattr(self._runtime, "_load_config", None)
        config = None
        if callable(loader):
            try:
                config = loader()
            except (ConfigError, OSError):
                config = None
        section = getattr(getattr(config, "v2", None), "sessions", None)
        return bool(getattr(section, "auto_title", True)), str(getattr(section, "title_model", "low") or "low")

    def candidate(self, session: str, text: str, labels: list[str] | None = None) -> str | None:
        """The text to title if ``session`` is a fresh root session, else ``None``.

        Call this *before* the turn starts: the first message is not in the log
        yet, so ``message_count`` is still zero.
        """
        enabled, model = self._settings()
        if not enabled or session in self._tasks:
            return None
        sessions = getattr(self._runtime, "sessions", None)
        router = getattr(self._runtime, "router", None)
        if sessions is None or router is None:
            return None
        # A tier with no runnable model (a plain config has no tiers at all) must
        # not be sent to a provider as a model id: titles stay the first message,
        # which Settings -> Session titles says in the same words.
        order = getattr(getattr(self._runtime, "tiers", None), "order", ("low", "medium", "high"))
        runnable = getattr(router, "tier_runnable", None)
        if model in order and callable(runnable) and not runnable(model):
            return None
        try:
            summary = sessions.summary(session)
        except SessionError:
            summary = None  # not created yet: the first message creates it
        if summary is not None and (summary.message_count > 0 or summary.parent_id):
            return None
        parts = [text.strip(), *(f"@{label}" for label in (labels or []) if label)]
        joined = " ".join(part for part in parts if part)
        return joined or None

    def start(self, session: str, text: str | None) -> None:
        """Schedule the background call for ``text`` (a :meth:`candidate` result)."""
        if not text or session in self._tasks:
            return
        task = asyncio.get_running_loop().create_task(self._run(session, text), name=f"title:{session}")
        self._tasks[session] = task
        task.add_done_callback(lambda done, key=session: self._finished(key, done))

    def _finished(self, session: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(session) is task:
            del self._tasks[session]
        if not task.cancelled():
            task.exception()  # never leave an unretrieved exception behind

    def cancel(self, session: str) -> None:
        task = self._tasks.get(session)
        if task is not None:
            task.cancel()

    async def aclose(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    async def _run(self, session: str, text: str) -> None:
        _, model = self._settings()
        async with self._slots:
            result = await generate_title(self._runtime.router, model, text)
        if result is None:
            return
        sessions = self._runtime.sessions
        for _ in range(_STORE_ATTEMPTS):
            try:
                if sessions.set_auto_title(session, result.title):
                    return
                source = sessions.title_source(session)
            except SessionError:
                return
            # '' means the first message has not reached the log yet; anything
            # else (``auto``, ``user``) is final and is never overwritten.
            if source != "":
                return
            await asyncio.sleep(_STORE_DELAY_S)
