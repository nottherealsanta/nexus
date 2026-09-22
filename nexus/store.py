"""Atomic session snapshots with an OS lock held for the entire turn.

Locks are automatically released on process death. macOS and Linux supported.
"""
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .context import Exchange
from .errors import SessionBusy

__all__ = ["SessionBusy", "SessionStore"]


class SessionStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, session: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", session):
            raise ValueError("Session ID must be 1-80 letters, digits, underscores or hyphens, starting with a letter or digit")
        return self.directory / f"{session}.json"

    @contextmanager
    def lock(self, session: str):
        # Imported lazily so ``import nexus`` does not pull the platform locking
        # module on the legacy Agent path; locking is only needed at use time.
        import fcntl

        path = self._path(session)
        self.directory.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SessionBusy(f"Session {session!r} is already running") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self, session: str) -> list[Exchange]:
        path = self._path(session)
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("exchanges"), list):
            raise ValueError(f"Unsupported session format: {path}")
        result = []
        for row in data["exchanges"]:
            if not isinstance(row, dict) or set(row) != {"user", "assistant"} or not all(isinstance(v, str) for v in row.values()):
                raise ValueError(f"Invalid exchange in {path}")
            result.append(Exchange(**row))
        return result

    def save(self, session: str, history: list[Exchange]) -> None:
        """Caller must hold lock(session)."""
        path = self._path(session)
        payload = {"version": 1, "exchanges": [asdict(e) for e in history]}
        fd, temp = tempfile.mkstemp(dir=self.directory, prefix=".session-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
