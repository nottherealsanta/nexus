"""Where a remote MCP server's OAuth tokens and registered client live: `<home>/auth.json`.

The same owner-only file the providers use, one entry per server under the key
`mcp-oauth:<scope>:<name>:<url hash>`. The URL hash binds an entry to the address it
signed in to: editing a server's URL never sends the old tokens to the new address.
`scope` is `global` or the project's log folder (`projects/<hash>`), so the same server
name in two projects keeps separate sign-ins.

This module is light (no MCP SDK) so status rows can ask "signed in?" without loading it.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from ..llmhub.auth.store import CredentialStore
from .config import ServerConfig

PREFIX = "mcp-oauth:"


def _part(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text)


def prefix(scope: str, name: str) -> str:
    return f"{PREFIX}{_part(scope)}:{name}:"


def key(scope: str, config: ServerConfig) -> str:
    return prefix(scope, config.name) + hashlib.sha256(config.url.encode()).hexdigest()[:12]


def read(entry_key: str) -> dict[str, Any]:
    return CredentialStore().read(entry_key) or {}


def update(entry_key: str, **fields: Any) -> None:
    store = CredentialStore()
    with store.locked():
        store.write(entry_key, {**(store.read(entry_key) or {}), **fields})


def clear(entry_key: str) -> None:
    store = CredentialStore()
    with store.locked():
        store.write(entry_key, None)


def forget_server(scope: str, name: str) -> None:
    """Drop every stored sign-in for one server name (it was removed, or its address changed)."""
    store = CredentialStore()
    with store.locked():
        for stored in [k for k in store.names() if k.startswith(prefix(scope, name))]:
            store.write(stored, None)


def signed_in(scope: str, config: ServerConfig) -> bool:
    try:
        return bool(read(key(scope, config)).get("tokens"))
    except Exception:  # noqa: BLE001 - an unreadable credential file is "not signed in", never a failed status call
        return False
