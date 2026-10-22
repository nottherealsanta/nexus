"""Native launch seam and packaged binary discovery (feasibility §8).

No terminal initialization on import. Installed wheels carry a presentation-only
executable beside Python; source checkouts may use a Cargo build.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys


@dataclass(frozen=True)
class ReloadRequest:
    """Returned only after the native bridge has fully unwound."""
    workspace: Path
    session: str


async def rebuild() -> Path:
    """Return Cargo's actual executable, never binary discovery."""
    manifest = Path(__file__).resolve().parents[3] / "rust/tui/Cargo.toml"
    if not manifest.is_file() or not manifest.with_name("Cargo.lock").is_file():
        raise RuntimeError("/reload requires a source checkout with rust/tui/Cargo.toml and Cargo.lock. Rebuild Nexus manually.")
    print("Rebuilding native TUI…", flush=True)
    try:
        process = await asyncio.create_subprocess_exec(
            "cargo", "build", "--release", "--locked", "--manifest-path", str(manifest),
            "--bin", "nexus-ratatui", "--message-format=json-render-diagnostics",
            stdout=asyncio.subprocess.PIPE, cwd=manifest.parent)
    except OSError as exc:
        raise RuntimeError(f"Cannot run Cargo for /reload: {exc}. Install Rust/Cargo and retry.") from exc
    output, _ = await process.communicate()
    executable = None
    for line in output.decode(errors="replace").splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            print(line)
            continue
        if message.get("reason") == "compiler-message":
            print(message.get("message", {}).get("rendered", ""), end="", file=sys.stderr)
        if (message.get("reason") == "compiler-artifact"
                and message.get("target", {}).get("name") == "nexus-ratatui"
                and message.get("executable")):
            executable = Path(message["executable"])
    if process.returncode or executable is None or not executable.is_file():
        raise RuntimeError("/reload build failed; the workspace daemon was not stopped. Fix the Cargo errors and run nexus chat again.")
    return executable.resolve()


async def restart_workspace(workspace: Path) -> None:
    from nexus.host import daemon
    print(f"Restarting workspace daemon: {workspace}", flush=True)
    report = await daemon.status(workspace)
    pid = report.get("pid") if report.get("running") else None
    stopped = await daemon.stop(workspace)
    if pid and not stopped:
        raise RuntimeError("/reload could not stop daemon. Run nexus daemon stop and relaunch.")
    if not await daemon.wait_stopped(workspace, pid, polls=100, interval=0.1):
        raise RuntimeError("/reload timed out waiting for daemon shutdown. Run nexus daemon stop before relaunching.")


def binary_path() -> Path:
    override = os.environ.get("NEXUS_TUI_BINARY")
    if override:
        candidates = [Path(override)]
    else:
        root = Path(__file__).resolve().parents[3]
        installed = shutil.which("nexus-ratatui")
        profiles = [Path(sys.executable).parent / "nexus-ratatui",
                    root / "rust/tui/target/release/nexus-ratatui"]
        if installed:
            profiles.append(Path(installed))
        # Development: among installed/venv/release builds the most recently built wins, so
        # a fresh `cargo build --release` is picked up on the next launch. The debug build
        # is unoptimized (it redraws at a high idle CPU cost), so it is only a last resort.
        # A stable sort keeps the listed order for equal timestamps.
        profiles.sort(key=lambda path: path.stat().st_mtime if path.is_file() else 0, reverse=True)
        candidates = [*profiles, root / "rust/tui/target/debug/nexus-ratatui"]
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


async def spawn(binary: Path | None = None):
    """Start the native client now, so its splash answers the launch before the host is ready.

    The bridge protocol is unchanged: the process draws "Nexus" until the first snapshot
    reaches its stdin, which the controller sends once the session has loaded.
    """
    return await asyncio.create_subprocess_exec(
        str(binary if binary is not None else binary_path()), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024)


async def progress(process, step: int) -> None:
    """Tell the splash how far host startup got (``{"startup": N}``, milestones 2 and 3).

    Without it the bar waits at the client-only first milestone until the first snapshot,
    however far the host is. Best effort: a client that already left is not an error.
    """
    if process is None or process.returncode is not None:
        return
    with contextlib.suppress(Exception):
        process.stdin.write(f'{{"startup":{step}}}\n'.encode())
        await process.stdin.drain()


#: Bound on the stray-stderr log: past this size it starts over on the next launch.
_STDERR_LOG_LIMIT = 1024 * 1024


@contextlib.contextmanager
def stderr_to(path: Path):
    """Point this process's fd 2 at ``path`` while the native client owns the terminal.

    Native libraries (PortAudio for voice cues and dictation) write warnings straight to
    fd 2, which would print over the Ratatui frame. The client was spawned with the real
    terminal as its stderr, so only the controller's writes move. Restored on exit, so a
    final error still reaches the user. Best effort: an unwritable log leaves fd 2 alone.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if path.is_file() and path.stat().st_size > _STDERR_LOG_LIMIT else "a"
        log = open(path, mode, encoding="utf-8")  # noqa: SIM115 - closed in the finally below
    except OSError:
        yield
        return
    with contextlib.suppress(Exception):
        sys.stderr.flush()
    saved = os.dup(2)
    try:
        os.dup2(log.fileno(), 2)
        yield
    finally:
        with contextlib.suppress(Exception):
            sys.stderr.flush()
        os.dup2(saved, 2)
        os.close(saved)
        log.close()


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
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


async def run(client, *, session="default", reconnect=None, workspace=None, process=None):
    from .prototype import run as run_native
    return await run_native(Path(workspace or Path.cwd()).resolve(), session, binary_path(), client=client, reconnect=reconnect, process=process)
