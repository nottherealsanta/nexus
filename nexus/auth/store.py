"""Private file-backed provider credentials (plan section 7).

OAuth persists only refresh tokens and routing metadata. Credentials live in
~/.nexus/credentials.json, never config, logs or host responses. Atomic writes
and a bounded cross-process lock protect concurrent daemon and CLI updates.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..errors import ProviderError
from ..config.paths import nexus_home

SERVICE = "nexus.chatgpt-oauth"
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MAX_RECORD_BYTES = 16_384


def validate_profile(profile: str) -> str:
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
        raise ValueError("OAuth profile must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return profile


@dataclass(frozen=True, repr=False)
class CredentialRecord:
    refresh_token: str
    account_id: str | None
    residency: str | None = None
    generation: int = 0
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1 or not isinstance(self.generation, int) or self.generation < 0:
            raise ValueError("invalid OAuth credential record")
        if not isinstance(self.refresh_token, str) or not self.refresh_token or len(self.refresh_token) > 4096:
            raise ValueError("invalid OAuth credential record")
        if self.account_id is not None and (not isinstance(self.account_id, str) or not self.account_id or len(self.account_id) > 4096):
            raise ValueError("invalid OAuth credential record")
        if self.residency is not None and (not isinstance(self.residency, str) or len(self.residency) > 256):
            raise ValueError("invalid OAuth credential record")

    def __repr__(self) -> str:
        return f"CredentialRecord(version={self.version}, generation={self.generation}, refresh_token='***', account_id='***', residency={'***' if self.residency else None!r})"

    def serialize(self) -> str:
        return json.dumps({"v": 1, "refresh_token": self.refresh_token, "account_id": self.account_id, "residency": self.residency, "generation": self.generation}, separators=(",", ":"))

    @classmethod
    def parse(cls, value: str) -> CredentialRecord:
        if not isinstance(value, str) or len(value.encode()) > _MAX_RECORD_BYTES:
            raise ValueError("invalid OAuth credential record")
        try:
            data = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid OAuth credential record") from exc
        if not isinstance(data, dict) or set(data) != {"v", "refresh_token", "account_id", "residency", "generation"}:
            raise ValueError("invalid OAuth credential record")
        return cls(version=data["v"], refresh_token=data["refresh_token"], account_id=data["account_id"], residency=data["residency"], generation=data["generation"])


class CredentialStore(Protocol):
    async def read(self, profile: str) -> CredentialRecord | None: ...
    async def write(self, profile: str, record: CredentialRecord) -> None: ...
    async def delete(self, profile: str) -> None: ...
    @asynccontextmanager
    async def lock(self, profile: str): ...


_MAX_FILE_BYTES = 1024 * 1024


class FileCredentialStore:
    """Owner-only JSON persistence; no keychain reads or fallback."""

    def __init__(self, *, path: Path | None = None, lock_dir: Path | None = None, lock_timeout: float = 5.0) -> None:
        self._path = path or nexus_home() / "credentials.json"
        self._lock_dir = lock_dir or self._path.parent / "locks"
        self._lock_timeout = lock_timeout

    @staticmethod
    def _private(info, *, directory: bool = False) -> None:
        valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not valid or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProviderError("credential storage is not private")

    def _directory(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._private(self._path.parent.lstat(), directory=True)

    def _read_data(self) -> dict[str, str]:
        self._directory()
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as handle:
            self._private(os.fstat(handle.fileno()))
            raw = handle.read(_MAX_FILE_BYTES + 1)
        try:
            if len(raw) > _MAX_FILE_BYTES:
                raise ValueError
            data = json.loads(raw)
            if not isinstance(data, dict) or not all(
                isinstance(k, str) and isinstance(v, str) and len(v.encode()) <= _MAX_RECORD_BYTES
                for k, v in data.items()
            ):
                raise ValueError
            return data
        except (ValueError, UnicodeError) as exc:
            raise ProviderError("stored credentials are invalid; sign in again") from exc

    def _get(self, service: str, account: str) -> str | None:
        return self._read_data().get(f"{service}/{account}")

    def _mutate(self, service: str, account: str, value: str | None) -> None:
        import fcntl

        self._directory()
        lock_path = self._path.parent / "credentials.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        temporary = self._path.parent / f".credentials-{secrets.token_hex(8)}.tmp"
        try:
            self._private(os.fstat(fd))
            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise ProviderError("credential storage is busy; try again") from exc
                    time.sleep(0.05)
            data = self._read_data()
            key = f"{service}/{account}"
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
            raw = json.dumps(data, separators=(",", ":")).encode()
            if len(raw) > _MAX_FILE_BYTES:
                raise ProviderError("credential storage is full")
            out = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)
            os.close(fd)

    def _set(self, service: str, account: str, value: str) -> None:
        self._mutate(service, account, value)

    def _delete(self, service: str, account: str) -> None:
        self._mutate(service, account, None)

    async def read(self, profile: str) -> CredentialRecord | None:
        validate_profile(profile)
        value = await asyncio.to_thread(self._get, SERVICE, f"codex:{profile}")
        return CredentialRecord.parse(value) if value else None

    async def write(self, profile: str, record: CredentialRecord) -> None:
        validate_profile(profile)
        await asyncio.to_thread(self._set, SERVICE, f"codex:{profile}", record.serialize())

    async def delete(self, profile: str) -> None:
        validate_profile(profile)
        await asyncio.to_thread(self._delete, SERVICE, f"codex:{profile}")

    @asynccontextmanager
    async def lock(self, profile: str):
        """A bounded, cancellable, cross-process lock for one credential profile."""
        validate_profile(profile)
        self._lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = await asyncio.to_thread(os.lstat, self._lock_dir)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o077:
            raise ProviderError("codex: OAuth lock directory is not private")
        name = hashlib.sha256(profile.encode("utf-8")).hexdigest()
        path = self._lock_dir / f"codex-{name}.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = await asyncio.to_thread(os.open, path, flags, 0o600)
        try:
            import fcntl
            info = await asyncio.to_thread(os.fstat, fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ProviderError("codex: OAuth lock file is unsafe")
            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    if time.monotonic() >= deadline:
                        raise ProviderError("codex: OAuth credential is busy; try again") from exc
                    await asyncio.sleep(0.05)
            yield
        finally:
            try:
                import fcntl
                await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_UN)
            finally:
                await asyncio.to_thread(os.close, fd)


SECRET_SERVICE = "nexus.provider-credentials"
_ACCOUNT = re.compile(r"[a-z0-9][a-z0-9-]{0,31}:[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MAX_SECRET_BYTES = 8_192


class SecretStore(Protocol):
    """Opaque per-account secrets (a Copilot token, a pasted API key)."""

    async def read_secret(self, account: str) -> str | None: ...
    async def write_secret(self, account: str, value: str) -> None: ...
    async def delete_secret(self, account: str) -> None: ...


def validate_account(account: str) -> str:
    """``<provider>:<profile>``; the account name never carries a secret."""
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        raise ValueError("credential account must be '<provider>:<profile>'")
    return account


class FileSecretStore(FileCredentialStore):
    """Opaque provider secrets in private local credential storage."""

    async def read_secret(self, account: str) -> str | None:
        validate_account(account)
        value = await asyncio.to_thread(self._get, SECRET_SERVICE, account)
        if value is not None and (not isinstance(value, str) or len(value.encode()) > _MAX_SECRET_BYTES):
            raise ProviderError("stored provider credential is invalid; sign in again")
        return value or None

    async def write_secret(self, account: str, value: str) -> None:
        validate_account(account)
        if not isinstance(value, str) or not value or len(value.encode()) > _MAX_SECRET_BYTES:
            raise ValueError("provider credential must be a nonempty string under 8 KiB")
        await asyncio.to_thread(self._set, SECRET_SERVICE, account, value)

    async def delete_secret(self, account: str) -> None:
        validate_account(account)
        await asyncio.to_thread(self._delete, SECRET_SERVICE, account)

# Compatibility for callers importing the previous store names.
KeyringCredentialStore = FileCredentialStore
KeyringSecretStore = FileSecretStore
