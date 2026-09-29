"""One-time import of a project's legacy ``.nexus/sessions``/``.nexus/trash``
into the shared state database (STATE_PLAN §5.1).

Runs at most once per project: :func:`import_workspace_sessions` checks
``projects.legacy_imported_at`` first and is a fast no-op once set. The scan
itself is:

* **Idempotent** -- each session id already present in the database (by
  primary key) is skipped, so a crash between two sessions and a retry never
  double-imports or raises.
* **Flock-guarded** -- ``~/.nexus/locks/import-<project_id>.lock`` serializes
  two daemons racing to import the same project.
* **One transaction per session** -- each imported session's full record
  prefix (plus, for a trashed one, its exact historical trash metadata) is
  published by a single :meth:`~nexus.session.db.SqliteSessionStore.create_from_records`
  call.
* **Never destructive** -- once every session is imported the legacy
  ``sessions``/``trash`` directories are *renamed* to
  ``sessions.imported-<ts>``/``trash.imported-<ts>``, never deleted. A log an
  importer cannot parse is left exactly where it was (and reported), and does
  not block the rest of the scan.
* **Off the caller's critical path** -- every failure is caught and logged;
  import never raises out to a startup path (STATE_PLAN §5.1).
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import msgspec

from ..errors import SessionError
from . import snapshot as snapshot_mod
from .db import StateDatabase
from .ids import is_valid_session_id, validate_session_id
from .lock import TrashLock
from .migrate import migrate_session, should_migrate
from .store import JsonlSessionStore

_LOG = logging.getLogger(__name__)

#: Sub-directory names under a legacy ``<workspace>/.nexus``.
_SESSIONS_DIRNAME = "sessions"
_AGENTS_DIRNAME = "agents"
_TRASH_DIRNAME = "trash"
_ARCHIVE_INDEX = "archive.json"
_TRASH_META = "meta.json"

#: Hard cap on sessions imported per project per run, so a runaway legacy
#: directory can never make startup import unbounded.
MAX_IMPORTED_SESSIONS = 20_000


@dataclass(frozen=True)
class ImportResult:
    """Outcome of one :func:`import_workspace_sessions` call."""

    ran: bool = False
    imported: int = 0
    skipped_existing: int = 0
    corrupt: tuple[str, ...] = ()
    renamed: tuple[str, ...] = field(default_factory=tuple)


def import_workspace_sessions(
    db: StateDatabase,
    project_id: str,
    legacy_dir: str | Path,
    *,
    home: str | Path | None = None,
) -> ImportResult:
    """Import ``legacy_dir`` (a workspace's ``.nexus``) into ``db`` once.

    Safe to call on every daemon start: a project already marked imported
    returns immediately without touching the filesystem.
    """
    legacy_dir = Path(legacy_dir)
    project = db.project_row(project_id)
    if project is not None and project.get("legacy_imported_at"):
        return ImportResult(ran=False)
    sessions_dir = legacy_dir / _SESSIONS_DIRNAME
    trash_dir = legacy_dir / _TRASH_DIRNAME
    if not sessions_dir.is_dir() and not trash_dir.is_dir():
        db.touch_project(project_id, str(legacy_dir.parent))
        db.mark_legacy_imported(project_id)
        return ImportResult(ran=False)

    lock_path = _import_lock_path(home, project_id)
    lock = TrashLock(lock_path)
    lock.acquire(blocking=True)
    try:
        # Re-check: a racing daemon may have finished the import while we waited.
        project = db.project_row(project_id)
        if project is not None and project.get("legacy_imported_at"):
            return ImportResult(ran=False)
        return _import_locked(db, project_id, legacy_dir, sessions_dir, trash_dir)
    finally:
        lock.release()


def _import_lock_path(home: str | Path | None, project_id: str) -> Path:
    from ..config.paths import nexus_home

    return nexus_home(home) / "locks" / f"import-{project_id}.lock"


def _import_locked(
    db: StateDatabase, project_id: str, legacy_dir: Path, sessions_dir: Path, trash_dir: Path
) -> ImportResult:
    imported = 0
    skipped = 0
    corrupt: list[str] = []
    archive_index = _read_archive_index(sessions_dir / _ARCHIVE_INDEX)

    if sessions_dir.is_dir():
        for namespace, directory in (("main", sessions_dir), ("agents", sessions_dir / _AGENTS_DIRNAME)):
            if not directory.is_dir():
                continue
            for session_id in _candidate_ids(directory):
                if imported + skipped >= MAX_IMPORTED_SESSIONS:
                    break
                try:
                    outcome = _import_one_live(
                        db, project_id, namespace, directory, session_id, archive_index
                    )
                except Exception as exc:  # noqa: BLE001 - a bad log must not block the rest
                    _LOG.warning(
                        "legacy session import: skipping corrupt log %s/%s: %s",
                        directory, session_id, exc,
                    )
                    corrupt.append(session_id)
                    continue
                if outcome:
                    imported += 1
                else:
                    skipped += 1

    if trash_dir.is_dir():
        for entry in sorted(p for p in trash_dir.iterdir() if p.is_dir() and not p.is_symlink()):
            try:
                outcome = _import_one_trashed(db, project_id, entry)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("legacy session import: skipping corrupt trash entry %s: %s", entry, exc)
                corrupt.append(entry.name)
                continue
            if outcome:
                imported += 1
            else:
                skipped += 1

    renamed: list[str] = []
    stamp = int(time.time())
    for directory in (sessions_dir, trash_dir):
        if not directory.is_dir():
            continue
        target = directory.with_name(f"{directory.name}.imported-{stamp}")
        try:
            directory.rename(target)
            renamed.append(str(target))
        except OSError as exc:  # pragma: no cover - defensive; never destructive either way
            _LOG.warning("legacy session import: could not rename %s: %s", directory, exc)

    db.touch_project(project_id, str(legacy_dir.parent))
    db.mark_legacy_imported(project_id)
    return ImportResult(
        ran=True, imported=imported, skipped_existing=skipped,
        corrupt=tuple(corrupt), renamed=tuple(renamed),
    )


def _candidate_ids(directory: Path) -> list[str]:
    """Session ids with a ``.jsonl`` or v1 ``.json`` log, deterministic order."""
    ids: set[str] = set()
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for path in entries:
        if not path.is_file() or path.is_symlink():
            continue
        name = path.name
        if name.endswith(".jsonl"):
            candidate = name[: -len(".jsonl")]
        elif name.endswith(".json") and _looks_like_legacy_v1(path):
            candidate = name[: -len(".json")]
        else:
            continue
        if is_valid_session_id(candidate):
            ids.add(candidate)
    return sorted(ids)


def _looks_like_legacy_v1(path: Path) -> bool:
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if not raw:
        return False
    try:
        data = msgspec.json.decode(raw)
    except (msgspec.DecodeError, msgspec.ValidationError):
        return False
    return isinstance(data, dict) and data.get("version") == 1 and isinstance(data.get("exchanges"), list)


def _import_one_live(
    db: StateDatabase,
    project_id: str,
    namespace: str,
    directory: Path,
    session_id: str,
    archive_index: dict[str, tuple[float, str]],
) -> bool:
    """Import one session from ``directory``; ``True`` if newly imported."""
    from .db import SqliteSessionStore

    store = SqliteSessionStore(db, project_id, namespace)
    if store.row_exists(session_id):
        return False
    if should_migrate(directory, session_id):
        # Materializes ``<id>.jsonl`` from the legacy ``<id>.json`` in place;
        # the directory is renamed away (never deleted) once import finishes.
        migrate_session(directory, session_id)
    legacy_store = JsonlSessionStore(directory)
    read = legacy_store.read(session_id)  # crash-tail dropped, never repaired on disk
    records = list(read.records)
    archived = archive_index.get(session_id)
    snapshot = _read_legacy_snapshot(directory, session_id)
    store.create_from_records(
        session_id,
        records,
        archived_at=archived[0] if archived else None,
        archive_reason=archived[1] if archived else None,
    )
    if snapshot is not None:
        try:
            store.write_snapshot(session_id, snapshot)
        except Exception:  # noqa: BLE001 - derived cache only
            pass
    return True


def _import_one_trashed(db: StateDatabase, project_id: str, entry: Path) -> bool:
    from .db import SqliteSessionStore

    meta_path = entry / _TRASH_META
    if meta_path.is_symlink() or not meta_path.is_file():
        return False
    try:
        meta = json.loads(meta_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise SessionError(f"unreadable trash meta.json: {meta_path}") from exc
    if not isinstance(meta, dict):
        raise SessionError(f"malformed trash meta.json: {meta_path}")
    session_id = meta.get("session_id")
    if not is_valid_session_id(session_id):
        raise SessionError(f"trash meta.json names an invalid session id: {meta_path}")
    session_id = validate_session_id(session_id)
    store = SqliteSessionStore(db, project_id, "main")
    if store.row_exists(session_id):
        return False
    if should_migrate(entry, session_id):
        migrate_session(entry, session_id)
    legacy_store = JsonlSessionStore(entry)
    read = legacy_store.read(session_id)
    records = list(read.records)
    snapshot = _read_legacy_snapshot(entry, session_id)
    trashed_at = _finite_float(meta.get("trashed_at"))
    delete_after = _finite_float(meta.get("delete_after"))
    reason = meta.get("reason") if isinstance(meta.get("reason"), str) else ""
    store.create_from_records(
        session_id,
        records,
        trash_id=str(meta.get("trash_id") or entry.name),
        trashed_at=trashed_at,
        trash_expires_at=delete_after,
        trash_reason=reason,
    )
    if snapshot is not None:
        try:
            store.write_snapshot(session_id, snapshot)
        except Exception:  # noqa: BLE001
            pass
    return True


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _read_legacy_snapshot(directory: Path, session_id: str) -> snapshot_mod.Snapshot | None:
    path = snapshot_mod.snapshot_path(directory, session_id)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return msgspec.json.decode(raw, type=snapshot_mod.Snapshot)
    except (msgspec.DecodeError, msgspec.ValidationError):
        return None


def _read_archive_index(path: Path) -> dict[str, tuple[float, str]]:
    """Parse a legacy ``archive.json`` sidecar; corrupt/absent yields ``{}``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, tuple[float, str]] = {}
    for session_id, value in raw.items():
        if not is_valid_session_id(session_id) or not isinstance(value, dict):
            continue
        reason = value.get("reason")
        archived_at = value.get("archived_at")
        if (
            not isinstance(reason, str)
            or reason not in {"auto", "user"}
            or isinstance(archived_at, bool)
            or not isinstance(archived_at, (int, float))
            or not math.isfinite(archived_at)
            or archived_at < 0
        ):
            continue
        result[session_id] = (float(archived_at), reason)
    return result


__all__ = [
    "MAX_IMPORTED_SESSIONS",
    "ImportResult",
    "import_workspace_sessions",
]
