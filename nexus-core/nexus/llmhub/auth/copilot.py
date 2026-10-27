"""GitHub Copilot: GitHub.com device sign-in; the GitHub token is the Copilot bearer.

Ported from old Nexus `nexus/auth/copilot.py` (same first-party OAuth app and
integration id). There is no `copilot_internal` token exchange. The token is
checked against `GET /models` before it is saved. GitHub Enterprise is not
supported yet.
"""
from __future__ import annotations

from collections.abc import Callable

import httpx

from .device import device_flow
from .errors import AuthError
from .store import CredentialStore

ENTRY = "copilot"
CLIENT_ID = "Ov23li8tweQw6odWQebz"
INTEGRATION_ID = "copilot-developer-cli"
API_BASE = "https://api.githubcopilot.com"


class CopilotAuth:
    methods = ("device",)

    def __init__(self, store: CredentialStore | None = None) -> None:
        self.store = store or CredentialStore()

    def logged_in(self) -> bool:
        return self.store.read(ENTRY) is not None

    def logout(self) -> None:
        with self.store.locked():
            self.store.write(ENTRY, None)

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {token}",
            "user-agent": "nexus/0.10",
            "openai-intent": "conversation-edits",
            "copilot-integration-id": INTEGRATION_ID,
        }

    async def headers(self) -> dict[str, str]:
        entry = self.store.read(ENTRY)
        if not entry or not entry.get("github_token"):
            raise AuthError("copilot: not logged in; run `nexus login copilot`")
        return self._headers(entry["github_token"])

    async def login(self, method: str = "device", notify: Callable[[str], None] = print) -> None:
        result = await device_flow(
            label="copilot",
            code_url="https://github.com/login/device/code",
            token_url="https://github.com/login/oauth/access_token",
            start_form={"client_id": CLIENT_ID, "scope": "read:user"},
            poll_form=lambda code: {"client_id": CLIENT_ID, "device_code": code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
            notify=notify,
        )
        token = str(result["access_token"])
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                check = await client.get(f"{API_BASE}/models", headers=self._headers(token))
        except httpx.HTTPError as exc:
            raise AuthError("copilot: could not reach the Copilot API to verify the sign-in") from exc
        if check.status_code == 403:
            raise AuthError("copilot: the account has no active Copilot plan, or its organization blocks this app")
        if check.status_code >= 400:
            raise AuthError(f"copilot: the Copilot API rejected the sign-in (HTTP {check.status_code})")
        with self.store.locked():
            self.store.write(ENTRY, {"github_token": token})
