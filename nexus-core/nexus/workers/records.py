"""Durable worker records and orphan detection (zmx-design 4.7).

One small file per session under `$NEXUS10_HOME/workers/records/` says which CLI process Nexus started for a turn:
pid, process start time, CLI session id and transcript path. Recovery reads it to finish a crashed turn from the CLI's
own transcript, and to offer (never perform) cleanup of a process that outlived the daemon. A pid is never killed on
its own: the recorded start time must still match, and nothing is killed automatically.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

from ..paths import home
from .base import WorkerError
from .claude_code import private_directory

_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def records_dir() -> Path:
    return private_directory(private_directory(home() / "workers") / "records")


def _path(session_id: str) -> Path:
    if not _SAFE.fullmatch(session_id):
        raise WorkerError("invalid session id for a worker record")
    return records_dir() / f"{session_id}.json"


def process_start(pid: int) -> str | None:
    """The kernel's start time text for `pid`, or None when no such process exists."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False,
                             env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"}).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def write(session_id: str, data: dict[str, Any]) -> None:
    path = _path(session_id)
    temporary = path.with_name(path.name + f".{os.getpid()}.part")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump({**data, "session_id": session_id}, out)
    os.replace(temporary, path)


def read(session_id: str) -> dict[str, Any] | None:
    try:
        value = json.loads(_path(session_id).read_bytes()[:65536])
    except (OSError, ValueError, WorkerError):
        return None
    return value if isinstance(value, dict) else None


def clear(session_id: str) -> None:
    try:
        _path(session_id).unlink()
    except (OSError, WorkerError):
        pass


def all_records() -> list[dict[str, Any]]:
    try:
        names = sorted(p.stem for p in records_dir().glob("*.json"))
    except (OSError, WorkerError):
        return []
    return [r for n in names if (r := read(n))]


def orphans() -> list[dict[str, Any]]:
    """Records whose process is still alive with the same start time (pid reuse does not match)."""
    found = []
    for record in all_records():
        pid, started = record.get("pid"), record.get("started")
        if isinstance(pid, int) and started and process_start(pid) == started:
            found.append(record)
    return found


def terminate(record: dict[str, Any]) -> bool:
    """TERM the recorded process group, only while pid and start time still match. The caller must be a user action."""
    pid = record.get("pid")
    if not isinstance(pid, int) or process_start(pid) != record.get("started"):
        return False
    try:
        if os.getpgid(pid) != pid:  # Nexus starts every worker as its own session/group leader
            return False
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return False
    return True
