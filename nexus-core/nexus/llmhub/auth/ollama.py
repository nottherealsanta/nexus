"""Ollama: a local server, so there is no key. Signing in records which server to use.

The address comes from `$OLLAMA_HOST` (as the Ollama CLI reads it) or the default
`http://localhost:11434`, and is stored in `auth.json` so a daemon started elsewhere
uses the same one.
"""
from __future__ import annotations

import os
from collections.abc import Callable

from .errors import AuthError
from .store import CredentialStore

ENTRY = "ollama"
DEFAULT_HOST = "http://localhost:11434"


def normalize_host(value: str) -> str:
    host = value.strip().rstrip("/")
    if not host:
        return DEFAULT_HOST
    if "://" not in host:
        host = f"http://{host}"
    if not host.startswith(("http://", "https://")) or any(ch.isspace() for ch in host):
        raise AuthError("ollama: that does not look like a server address")
    return host


class OllamaAuth:
    methods = ("local",)

    def __init__(self, store: CredentialStore | None = None) -> None:
        self.store = store or CredentialStore()

    @property
    def base_url(self) -> str:
        return ((self.store.read(ENTRY) or {}).get("base_url")) or DEFAULT_HOST

    def logged_in(self) -> bool:
        return self.store.read(ENTRY) is not None

    def logout(self) -> None:
        with self.store.locked():
            self.store.write(ENTRY, None)

    async def headers(self) -> dict[str, str]:
        return {"user-agent": "nexus/0.10"}

    async def login(self, method: str = "local", notify: Callable[[str], None] = print) -> None:
        self.set_host(os.environ.get("OLLAMA_HOST", ""))
        notify(f"Using the Ollama server at {self.base_url}.")

    def set_host(self, value: str) -> None:
        with self.store.locked():
            self.store.write(ENTRY, {"base_url": normalize_host(value)})
