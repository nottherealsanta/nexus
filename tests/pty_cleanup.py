"""Teardown for PTY tests that spawn the native client through a `setsid` wrapper.

The wrapper calls `os.setsid()`, so its pid is also its process-group id. Killing only the
wrapper orphans anything it started (the binary under `subprocess.run`, a `child.py` bridge
helper) and an orphan on a dead terminal used to spin forever; kill the whole group.
"""
import os
import signal
import subprocess


def kill_group(process: subprocess.Popen, timeout: float = 3) -> None:
    """SIGKILL the wrapper's process group (even if the wrapper already exited) and reap it."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
