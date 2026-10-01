"""Pasted provider API keys kept in the private credential file (OpenCode Go).

A key is written once by the host (``ProviderKeySet``) and read by the
adapter on each request, so a replaced or removed key applies without a
restart. It is never returned to a client, logged, or written to config.
"""
from __future__ import annotations

from collections.abc import Mapping

from ..errors import ProviderError
from .store import FileSecretStore, SecretStore, validate_profile

_MIN_KEY, _MAX_KEY = 8, 512


def validate_api_key(value: object) -> str:
    """A single-line printable token; the message never echoes the value."""
    key = value.strip() if isinstance(value, str) else ""
    if not _MIN_KEY <= len(key) <= _MAX_KEY or not all(33 <= ord(char) <= 126 for char in key):
        raise ValueError("API key must be 8-512 printable characters without spaces")
    return key


class StoredKeyAuth:
    def __init__(self, provider: str, *, profile: str = "default", store: SecretStore | None = None) -> None:
        self.provider = provider
        self.profile = validate_profile(profile)
        self._store = store or FileSecretStore()

    @property
    def account(self) -> str:
        return f"{self.provider}:{self.profile}"

    async def status(self) -> bool:
        return await self._store.read_secret(self.account) is not None

    async def save(self, key: str) -> None:
        await self._store.write_secret(self.account, validate_api_key(key))

    async def logout(self) -> None:
        await self._store.delete_secret(self.account)

    async def headers(self) -> Mapping[str, str]:
        key = await self._store.read_secret(self.account)
        if key is None:
            raise ProviderError(f"{self.provider}: add an API key in Settings → Providers")
        return {"authorization": f"Bearer {key}"}


__all__ = ["StoredKeyAuth", "validate_api_key"]
