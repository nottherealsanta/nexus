"""Secure, bounded credential persistence for provider sign-in.

For ChatGPT OAuth only a rotating refresh token and routing metadata are
persisted; access and ID tokens deliberately never cross this boundary. Other
providers (GitHub Copilot, OpenCode Go) keep one opaque secret per
``<provider>:<profile>`` account in the same secure native keychain.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import stat
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..errors import ProviderError

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


class KeyringCredentialStore:
    """A keyring adapter that refuses keyring's null, plaintext, and fail backends."""

    def __init__(self, keyring_module=None, *, lock_dir: Path | None = None, lock_timeout: float = 5.0) -> None:
        self._keyring = keyring_module
        self._lock_dir = lock_dir or Path.home() / ".nexus" / "locks"
        self._lock_timeout = lock_timeout

    @staticmethod
    def _secure_backend(backend) -> bool:
        name = f"{type(backend).__module__}.{type(backend).__name__}".lower()
        priority = getattr(backend, "priority", 0)
        if not isinstance(priority, (int, float)) or priority <= 0:
            return False
        if "chainer" in name:
            backends = getattr(backend, "backends", None)
            return isinstance(backends, (list, tuple)) and bool(backends) and all(
                KeyringCredentialStore._secure_backend(item) for item in backends
            )
        return "keyring.backends.macos" in name or "keyring.backends.secretservice" in name

    def _backend(self):
        if self._keyring is None:
            try:
                import keyring  # lazy: ordinary model/API-key users never import it
            except ImportError as exc:
                raise ProviderError("ChatGPT OAuth requires the keyring package and a secure native keychain") from exc
            self._keyring = keyring
        backend = self._keyring.get_keyring()
        if not self._secure_backend(backend):
            raise ProviderError("ChatGPT OAuth requires a secure native keychain (macOS Keychain or Secret Service); insecure keyring backends are refused")
        return self._keyring

    async def read(self, profile: str) -> CredentialRecord | None:
        validate_profile(profile)
        value = await asyncio.to_thread(self._backend().get_password, SERVICE, f"codex:{profile}")
        return CredentialRecord.parse(value) if value else None

    async def write(self, profile: str, record: CredentialRecord) -> None:
        validate_profile(profile)
        await asyncio.to_thread(self._backend().set_password, SERVICE, f"codex:{profile}", record.serialize())

    async def delete(self, profile: str) -> None:
        validate_profile(profile)
        keyring = self._backend()
        try:
            await asyncio.to_thread(keyring.delete_password, SERVICE, f"codex:{profile}")
        except keyring.errors.PasswordDeleteError:
            pass

    @asynccontextmanager
    async def lock(self, profile: str):
        """A bounded, cancellable, cross-process lock for one credential profile."""
        validate_profile(profile)
        self._lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = await asyncio.to_thread(os.stat, self._lock_dir)
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


class KeyringSecretStore(KeyringCredentialStore):
    """Provider secrets in the same secure native keychain as ChatGPT OAuth.

    Values are opaque strings bounded to 8 KiB; insecure keyring backends are
    refused exactly as for the Codex record.
    """

    async def read_secret(self, account: str) -> str | None:
        validate_account(account)
        value = await asyncio.to_thread(self._backend().get_password, SECRET_SERVICE, account)
        if value is not None and (not isinstance(value, str) or len(value.encode()) > _MAX_SECRET_BYTES):
            raise ProviderError("stored provider credential is invalid; sign in again")
        return value or None

    async def write_secret(self, account: str, value: str) -> None:
        validate_account(account)
        if not isinstance(value, str) or not value or len(value.encode()) > _MAX_SECRET_BYTES:
            raise ValueError("provider credential must be a nonempty string under 8 KiB")
        await asyncio.to_thread(self._backend().set_password, SECRET_SERVICE, account, value)

    async def delete_secret(self, account: str) -> None:
        validate_account(account)
        keyring = self._backend()
        try:
            await asyncio.to_thread(keyring.delete_password, SECRET_SERVICE, account)
        except keyring.errors.PasswordDeleteError:
            pass
