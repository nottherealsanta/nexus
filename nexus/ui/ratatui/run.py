"""Native launch seam and packaged binary discovery (feasibility §8).

No terminal initialization on import. Installed wheels carry a presentation-only
executable beside Python; source checkouts may use a Cargo build.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


def binary_path() -> Path:
    override = os.environ.get("NEXUS_TUI_BINARY")
    if override:
        candidates = [Path(override)]
    else:
        root = Path(__file__).resolve().parents[3]
        installed = shutil.which("nexus-ratatui")
        candidates = [Path(sys.executable).parent / "nexus-ratatui",
                      root / "rust/tui/target/debug/nexus-ratatui",
                      root / "rust/tui/target/release/nexus-ratatui"]
        if installed:
            candidates.append(Path(installed))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    raise RuntimeError("Native TUI executable is missing. Reinstall the Nexus wheel or build the source with `cargo build --manifest-path rust/tui/Cargo.toml`.")


def available() -> bool:
    """Whether the native executable can be launched on this machine."""
    try:
        binary_path()
    except RuntimeError:
        return False
    return True


async def run(client, *, session="default", reconnect=None, workspace=None):
    from .prototype import run as run_native
    return await run_native(Path(workspace or Path.cwd()).resolve(), session, binary_path(), client=client, reconnect=reconnect)
