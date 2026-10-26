"""Nexus core stays independent of the NSH clients, and the clients of Nexus internals.

(a) Only `nexus/host/nsh_static.py` may know where the NSH bundle lives.
(b) The NSH app (`rust/app/src`) talks HTTP only: no files, no database.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEXUS = ROOT / "nexus"
APP_SRC = ROOT / "rust" / "app" / "src"

CORE_FORBIDDEN = ("ui/nsh", "NSH_ROOT", "rust/app", '"nsh"')
APP_FORBIDDEN = (".nexus/", "nexus.db", "sqlite", "std::fs")


def _hits(files, needles):
    out = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        out += [f"{path.relative_to(ROOT)}: {n}" for n in needles if n in text]
    return out


def test_nexus_package_does_not_reference_the_nsh_bundle():
    files = [p for p in NEXUS.rglob("*.py") if p.name != "nsh_static.py" or p.parent.name != "host"]
    assert not _hits(files, CORE_FORBIDDEN)


def test_nsh_app_uses_http_api_only():
    files = list(APP_SRC.rglob("*.rs"))
    assert files
    assert not _hits(files, APP_FORBIDDEN)
