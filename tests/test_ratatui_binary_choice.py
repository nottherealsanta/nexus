"""binary_path prefers release/installed builds over a fresher debug build."""
from __future__ import annotations

import os

from nexus.ui.ratatui import run


def test_debug_build_is_last_resort(tmp_path, monkeypatch):
    root = run.Path(run.__file__).resolve().parents[3]
    release = root / "rust/tui/target/release/nexus-ratatui"
    debug = root / "rust/tui/target/debug/nexus-ratatui"
    monkeypatch.delenv("NEXUS_TUI_BINARY", raising=False)
    monkeypatch.setattr(run.shutil, "which", lambda _n: None)
    monkeypatch.setattr(run.sys, "executable", str(tmp_path / "bin" / "python"))
    stat_times = {release: 1, debug: 99}

    def fake_is_file(self):
        return self in stat_times
    monkeypatch.setattr(run.Path, "is_file", fake_is_file)
    monkeypatch.setattr(os, "access", lambda *_a: True)

    class _S:
        def __init__(self, t): self.st_mtime = t
    monkeypatch.setattr(run.Path, "stat", lambda self, *a, **k: _S(stat_times[self]))
    assert run.binary_path() == release.resolve()
    stat_times.pop(release)
    assert run.binary_path() == debug.resolve()
