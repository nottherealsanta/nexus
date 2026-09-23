"""One-way migration from the v1 whole-file format to the v2 JSONL log.

The legacy runtime writes ``<id>.json`` as ``{version: 1, exchanges: [...]}``.
The new runtime reads and writes ``<id>.jsonl``. Migration is explicit and owned
by the new runtime: it runs on first open, only when the JSONL log is absent,
and never overwrites a valid JSONL log.

Properties this module guarantees:

* **Strict validation** — a malformed v1 document raises instead of producing a
  half-valid log.
* **Immutable backup** — the original bytes are copied to ``<id>.v1.bak`` and
  that file is never overwritten.
* **Idempotent** — once ``<id>.jsonl`` exists the call is a no-op.
* **Atomic publish** — the log is written to a temp file, ``fsync``'d, then
  renamed, so a crash cannot leave a partial log that blocks re-migration.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec

from ..errors import SessionError
from ..model.message import Message, Text
from .ids import validate_session_id
from .store import MessageRecord


@dataclass(frozen=True)
class MigrationResult:
    session: str
    migrated: bool
    messages: int = 0
    json_path: Path | None = None
    jsonl_path: Path | None = None
    backup_path: Path | None = None


def legacy_path(directory: str | Path, session: str) -> Path:
    return Path(directory) / f"{validate_session_id(session)}.json"


def backup_path(directory: str | Path, session: str) -> Path:
    return Path(directory) / f"{validate_session_id(session)}.v1.bak"


def jsonl_path(directory: str | Path, session: str) -> Path:
    return Path(directory) / f"{validate_session_id(session)}.jsonl"


def should_migrate(directory: str | Path, session: str) -> bool:
    """True only when a legacy file exists and no JSONL log has been written."""
    return not jsonl_path(directory, session).exists() and legacy_path(directory, session).exists()


def migrate_session(directory: str | Path, session: str) -> MigrationResult:
    """Convert ``<id>.json`` to ``<id>.jsonl`` if needed.

    Callers should hold the session's exclusive lock so concurrent opens do not
    race; the manager does exactly that. This function acquires **no** lock of
    its own, so it is reentrant-safe to call while the caller already holds the
    session and/or trash locks (as the create path does): there is no nested
    ``flock`` and no deadlock. The atomic publish still refuses to overwrite a
    log another opener won.
    """
    directory = Path(directory)
    session_id = validate_session_id(session)
    legacy = directory / f"{session_id}.json"
    log = directory / f"{session_id}.jsonl"
    backup = directory / f"{session_id}.v1.bak"

    if log.exists():
        return MigrationResult(session_id, False, jsonl_path=log)
    if not legacy.exists():
        return MigrationResult(session_id, False, json_path=None, jsonl_path=log)

    try:
        raw = legacy.read_bytes()
    except OSError as exc:
        raise SessionError(f"Cannot read legacy session: {legacy}") from exc

    exchanges = _parse_v1(raw, legacy)

    directory.mkdir(parents=True, exist_ok=True)
    _write_backup(backup, raw)

    messages = _messages_from_exchanges(exchanges)
    records = _build_records(messages)
    published = _publish(log, records)

    return MigrationResult(
        session_id,
        published,
        messages=len(messages) if published else 0,
        json_path=legacy,
        jsonl_path=log,
        backup_path=backup if backup.exists() else None,
    )


def _parse_v1(raw: bytes, path: Path) -> list[dict[str, str]]:
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SessionError(f"Invalid v1 session JSON: {path}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SessionError(f"Unsupported session format: {path}")
    exchanges = data.get("exchanges")
    if not isinstance(exchanges, list):
        raise SessionError(f"Invalid v1 session: 'exchanges' must be a list: {path}")
    parsed: list[dict[str, str]] = []
    for row in exchanges:
        if (
            not isinstance(row, dict)
            or set(row) != {"user", "assistant"}
            or not all(isinstance(value, str) for value in row.values())
        ):
            raise SessionError(f"Invalid exchange in v1 session: {path}")
        parsed.append({"user": row["user"], "assistant": row["assistant"]})
    return parsed


def _messages_from_exchanges(exchanges: list[dict[str, str]]) -> list[Message]:
    messages: list[Message] = []
    for exchange in exchanges:
        messages.append(Message(role="user", content=[Text(text=exchange["user"])]))
        messages.append(Message(role="assistant", content=[Text(text=exchange["assistant"])]))
    return messages


def _build_records(messages: list[Message]) -> list[MessageRecord]:
    return [
        MessageRecord(seq=index, message=message)
        for index, message in enumerate(messages, start=1)
    ]


def _write_backup(path: Path, raw: bytes) -> bool:
    """Copy the legacy bytes once. Returns False if a backup already existed."""
    try:
        with open(path, "xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return False
    return True


def _publish(log: Path, records: list[MessageRecord]) -> bool:
    """Atomically publish the JSONL log; ``False`` if another opener won."""
    payload = b"".join(msgspec.json.encode(record) + b"\n" for record in records)
    fd, temp = tempfile.mkstemp(dir=log.parent, prefix=".migrate-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if log.exists():  # another opener won the race; never overwrite
            return False
        os.replace(temp, log)
        _fsync_dir(log.parent)
        return True
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform without dir fsync
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


__all__ = [
    "MigrationResult",
    "backup_path",
    "jsonl_path",
    "legacy_path",
    "migrate_session",
    "should_migrate",
]
