"""Doctor caches its heavy SQLite checks per store (idle-lag fix)."""
from __future__ import annotations

from pathlib import Path

from nexus.host_support import doctor


class _Db:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    def schema_version(self) -> int:
        return 1

    def quick_check(self) -> str:
        self.calls += 1
        return "ok"


class _Store:
    def __init__(self, db: _Db) -> None:
        self.db = db

    def tail_bytes(self, *_a, **_k) -> bytes:
        return b""


def test_quick_check_is_cached_until_ttl(tmp_path, monkeypatch):
    doctor._heavy_cache.clear()
    store = _Store(_Db(tmp_path / "x.db"))
    assert doctor.database_diagnostics(store)["quick_check"] == "ok"
    assert doctor.database_diagnostics(store)["quick_check"] == "ok"
    assert store.db.calls == 1
    monkeypatch.setattr(doctor, "HEAVY_TTL", 0.0)
    doctor.database_diagnostics(store)
    assert store.db.calls == 2
