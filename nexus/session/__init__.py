"""Session layer: shared SQLite state database, cross-process lock, handle.

Production sessions live in :mod:`nexus.session.db` (STATE_PLAN §4-5.1);
:mod:`nexus.session.store` survives as the legacy JSONL reader used by
:mod:`nexus.session.import_legacy`.
"""
from ..errors import SessionBusy, SessionError
from . import export
from .db import SqliteSessionStore, StateDatabase
from .ids import is_valid_session_id, validate_session_id
from .import_legacy import ImportResult, import_workspace_sessions
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
from .migrate import (
    MigrationResult,
    backup_path,
    jsonl_path,
    legacy_path,
    migrate_session,
    should_migrate,
)
from .session import (
    DEFAULT_EVENT_BUFFER,
    DEFAULT_UNATTENDED_DECISION,
    TERMINAL_EVENTS,
    Session,
    TurnLease,
)
from .snapshot import (
    SNAPSHOT_SUFFIX,
    SNAPSHOT_VERSION,
    CurrentState,
    Snapshot,
    SnapshotSummary,
    snapshot_path,
)
from .store import (
    SESSION_LOG_VERSION,
    EventRecord,
    JsonlSessionStore,
    MessageRecord,
    ReadResult,
    SessionRecord,
    SessionStore,
    SummaryRecord,
)

__all__ = [
    "DEFAULT_EVENT_BUFFER",
    "DEFAULT_RETENTION_SECONDS",
    "DEFAULT_UNATTENDED_DECISION",
    "SESSION_LOG_VERSION",
    "SNAPSHOT_SUFFIX",
    "SNAPSHOT_VERSION",
    "TERMINAL_EVENTS",
    "TRASH_VERSION",
    "ArchiveRecord",
    "CurrentState",
    "EventRecord",
    "ImportResult",
    "JsonlSessionStore",
    "MessageRecord",
    "MigrationResult",
    "ReadResult",
    "Session",
    "SessionBusy",
    "SessionError",
    "SessionLock",
    "SessionManager",
    "SessionRecord",
    "SessionState",
    "SessionStore",
    "SessionSummary",
    "Snapshot",
    "SnapshotSummary",
    "SqliteSessionStore",
    "StateDatabase",
    "SummaryRecord",
    "TrashRecord",
    "TurnLease",
    "backup_path",
    "export",
    "import_workspace_sessions",
    "is_valid_session_id",
    "jsonl_path",
    "legacy_path",
    "migrate_session",
    "should_migrate",
    "snapshot_path",
    "validate_session_id",
]
