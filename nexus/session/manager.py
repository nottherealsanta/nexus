"""SessionManager: the open/migrate/recover entry point (plan section 5.1).

Phase 1 scope is intentionally small. ``open`` validates the ID, migrates a
legacy ``<id>.json`` v1 session on first open, and runs dangling-tool recovery.
``fork``/``list``/``delete``/``replay`` are later packets; this class exists so
those can be added without changing the store, lock, or session handle.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.turn import TurnLimits
from ..errors import SessionBusy, SessionError
from .ids import validate_session_id
from .lock import SessionLock
from .migrate import MigrationResult, migrate_session, should_migrate
from .session import DEFAULT_EVENT_BUFFER, Session
from .store import SessionStore


class SessionManager:
    """Owns a sessions directory and hands out :class:`Session` handles."""

    def __init__(
        self,
        directory: str | Path,
        *,
        store: SessionStore | None = None,
        assemble: Callable[[object], Any] | None = None,
        provider_for: Callable[..., Any] | None = None,
        limits: TurnLimits | Callable[[], TurnLimits] | None = None,
        tools: Callable[..., Any] | None = None,
        attended: bool = False,
        event_buffer: int = DEFAULT_EVENT_BUFFER,
    ):
        self.directory = Path(directory)
        self.store = store if store is not None else SessionStore(self.directory)
        # Loop dependencies are opaque callables forwarded to each handle, so
        # this layer stays free of context/router/tools imports.
        self._assemble = assemble
        self._provider_for = provider_for
        self._limits = limits
        self._tools = tools
        self._attended = bool(attended)
        self._event_buffer = event_buffer

    def path(self, session_id: str) -> Path:
        return self.store.log_path(session_id)

    def exists(self, session_id: str) -> bool:
        return self.store.exists(session_id)

    def open(
        self,
        session_id: str,
        *,
        create: bool = True,
        recover: bool = True,
        migrate: bool = True,
    ) -> Session:
        """Open a session, migrating a legacy v1 file first if one exists.

        ``create=False`` raises when the session has no log. ``recover`` runs
        dangling-tool crash recovery before returning the handle.
        """
        session_id = validate_session_id(session_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        if migrate:
            self.migrate(session_id)
        if not self.store.exists(session_id):
            if not create:
                raise SessionError(f"Session {session_id!r} does not exist")
            # Create only after migration so a legacy file is never shadowed.
            self.store.create(session_id)
        session = Session(
            session_id,
            store=self.store,
            assemble=self._assemble,
            provider_for=self._provider_for,
            limits=self._limits,
            tools=self._tools,
            attended=self._attended,
            event_buffer=self._event_buffer,
        )
        if recover:
            session.recover_dangling_tool_uses()
        return session

    def migrate(self, session_id: str) -> MigrationResult | None:
        """Migrate one session if needed, serialized by the session lock."""
        session_id = validate_session_id(session_id)
        if not should_migrate(self.directory, session_id):
            return None
        lock = SessionLock.for_session(self.directory, session_id)
        try:
            with lock.exclusive(blocking=False):
                return migrate_session(self.directory, session_id)
        except SessionBusy:
            # Another opener is migrating or running. If it finished, we are
            # done; otherwise surface the contention instead of corrupting.
            if should_migrate(self.directory, session_id):
                raise
            return None


__all__ = ["SessionManager"]
