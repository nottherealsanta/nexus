"""Offline database reclamation, with a verified backup and atomic replacement."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from .store import SCHEMA_VERSION, Store, StoreError, database_lock

CANONICAL = ("session", "message", "session_snapshot", "turn", "turn_request", "turn_tool", "session_usage")


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    return {table: db.execute(f"select count(*) from {table}").fetchone()[0] for table in CANONICAL}


def compact_database(path: Path, *, apply: bool = False) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"no database at {path}")
    # The dry-run is read-only, including no additive migrations or WAL checkpoint.
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if db.execute("pragma user_version").fetchone()[0] != SCHEMA_VERSION:
            raise StoreError("offline compaction requires the current Nexus database schema")
        page = db.execute("pragma page_size").fetchone()[0]
        result: dict[str, Any] = {
            "dry_run": not apply, "path": str(path), "file_bytes": path.stat().st_size,
            "free_bytes": page * db.execute("pragma freelist_count").fetchone()[0],
            "counts": _counts(db),
            "note": "Finished sessions will lose diagnostic events; canonical state is retained. "
                    "Requires a stopped daemon and free disk for a full backup plus the new database.",
        }
    finally:
        db.close()
    if not apply:
        return result
    lock = database_lock(path, exclusive=True)
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}")
    building = path.with_name(path.name + ".new")
    store = None
    try:
        if building.exists():
            raise StoreError(f"{building} already exists; inspect it before retrying")
        # Backup before even additive migrations touch the source.
        source = sqlite3.connect(path)
        try:
            with sqlite3.connect(backup) as saved:
                source.backup(saved)
                if saved.execute("pragma integrity_check").fetchone()[0] != "ok":
                    raise StoreError("backup failed integrity_check")
        finally:
            source.close()
        store = Store(path, _locked=True)
        generation = store.log_generation()
        counts = _counts(store.db)
        removed = 0
        for row in store.db.execute("select id from session").fetchall():
            sid = row[0]
            store._migrate_session_lifecycle(sid)
            while info := store.compact_session(sid, now=time.time(), idle_seconds=0, batch_size=5000):
                removed += info["removed"]
        while store.deduplicate_requests(limit=50):
            pass
        store.db.commit()
        store.db.execute("pragma wal_checkpoint(truncate)").fetchall()
        store.db.execute("pragma auto_vacuum = incremental")
        store.db.execute("vacuum into ?", (str(building),))
        verified = sqlite3.connect(building)
        try:
            if verified.execute("pragma integrity_check").fetchone()[0] != "ok":
                raise StoreError("compacted database failed integrity_check")
            if _counts(verified) != counts:
                raise StoreError("canonical record counts changed during compaction")
            if verified.execute("select generation from log where id = 1").fetchone()[0] != generation:
                raise StoreError("log generation changed during compaction")
            if verified.execute("pragma auto_vacuum").fetchone()[0] != 2:
                raise StoreError("compacted database did not enable incremental vacuum")
        finally:
            verified.close()
        store.close()
        store = None
        # Source is checkpointed and all cooperating users are excluded. Never
        # leave stale WAL files alongside the replacement database.
        for suffix in ("-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        with building.open("rb") as ready:
            os.fsync(ready.fileno())
        os.replace(building, path)
        return {**result, "dry_run": False, "backup": str(backup), "removed_events": removed,
                "after_bytes": path.stat().st_size, "integrity_check": "ok", "generation": generation}
    finally:
        if store is not None:
            store.close()
        lock.close()
