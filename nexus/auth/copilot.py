"""GitHub Copilot model access from a token already in the keychain.

Nexus does not run a GitHub OAuth device flow. That flow has no Nexus OAuth
app: the client id previously used here belonged to OpenCode (Anomaly), so
GitHub's consent page asked the user to authorize OpenCode. Signing in through
another product's app is not acceptable.

A token and its GitHub domain, if already stored, stay in the secure native
keychain and never cross the host boundary. They are sent as a bearer token
(no ``copilot_internal`` exchange). This is Copilot *model* access only, not
GitHub repository access. New sign-in is refused until Nexus has its own app.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from ..errors import ProviderError
from .store import KeyringSecretStore, SecretStore, validate_profile

DEFAULT_DOMAIN = "github.com"
DEFAULT_BASE_URL = "https://api.githubcopilot.com"
PROVIDER = "github-copilot"
USER_AGENT = "Nexus/0.1"
_HOST = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
_NO_APP = "github-copilot: Nexus does not sign in through OpenCode's GitHub app"


def normalize_domain(value: str | None) -> str:
    """``github.com`` or a GitHub Enterprise host (``company.ghe.com``)."""
    text = (value or "").strip().lower()
    if not text:
        return DEFAULT_DOMAIN
    try:
        parts = urlsplit(text if "://" in text else f"https://{text}")
        host, port = parts.hostname or "", parts.port
    except ValueError:
        host, port = "", None
    if (
        parts.scheme != "https" or port is not None or parts.username or parts.password
        or parts.path not in ("", "/") or parts.query or parts.fragment or not _HOST.fullmatch(host)
    ):
        raise ValueError("enter a GitHub Enterprise domain such as company.ghe.com")
    return host


def api_base_url(domain: str | None) -> str:
    """The Copilot model endpoint for a GitHub domain."""
    host = normalize_domain(domain)
    return DEFAULT_BASE_URL if host == DEFAULT_DOMAIN else f"https://copilot-api.{host}"


class CopilotAuthManager:
    def __init__(
        self,
        *,
        profile: str = "default",
        store: SecretStore | None = None,
        client: Any | None = None,
        now: Callable[[], float] | None = None,
        sleep: Callable[..., Any] | None = None,
    ) -> None:
        del client, now, sleep
        self.profile = validate_profile(profile)
        self._store = store or KeyringSecretStore()

    @property
    def account(self) -> str:
        return f"{PROVIDER}:{self.profile}"

    async def _record(self) -> dict[str, str] | None:
        raw = await self._store.read_secret(self.account)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("v") != 1:
            return None
        token, domain = data.get("token"), data.get("domain")
        if not isinstance(token, str) or not token or not isinstance(domain, str):
            return None
        return {"token": token, "domain": domain}

    async def status(self) -> bool:
        return await self._record() is not None

    async def domain(self) -> str | None:
        record = await self._record()
        return record["domain"] if record else None

    async def logout(self) -> None:
        await self._store.delete_secret(self.account)

    async def headers(self) -> dict[str, str]:
        """Read on every request so a sign-out applies without a restart."""
        record = await self._record()
        if record is None:
            raise ProviderError(_NO_APP)
        return {
            "authorization": f"Bearer {record['token']}",
            "user-agent": USER_AGENT,
            "openai-intent": "conversation-edits",
        }

    async def device_login(
        self, *, domain: str | None = None, on_code: Callable[[str, str], None] | None = None,
        cancel: Any | None = None,
    ) -> None:
        """Refuse GitHub device sign-in. Nexus has no OAuth app of its own."""
        del domain, on_code, cancel
        raise ProviderError(_NO_APP)


def _is_agent_turn(body: Mapping[str, Any]) -> tuple[bool, bool]:
    """``(agent_initiated, has_image)`` for a chat-completions or Responses body."""
    items = body.get("messages") if isinstance(body.get("messages"), list) else body.get("input")
    if not isinstance(items, list) or not items:
        return False, False
    image_parts = {"image_url", "input_image", "image"}
    has_image = any(
        isinstance(item, Mapping) and isinstance(item.get("content"), list)
        and any(isinstance(part, Mapping) and part.get("type") in image_parts for part in item["content"])
        for item in items
    )
    last = items[-1]
    return not (isinstance(last, Mapping) and last.get("role") == "user"), has_image


class CopilotHeaders:
    """OpenAIProvider auth strategy: bearer token plus Copilot request headers.

    ``x-initiator`` is ``agent`` for tool-result follow-ups and ``user`` only
    when the request ends with the operator's message, matching the other
    Copilot clients so a multi-step turn is billed as one user request.
    """

    def __init__(self, manager: CopilotAuthManager) -> None:
        self._manager = manager

    async def headers(self) -> Mapping[str, str]:
        return await self._manager.headers()

    def request_headers(self, body: Mapping[str, Any]) -> Mapping[str, str]:
        agent, image = _is_agent_turn(body)
        headers = {"x-initiator": "agent" if agent else "user"}
        if image:
            headers["copilot-vision-request"] = "true"
        return headers


__all__ = [
    "DEFAULT_BASE_URL",
    "CopilotAuthManager",
    "CopilotHeaders",
    "api_base_url",
    "normalize_domain",
]
