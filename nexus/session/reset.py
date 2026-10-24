"""One-time "back up the state database and start a fresh one" reset (schema 10).

Schema version 10 is a *reset version*: a database whose ``user_version`` is
between 1 and 9 is not migrated. It is backed up and replaced by an empty
database. Steps, run once under a home-level ``flock`` before anything uses the
database:

1. Take exclusive ownership of the old file. A connection switches the journal to
   ``DELETE`` in ``locking_mode=EXCLUSIVE``; SQLite refuses that while any other
   connection (an old daemon, another process, another thread) has the file open,
   and the reset stops with a "stop the old daemon" message.
2. Write one self-contained backup (SQLite online backup API, WAL folded in) to
   ``<home>/backups/nexus-v<old>-<UTC YYYYMMDDTHHMMSSZ>.db``, dir ``0700``, file
   ``0600``, never overwriting an existing file, bounded by ``BACKUP_TIMEOUT_SECONDS``.
3. Verify it (``PRAGMA quick_check`` and the ``records`` row count).
4. Only then delete ``nexus.db`` and its ``-wal``/``-shm`` files.

Any failure before step 4 deletes the partial backup, leaves the original
untouched and raises :class:`~nexus.errors.SessionError`. The caller records the
returned :class:`ResetInfo` in the new database's ``meta`` table, and the first
``nexus chat``/``nexus run`` after it announces the backup once
(:func:`claim_notice`). Backups are never deleted automatically.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..errors import SessionError
from .lock import _open_lock_file

#: Upper bound for copying the old database; a stuck disk fails the reset
#: instead of hanging daemon startup.
BACKUP_TIMEOUT_SECONDS = 600.0
#: How long to wait for another process to release the old database.
OPEN_HANDLE_TIMEOUT_SECONDS = 1.0
#: How long to wait for a concurrent reset (another starting daemon).
RESET_LOCK_TIMEOUT_SECONDS = 30.0
BACKUPS_DIRNAME = "backups"

META_BACKUP_PATH = "reset.backup_path"
META_OLD_VERSION = "reset.old_version"
META_AT = "reset.at"
META_NOTICE_SHOWN = "reset.notice_shown"


@dataclass(frozen=True, slots=True)
class ResetInfo:
    backup_path: str
    old_version: int
    at: str


def peek_version(path: Path) -> int:
    """``user_version`` of ``path`` (0 for a missing or empty file); read-only."""
    if not path.exists() or path.stat().st_size == 0:
        return 0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        raise SessionError(f"state database is unreadable: {path}") from exc
    finally:
        conn.close()


@contextlib.contextmanager
def reset_lock(path: Path):
    """Home-level cross-process lock so two starting daemons never reset twice."""
    handle = _open_lock_file(path.with_name(path.name + ".reset.lock"))
    try:
        deadline = time.monotonic() + RESET_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise SessionError(
                        "another Nexus process is resetting the state database; retry shortly"
                    ) from exc
                time.sleep(0.1)
        yield
    finally:
        handle.close()


def _reserve_backup_file(directory: Path, old_version: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"-{attempt}"
        target = directory / f"nexus-v{old_version}-{stamp}{suffix}.db"
        try:
            os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            continue
        return target
    raise SessionError(f"could not choose an unused backup name in {directory}")


def _records_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])


def _copy_and_verify(source: sqlite3.Connection, target: Path) -> None:
    deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("backup timed out")

    dest = sqlite3.connect(str(target))
    try:
        source.backup(dest, pages=1024, progress=progress)
        dest.commit()
        check = dest.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise SessionError("backup failed integrity check (quick_check)")
        if _records_count(dest) != _records_count(source):
            raise SessionError("backup record count does not match the source")
    finally:
        dest.close()
    fd = os.open(target, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup_and_remove(path: Path, old_version: int) -> ResetInfo:
    """Back up ``path`` (schema ``old_version``), verify, then delete it."""
    hint = (
        "Stop the old Nexus daemon (run `nexus daemon stop` with the old install) and retry."
    )
    source = sqlite3.connect(str(path), timeout=OPEN_HANDLE_TIMEOUT_SECONDS, isolation_level=None)
    backup: Path | None = None
    try:
        try:
            source.execute("PRAGMA locking_mode=EXCLUSIVE")
            source.execute("PRAGMA journal_mode=DELETE")  # needs sole ownership; folds WAL in
            source.execute("BEGIN EXCLUSIVE")
            source.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            raise SessionError(
                f"the old Nexus database is still open by another process ({path}). {hint}"
            ) from exc
        try:
            backup = _reserve_backup_file(path.parent / BACKUPS_DIRNAME, old_version)
            _copy_and_verify(source, backup)
        except SessionError:
            raise
        except (OSError, sqlite3.Error, TimeoutError) as exc:
            raise SessionError(
                f"could not back up the old Nexus database ({type(exc).__name__}: {exc}); "
                "nothing was changed"
            ) from exc
        for name in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path.with_name(path.name + name))
    except BaseException:
        if backup is not None and path.exists():
            with contextlib.suppress(OSError):
                backup.unlink()
        raise
    finally:
        source.close()
    return ResetInfo(
        backup_path=str(backup),
        old_version=old_version,
        at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def record(conn: sqlite3.Connection, info: ResetInfo) -> None:
    """Store ``info`` in ``meta`` (inside the caller's transaction)."""
    rows = [
        (META_BACKUP_PATH, info.backup_path), (META_OLD_VERSION, str(info.old_version)),
        (META_AT, info.at), (META_NOTICE_SHOWN, "0"),
    ]
    conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", rows)


def read_reset(path: Path) -> dict[str, object] | None:
    """The recorded reset (path, old version, time, size, notice state) or ``None``."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta WHERE key LIKE 'reset.%'").fetchall())
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    backup = meta.get(META_BACKUP_PATH)
    if not backup:
        return None
    try:
        size = os.stat(backup).st_size
    except OSError:
        size = None
    try:
        old_version = int(meta.get(META_OLD_VERSION, ""))
    except ValueError:
        old_version = None
    return {
        "backup_path": backup, "old_version": old_version, "at": meta.get(META_AT, ""),
        "size_bytes": size, "notice_shown": meta.get(META_NOTICE_SHOWN) == "1",
    }


def claim_notice(path: Path) -> str:
    """The one-time notice text, marking it shown; ``""`` when none is pending."""
    info = read_reset(path)
    if info is None or info["notice_shown"]:
        return ""
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        claimed = conn.execute(
            "UPDATE meta SET value='1' WHERE key=? AND value!='1'", (META_NOTICE_SHOWN,)
        ).rowcount
        conn.execute("COMMIT")
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    if not claimed:
        return ""
    return (
        "Nexus started a new session database. "
        f"Your old sessions are backed up at {info['backup_path']}."
    )
