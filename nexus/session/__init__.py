"""Session layer: append-only JSONL log, cross-process lock, migration, handle."""
from ..errors import SessionBusy, SessionError
from . import export
from .ids import is_valid_session_id, validate_session_id
from .lock import SessionLock
from .manager import (
    DEFAULT_RETENTION_SECONDS,
    TRASH_VERSION,
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
    "CurrentState",
    "EventRecord",
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
    "SummaryRecord",
    "TrashRecord",
    "TurnLease",
    "backup_path",
    "export",
    "is_valid_session_id",
    "jsonl_path",
    "legacy_path",
    "migrate_session",
    "should_migrate",
    "snapshot_path",
    "validate_session_id",
]
