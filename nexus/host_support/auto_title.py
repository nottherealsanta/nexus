"""Start, bound and cancel the background title call for a new session.

Contract (plans/done/SESSION_TITLE_PLAN.md, Part 2): when a *root* session receives
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

__all__ = ["AutoTitler", "MAX_CONCURRENT_TITLES", "USER_TITLE_MAX_CHARS", "clean_user_title", "set_user_title"]

#: Longest title a user may set (longer than a model-written one, which is 50).
USER_TITLE_MAX_CHARS = 100


def clean_user_title(title: Any) -> str:
    """One line, control characters dropped, at most ``USER_TITLE_MAX_CHARS``; ``""`` if empty."""
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    kept = "".join(" " if char in "\r\n\t" else char for char in title if char in "\r\n\t" or char.isprintable())
    return " ".join(kept.split())[:USER_TITLE_MAX_CHARS].rstrip()


def set_user_title(sessions: Any, session_id: str, title: str) -> str:
    """Store a user-chosen title durably and return it.

    The title lives in the same ``sessions`` row column the auto title uses, but
    with ``title_source='user'``: ``SessionStore.set_auto_title`` only replaces
    ``first_message`` titles and the first-message derivation only fills an empty
    title, so neither ever overwrites it. Unlike the auto title it may replace
    an ``auto`` or earlier ``user`` title. A session that is not yet created, or
    is trashed, raises :class:`SessionError`.

    SessionManager exposes only the first-message-only auto-title setter, not
    a user-title setter. Like session_archive's metadata copy, this uses the
    existing store.db.transaction convention for a scoped metadata update.
    """
    cleaned = clean_user_title(title)
    if not cleaned:
        raise ValueError("title must not be empty")
    store = getattr(sessions, "store", None)
    if store is None:
        raise SessionError("this session store cannot rename sessions")
    session_id = sessions.summary(session_id).id  # validates the id; trashed or unknown raises
    with store.db.transaction() as conn:
        changed = conn.execute(
            "UPDATE sessions SET title=?, title_source='user' "
            "WHERE project_id=? AND namespace=? AND id=? AND trash_id IS NULL",
            (cleaned, store.project_id, store.namespace, session_id),
        ).rowcount
    if not changed:
        raise SessionError(f"Session {session_id!r} does not exist")
    return cleaned

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
        return bool(getattr(section, "auto_title", True)), str(getattr(section, "title_model", "openai/gpt-5-mini") or "openai/gpt-5-mini")

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
        if model in order:
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
            result = await generate_title(self._runtime.router, model, text, session)
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
