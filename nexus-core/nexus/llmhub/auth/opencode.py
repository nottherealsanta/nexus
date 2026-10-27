"""OpenCode Go: an API key from the subscription (opencode.ai/auth), pasted once.

`$OPENCODE_API_KEY` is used when set and nothing is stored. The key is read
with no echo and saved only in the private credential file.
"""
from __future__ import annotations

import getpass
import os
from collections.abc import Callable

from .errors import AuthError
from .store import CredentialStore

ENTRY = "opencode"
ENV = "OPENCODE_API_KEY"


class OpenCodeAuth:
    methods = ("api_key",)

    def __init__(self, store: CredentialStore | None = None) -> None:
        self.store = store or CredentialStore()

    def _key(self) -> str | None:
        entry = self.store.read(ENTRY)
        return (entry or {}).get("api_key") or os.environ.get(ENV) or None

    def logged_in(self) -> bool:
        return self._key() is not None

    def logout(self) -> None:
        with self.store.locked():
            self.store.write(ENTRY, None)

    async def headers(self) -> dict[str, str]:
        key = self._key()
        if not key:
            raise AuthError(f"opencode: no API key; run `nexus login opencode` or set {ENV}")
        return {"authorization": f"Bearer {key}", "user-agent": "nexus/0.10"}

    async def login(self, method: str = "api_key", notify: Callable[[str], None] = print) -> None:
        notify("Paste your OpenCode Go API key (from opencode.ai/auth). Input is hidden.")
        key = getpass.getpass("API key: ").strip()
        if not key or len(key) > 4096 or any(ch.isspace() for ch in key):
            raise AuthError("opencode: that does not look like an API key")
        with self.store.locked():
            self.store.write(ENTRY, {"api_key": key})
