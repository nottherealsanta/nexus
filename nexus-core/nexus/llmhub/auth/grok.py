"""grok: SuperGrok subscription device sign-in (auth.x.ai) and proxy headers.

Ported from old Nexus `nexus/auth/xai.py`; protocol constants come from
OpenCode's xAI plugin (MIT). The OAuth access token is the bearer for the Grok
CLI proxy (`cli-chat-proxy.grok.com/v1`). That proxy also requires the Grok
CLI's client headers, including a current version: read from
`~/.grok/version.json` (the installed Grok CLI) or `$NEXUS_GROK_CLIENT_VERSION`.
Only the refresh token is stored. Refresh tokens rotate, so refreshes happen
under the store lock.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from .device import DEVICE_GRANT, device_flow, post_form
from .errors import AuthError
from .store import CredentialStore

ENTRY = "grok"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
ISSUER = "https://auth.x.ai/oauth2"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
REFRESH_MARGIN_S = 300.0
VERSION_ENV = "NEXUS_GROK_CLIENT_VERSION"
_VERSION = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}(?:-[0-9A-Za-z.]{1,24})?")


def grok_cli_version(home: Path | None = None) -> str | None:
    value = os.environ.get(VERSION_ENV)
    if value and _VERSION.fullmatch(value):
        return value
    path = (home or Path.home() / ".grok") / "version.json"
    try:
        data = json.loads(path.read_bytes()[:4096])
    except (OSError, ValueError):
        return None
    for key in ("version", "stable_version"):
        found = data.get(key) if isinstance(data, dict) else None
        if isinstance(found, str) and _VERSION.fullmatch(found):
            return found
    return None


class GrokAuth:
    methods = ("device",)

    def __init__(self, store: CredentialStore | None = None) -> None:
        self.store = store or CredentialStore()
        self._access: str | None = None
        self._expires_at = 0.0

    def logged_in(self) -> bool:
        return self.store.read(ENTRY) is not None

    def logout(self) -> None:
        with self.store.locked():
            self.store.write(ENTRY, None)
        self._access = None

    async def headers(self) -> dict[str, str]:
        if self._access is None or self._expires_at < time.time() + REFRESH_MARGIN_S:
            await self._refresh()
        version = grok_cli_version()
        if version is None:
            raise AuthError(
                f"grok: the Grok proxy needs the Grok CLI version; install the Grok CLI (`grok update`) or set {VERSION_ENV}"
            )
        # The proxy requires the Grok CLI's own client identity; this is a protocol requirement.
        return {
            "authorization": f"Bearer {self._access}",
            "x-xai-token-auth": "xai-grok-cli",
            "x-grok-client-version": version,
            "x-grok-client-identifier": "grok-shell",
            "user-agent": "xai-grok-cli",
        }

    async def _refresh(self) -> None:
        with self.store.locked():
            entry = self.store.read(ENTRY)
            if not entry or not entry.get("refresh_token"):
                raise AuthError("grok: not logged in; run `nexus login grok`")
            status, token = await post_form(
                f"{ISSUER}/token",
                {"grant_type": "refresh_token", "refresh_token": entry["refresh_token"], "client_id": CLIENT_ID},
                label="grok",
            )
            if status >= 400 or not token.get("access_token"):
                raise AuthError(f"grok: session refresh failed (HTTP {status}); run `nexus login grok`")
            if token.get("refresh_token") and token["refresh_token"] != entry["refresh_token"]:
                self.store.write(ENTRY, {"refresh_token": token["refresh_token"]})
        self._remember(token)

    def _remember(self, token: dict) -> None:
        self._access = str(token["access_token"])
        self._expires_at = time.time() + min(float(token.get("expires_in") or 3600), 86_400.0)

    async def login(self, method: str = "device", notify: Callable[[str], None] = print) -> None:
        token = await device_flow(
            label="grok",
            code_url=f"{ISSUER}/device/code",
            token_url=f"{ISSUER}/token",
            start_form={"client_id": CLIENT_ID, "scope": SCOPE, "referrer": "nexus"},
            poll_form=lambda code: {"grant_type": DEVICE_GRANT, "client_id": CLIENT_ID, "device_code": code},
            notify=notify,
            extra_wait=3.0,  # OpenCode adds 3 s per poll to stay clear of slow_down
        )
        if not token.get("refresh_token"):
            raise AuthError("grok: sign-in returned no refresh token")
        with self.store.locked():
            self.store.write(ENTRY, {"refresh_token": token["refresh_token"]})
        self._remember(token)
