"""Session layer: append-only JSONL log, cross-process lock, migration, handle."""
from ..errors import SessionBusy, SessionError
from .ids import is_valid_session_id, validate_session_id
from .lock import SessionLock
from .manager import SessionManager
from .migrate import (
    MigrationResult,
    backup_path,
    jsonl_path,
    legacy_path,
    migrate_session,
    should_migrate,
)
from .session import Session, TurnLease
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
    "SESSION_LOG_VERSION",
    "SNAPSHOT_SUFFIX",
    "SNAPSHOT_VERSION",
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
    "SessionStore",
    "Snapshot",
    "SnapshotSummary",
    "SummaryRecord",
    "TurnLease",
    "backup_path",
    "is_valid_session_id",
    "jsonl_path",
    "legacy_path",
    "migrate_session",
    "should_migrate",
    "snapshot_path",
    "validate_session_id",
]
