"""Pasted API keys kept in the private credential file, one class per provider.

`$<ENV>` is used when set and nothing is stored. The key is read with no echo
and saved only in `auth.json`. Adapted from the old OpenCode Go sign-in
(PLAN.md J1); old Nexus `auth/api_key.py` kept keys in a keychain store.
"""
from __future__ import annotations

import getpass
import os
from collections.abc import Callable

from .errors import AuthError
from .store import CredentialStore


class ApiKeyAuth:
    methods = ("api_key",)
    provider = ""
    env = ""
    hint = ""
    user_agent = "nexus/0.10"

    def __init__(self, store: CredentialStore | None = None) -> None:
        self.store = store or CredentialStore()

    def _key(self) -> str | None:
        entry = self.store.read(self.provider)
        return (entry or {}).get("api_key") or os.environ.get(self.env) or None

    def logged_in(self) -> bool:
        return self._key() is not None

    def key_source(self) -> str | None:
        """`stored` (auth.json), `env` (`$<ENV>`, used only when nothing is stored) or None."""
        if (self.store.read(self.provider) or {}).get("api_key"):
            return "stored"
        return "env" if os.environ.get(self.env) else None

    def logout(self) -> None:
        with self.store.locked():
            self.store.write(self.provider, None)

    async def headers(self) -> dict[str, str]:
        key = self._key()
        if not key:
            raise AuthError(f"{self.provider}: no API key; run `nexus login {self.provider}` or set {self.env}")
        return {"authorization": f"Bearer {key}", "user-agent": self.user_agent}

    async def login(self, method: str = "api_key", notify: Callable[[str], None] = print) -> None:
        notify(f"Paste your {self.provider} API key ({self.hint}). Input is hidden.")
        self.set_key(getpass.getpass("API key: ").strip())

    def set_key(self, key: str) -> None:
        if not key or len(key) > 4096 or any(ch.isspace() for ch in key):
            raise AuthError(f"{self.provider}: that does not look like an API key")
        with self.store.locked():
            self.store.write(self.provider, {"api_key": key})


def api_key_auth(provider: str, env: str, hint: str) -> type[ApiKeyAuth]:
    return type(f"{provider.title()}Auth", (ApiKeyAuth,), {"provider": provider, "env": env, "hint": hint})
