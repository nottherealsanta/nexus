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
from .store import (
    SESSION_LOG_VERSION,
    EventRecord,
    MessageRecord,
    ReadResult,
    SessionRecord,
    SessionStore,
)

__all__ = [
    "SESSION_LOG_VERSION",
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
    "TurnLease",
    "backup_path",
    "is_valid_session_id",
    "jsonl_path",
    "legacy_path",
    "migrate_session",
    "should_migrate",
    "validate_session_id",
]
