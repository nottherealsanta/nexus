"""Bounded host projections for durable session archive metadata."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import Callable, Iterable
from typing import Any

from ..errors import SessionBusy
from ..host.protocol import (
    ArchivedSummary,
    SessionArchive,
    SessionArchiveResult,
    SessionListArchived,
    SessionListArchivedResult,
    SessionPreview,
    SessionPreviewResult,
    SessionSearch,
    SessionSearchResult,
    SessionUnarchive,
    SessionUnarchiveResult,
)
from ..session.manager import SessionManager
from ..util import redact_secrets
from ..view import ConversationView, apply, initial_state

MAX_ARCHIVED_PAGE = 200
MAX_SEARCH_SESSIONS = 50
MAX_SEARCH_TAIL_BYTES = 512 * 1024
MAX_QUERY_CHARS = 256
_LOG = logging.getLogger(__name__)


async def sweep_stale_sessions(
    manager: SessionManager, days: int | Callable[[], int]
) -> int:
    """Run one bounded stale-session sweep off the daemon event loop."""
    if callable(days):
        try:
            days = await asyncio.to_thread(days)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("could not read session auto-archive config: %s", exc)
            return 0
    if type(days) is not int or not 0 <= days <= 3650 or days == 0:
        return 0
    sweep = getattr(manager, "archive_stale", None)
    if not callable(sweep):
        return 0
    try:
        records = await asyncio.to_thread(
            sweep, now=None, older_than=days * 24 * 60 * 60
        )
    except Exception as exc:  # noqa: BLE001 - cleanup never blocks daemon startup
        _LOG.warning("session auto-archive sweep failed: %s", exc)
        return 0
    return len(records)


async def reap_with_archive_sweep(
    stop_event: Any,
    interval: float,
    archive_sweep: Callable[[], Any],
    idle: Callable[[], bool],
    request_stop: Callable[[str], Any],
    log: Callable[[str], Any],
) -> None:
    """Run the daemon idle timer and startup/hourly archive maintenance."""
    await archive_sweep()
    next_sweep = asyncio.get_running_loop().time() + 60 * 60
    while not stop_event.is_set():
        await asyncio.sleep(interval)
        if stop_event.is_set():
            return
        if asyncio.get_running_loop().time() >= next_sweep:
            await archive_sweep()
            next_sweep = asyncio.get_running_loop().time() + 60 * 60
        if idle():
            log("daemon.idle_shutdown")
            request_stop("idle")
            return


def archived_page(
    manager: SessionManager, *, query: str = "", limit: int = 200, cursor: int = 0
) -> tuple[list[ArchivedSummary], bool]:
    """Return a bounded archived-session page with simple substring/subsequence matching."""
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        raise ValueError("query must be at most 256 characters")
    if type(limit) is not int or not 1 <= limit <= MAX_ARCHIVED_PAGE:
        raise ValueError(f"limit must be between 1 and {MAX_ARCHIVED_PAGE}")
    if type(cursor) is not int or cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    needle = query.casefold().strip()
    rows: list[tuple[int, ArchivedSummary]] = []
    for archived in manager.archived():
        try:
            summary = manager.summary(archived.session_id)
        except (OSError, ValueError):
            continue
        matched, rank = _matches(
            needle,
            (summary.id, summary.title),
        )
        if not matched:
            continue
        values = summary.to_dict()
        rows.append((rank, ArchivedSummary(**values, archived_at=archived.archived_at, reason=archived.reason)))
    rows.sort(key=lambda pair: (pair[0], -pair[1].last_activity, pair[1].id))
    page = [row for _, row in rows[cursor : cursor + limit]]
    return page, cursor + limit < len(rows)


def archive_summary_count(manager: SessionManager) -> int:
    """Count durable archive records that still refer to session artifacts."""
    archived = getattr(manager, "archived", None)
    return len(archived()) if callable(archived) else 0


def dispatch_archive_command(
    command: Any, manager: SessionManager, supervisor: Any
) -> Any | None:
    """Handle archive-specific host verbs; ``None`` means another route owns it."""
    if isinstance(command, SessionArchive):
        if supervisor.running_for(command.session) or supervisor.queued_for(command.session):
            raise SessionBusy(f"Session {command.session!r} has active or queued work")
        manager.archive(command.session, command.reason)
        return SessionArchiveResult(session=manager.summary(command.session))
    if isinstance(command, SessionUnarchive):
        manager.unarchive(command.session)
        return SessionUnarchiveResult(session=manager.summary(command.session))
    if isinstance(command, SessionListArchived):
        rows, has_more = archived_page(
            manager, query=command.query, limit=command.limit, cursor=command.cursor
        )
        return SessionListArchivedResult(sessions=rows, has_more=has_more)
    if isinstance(command, SessionPreview):
        manager.summary(command.session)
        text, truncated = preview(manager, command.session, max_chars=command.max_chars)
        return SessionPreviewResult(text=text, truncated=truncated)
    if isinstance(command, SessionSearch):
        return SessionSearchResult(
            ids=search_sessions(
                manager,
                query=command.query,
                archived_only=command.archived_only,
                limit=command.limit,
            )
        )
    return None


def preview(manager: SessionManager, session_id: str, *, max_chars: int = 6000) -> tuple[str, bool]:
    """Reduce a session log and return bounded user/assistant text only."""
    if type(max_chars) is not int or not 1 <= max_chars <= 6000:
        raise ValueError("max_chars must be between 1 and 6000")
    read = manager._consistent_read(session_id)
    view: ConversationView = initial_state(session_id)
    for event in read.events():
        view = apply(view, event)
    parts = []
    for message in view.messages:
        if message.role not in {"user", "assistant"}:
            continue
        body = _safe_transcript_text(redact_secrets(message.text))
        if body:
            parts.append(f"{message.role}: {body}")
    rendered = "\n\n".join(parts)
    truncated = len(rendered) > max_chars
    return rendered[:max_chars], truncated


def search_sessions(
    manager: SessionManager, *, query: str, archived_only: bool = True, limit: int = 50
) -> list[str]:
    """Search bounded log tails for content, returning session ids only."""
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ValueError("query must be non-empty and at most 256 characters")
    if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_SESSIONS:
        raise ValueError(f"limit must be between 1 and {MAX_SEARCH_SESSIONS}")
    needle = query.casefold()
    candidates = (
        [row.session_id for row in manager.archived()]
        if archived_only
        else [row.id for row in manager.list(include_archived=True)]
    )[:MAX_SEARCH_SESSIONS]
    found = []
    for session_id in candidates:
        artifacts = manager._artifact_paths(session_id)
        path = next(
            (item for item in artifacts if item.name == f"{session_id}.jsonl"),
            None,
        )
        if path is None:
            path = next(
                (item for item in artifacts if item.name == f"{session_id}.json"),
                None,
            )
        if path is None:
            continue
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                size = os.fstat(fd).st_size
                os.lseek(fd, max(0, size - MAX_SEARCH_TAIL_BYTES), os.SEEK_SET)
                data = os.read(fd, MAX_SEARCH_TAIL_BYTES)
            finally:
                os.close(fd)
        except OSError:
            continue
        if needle in data.decode("utf-8", errors="ignore").casefold():
            found.append(session_id)
            if len(found) >= limit:
                break
    return found


def _matches(query: str, values: Iterable[str]) -> tuple[bool, int]:
    if not query:
        return True, 0
    rank = 2
    for value in values:
        folded = value.casefold()
        if query in folded:
            return True, 0
        iterator = iter(folded)
        if all(any(char == candidate for candidate in iterator) for char in query):
            rank = min(rank, 1)
    return rank < 2, rank


def _safe_transcript_text(value: str) -> str:
    """Keep readable newlines while neutralizing terminal control characters."""
    import unicodedata

    return "".join(
        char if char in "\n\t" or unicodedata.category(char) != "Cc" else " "
        for char in value
    )


__all__ = [
    "archive_summary_count",
    "archived_page",
    "dispatch_archive_command",
    "preview",
    "reap_with_archive_sweep",
    "search_sessions",
    "sweep_stale_sessions",
]
