"""GPUI desktop launch seam; host-only workflows reuse the native bridge.

The desktop is a presentation client, never a runtime or session-file reader.
See docs/desktop.md for the bridge contract and source build instructions.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


def binary_path() -> Path:
    override = os.environ.get("NEXUS_DESKTOP_BINARY")
    if override:
        candidates = [Path(override)]
    else:
        root = Path(__file__).resolve().parents[3]
        installed = shutil.which("nexus-desktop")
        candidates = [Path(sys.executable).parent / "nexus-desktop",
                      *(root / f"rust/desktop/target/{profile}/nexus-desktop"
                        for profile in ("release", "debug"))]
        if installed:
            candidates.append(Path(installed))
        candidates.sort(key=lambda path: path.stat().st_mtime if path.is_file() else 0, reverse=True)
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    raise RuntimeError("Desktop executable is missing. Build it with `cargo build --manifest-path rust/desktop/Cargo.toml` or set NEXUS_DESKTOP_BINARY.")


async def run(workspace: Path, *, session: str, client=None, reconnect=None) -> int:
    from ..ratatui.prototype import run as run_native

    return await run_native(workspace.resolve(), session, binary_path(), client=client, reconnect=reconnect, desktop=True)
