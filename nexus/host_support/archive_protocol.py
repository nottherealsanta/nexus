"""Wire records for the bounded session archive commands."""

from __future__ import annotations

import msgspec

from ..session.manager import SessionSummary


class SessionArchive(msgspec.Struct, tag=True, frozen=True):
    session: str
    reason: str = "user"


class SessionUnarchive(msgspec.Struct, tag=True, frozen=True):
    session: str


class SessionListArchived(msgspec.Struct, tag=True, frozen=True):
    query: str = ""
    limit: int = 200
    cursor: int = 0


class SessionPreview(msgspec.Struct, tag=True, frozen=True):
    session: str
    max_chars: int = 6000


class SessionSearch(msgspec.Struct, tag=True, frozen=True):
    query: str
    archived_only: bool = True
    limit: int = 50


class SessionListResult(msgspec.Struct, tag=True, frozen=True):
    sessions: list[SessionSummary] = msgspec.field(default_factory=list)
    archived_count: int = 0


class ArchivedSummary(SessionSummary, frozen=True):
    """Session summary plus durable archive metadata for the archive browser."""

    archived_at: float = 0.0
    reason: str = "user"


class SessionArchiveResult(msgspec.Struct, tag=True, frozen=True):
    session: SessionSummary


class SessionUnarchiveResult(msgspec.Struct, tag=True, frozen=True):
    session: SessionSummary


class SessionListArchivedResult(msgspec.Struct, tag=True, frozen=True):
    sessions: list[ArchivedSummary] = msgspec.field(default_factory=list)
    has_more: bool = False


class SessionPreviewResult(msgspec.Struct, tag=True, frozen=True):
    text: str = ""
    truncated: bool = False


class SessionSearchResult(msgspec.Struct, tag=True, frozen=True):
    ids: list[str] = msgspec.field(default_factory=list)
