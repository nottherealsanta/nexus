"""Session layer: shared SQLite state database, cross-process lock, handle.

Session records and snapshots are persisted only in :mod:`nexus.session.db`.
JSONL remains an export format, never a session storage backend.
"""
from ..errors import SessionBusy, SessionError
from . import export
from .db import SqliteSessionStore, StateDatabase
from .ids import is_valid_session_id, validate_session_id
from .lock import SessionLock
from .manager import (
    DEFAULT_RETENTION_SECONDS,
    TRASH_VERSION,
    ArchiveRecord,
    SessionManager,
    SessionState,
    SessionSummary,
    TrashRecord,
)
from .records import (
    SESSION_LOG_VERSION,
    EventRecord,
    MessageRecord,
    ReadResult,
    SessionRecord,
    SummaryRecord,
)
from .session import (
    DEFAULT_EVENT_BUFFER,
    DEFAULT_UNATTENDED_DECISION,
    TERMINAL_EVENTS,
    Session,
    TurnLease,
)
from .snapshot import SNAPSHOT_VERSION, CurrentState, Snapshot, SnapshotSummary

__all__ = [
    "DEFAULT_EVENT_BUFFER",
    "DEFAULT_RETENTION_SECONDS",
    "DEFAULT_UNATTENDED_DECISION",
    "SESSION_LOG_VERSION",
    "SNAPSHOT_VERSION",
    "TERMINAL_EVENTS",
    "TRASH_VERSION",
    "ArchiveRecord",
    "CurrentState",
    "EventRecord",
    "MessageRecord",
    "ReadResult",
    "Session",
    "SessionBusy",
    "SessionError",
    "SessionLock",
    "SessionManager",
    "SessionRecord",
    "SessionState",
    "SessionSummary",
    "Snapshot",
    "SnapshotSummary",
    "SqliteSessionStore",
    "StateDatabase",
    "SummaryRecord",
    "TrashRecord",
    "TurnLease",
    "export",
    "is_valid_session_id",
    "validate_session_id",
]
