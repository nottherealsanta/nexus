"""Native launch seam and packaged binary discovery (feasibility §8).

No terminal initialization on import. Installed wheels carry a presentation-only
executable beside Python; source checkouts may use a Cargo build.
"""
from __future__ import annotations

import asyncio
import contextlib
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
                      *(root / f"rust/tui/target/{profile}/nexus-ratatui" for profile in ("release", "debug"))]
        if installed:
            candidates.append(Path(installed))
        # Development: the most recently built executable wins, so a fresh `cargo build`
        # is picked up on the next launch without copying it anywhere. A stable sort keeps
        # the listed order for equal timestamps.
        candidates.sort(key=lambda path: path.stat().st_mtime if path.is_file() else 0, reverse=True)
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


async def spawn():
    """Start the native client now, so its splash answers the launch before the host is ready.

    The bridge protocol is unchanged: the process draws "Nexus" until the first snapshot
    reaches its stdin, which the controller sends once the session has loaded.
    """
    return await asyncio.create_subprocess_exec(
        str(binary_path()), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024)


async def release(process) -> None:
    """Stop a spawned client that never reached (or already left) the bridge loop.

    Closing stdin makes it restore the terminal itself; a kill would leave raw mode on.
    """
    if process.returncode is None:
        with contextlib.suppress(Exception):
            process.stdin.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), 2)
        if process.returncode is None:
            process.terminate()
            await process.wait()


async def run(client, *, session="default", reconnect=None, workspace=None, process=None):
    from .prototype import run as run_native
    return await run_native(Path(workspace or Path.cwd()).resolve(), session, binary_path(), client=client, reconnect=reconnect, process=process)
