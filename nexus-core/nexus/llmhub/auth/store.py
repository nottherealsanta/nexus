"""Owner-only credential file at `<home>/auth.json`, one entry per provider.

The file and its directory must be private (0600/0700) and owned by the user.
Writes are atomic (temp file + rename). `locked()` is a cross-process lock so
two processes never rotate the same refresh token at once.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ...paths import home

_MAX_BYTES = 256 * 1024


class CredentialError(Exception):
    pass


class CredentialStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or home() / "auth.json"

    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CredentialError(f"{self.path.parent} must be a private directory (chmod 700)")

    def _read_all(self) -> dict[str, Any]:
        self._ensure_dir()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CredentialError(f"{self.path} must be private (chmod 600)")
            raw = handle.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise CredentialError(f"{self.path} is too large")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise CredentialError(f"{self.path} is not valid JSON; log in again") from exc
        return data if isinstance(data, dict) else {}

    def read(self, provider: str) -> dict[str, Any] | None:
        entry = self._read_all().get(provider)
        return entry if isinstance(entry, dict) else None

    def write(self, provider: str, entry: dict[str, Any] | None) -> None:
        data = self._read_all()
        if entry is None:
            data.pop(provider, None)
        else:
            data[provider] = entry
        temp = self.path.parent / f".auth-{secrets.token_hex(6)}.tmp"
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, indent=2)
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    @contextmanager
    def locked(self) -> Iterator[None]:
        self._ensure_dir()
        fd = os.open(self.path.parent / "auth.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
