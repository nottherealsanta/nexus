"""GitHub.com device OAuth and short-lived GitHub Copilot credentials.

The GitHub OAuth token stays in the native keychain and is the bearer for the
Copilot API directly; there is no ``copilot_internal`` token exchange. Sign-in
is verified against the models endpoint before anything is saved. See plan
section 3.4.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..errors import ProviderError
from .store import KeyringSecretStore, SecretStore, validate_profile

DEFAULT_DOMAIN = "github.com"
DEFAULT_BASE_URL = "https://api.githubcopilot.com"
PROVIDER = "github-copilot"
USER_AGENT = "Nexus/0.1"
INTEGRATION_ID = "copilot-developer-cli"
CLIENT_ID = "Ov23li8tweQw6odWQebz"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
VERIFY_URL = f"{DEFAULT_BASE_URL}/models"
DEVICE_URL = "https://github.com/login/device"
_HOST = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
_MAX_RESPONSE = 64 * 1024
_MAX_TOKEN = 16 * 1024
_LOGIN_ERROR = "github-copilot: GitHub.com device sign-in failed; check connectivity and try again"
_INCOMPATIBLE = (
    "github-copilot: GitHub Copilot API returned 403. Check that the account has an active "
    "Copilot plan and that its organization allows this OAuth app"
)
_LOGIN_REQUIRED = "github-copilot: GitHub login required; connect GitHub Copilot in Settings"


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
        self.profile = validate_profile(profile)
        self._store = store or KeyringSecretStore()
        self._client = client
        self._now = now or time.time
        self._sleep = sleep or asyncio.sleep
        self._refresh_lock = asyncio.Lock()
        self._generation = 0

    @property
    def account(self) -> str:
        return f"{PROVIDER}:{self.profile}"

    async def _record(self) -> dict[str, Any] | None:
        raw = await self._store.read_secret(self.account)
        if raw is None or len(raw.encode("utf-8", "replace")) > 8192:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if (
            not isinstance(data, dict)
            or set(data) not in ({"v", "github_token", "domain"}, {"v", "github_token", "domain", "expires_at"})
            or data.get("v") != 2
            or data.get("domain") != DEFAULT_DOMAIN
        ):
            return None
        token = data.get("github_token")
        if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN or any(ch.isspace() for ch in token):
            return None
        expiry = data.get("expires_at")
        if expiry is not None and (isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry)):
            return None
        return {"github_token": token, "domain": DEFAULT_DOMAIN, "expires_at": expiry, "raw": raw}

    async def status(self) -> bool:
        record = await self._record()
        return record is not None and (record["expires_at"] is None or record["expires_at"] > self._now())

    async def domain(self) -> str | None:
        record = await self._record()
        if record is None or (record["expires_at"] is not None and record["expires_at"] <= self._now()):
            return None
        return record["domain"]

    async def logout(self) -> None:
        self._generation += 1
        async with self._refresh_lock:
            await self._store.delete_secret(self.account)

    async def _get_client(self) -> tuple[Any, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.AsyncClient(timeout=20, follow_redirects=False), True

    @staticmethod
    def _json(response: Any, message: str) -> Mapping[str, Any]:
        content = getattr(response, "content", b"")
        if isinstance(content, (bytes, bytearray)) and len(content) > _MAX_RESPONSE:
            raise ProviderError(message)
        try:
            payload = response.json()
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ProviderError(message) from None
        if not isinstance(payload, Mapping):
            raise ProviderError(message)
        return payload

    @staticmethod
    def _token(payload: Mapping[str, Any], key: str, message: str) -> str:
        token = payload.get(key)
        if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN or any(ch.isspace() for ch in token):
            raise ProviderError(message)
        return token

    @staticmethod
    async def _request(client: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Read a response only up to the protocol's fixed size limit."""
        async with client.stream(method, url, **kwargs) as response:
            if response.status_code >= 300:
                return httpx.Response(response.status_code, headers=response.headers, request=response.request)
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_RESPONSE:
                    raise ProviderError("github-copilot: authentication service returned an oversized response")
                chunks.append(chunk)
            return httpx.Response(
                response.status_code,
                # aiter_bytes() has already decoded Content-Encoding. Keeping
                # gzip here makes the reconstructed response decode JSON twice.
                headers={key: value for key, value in response.headers.items()
                         if key.lower() not in ("content-encoding", "content-length")},
                content=b"".join(chunks),
                request=response.request,
            )

    async def _verify(self, github_token: str) -> None:
        """Confirm the GitHub token can reach the Copilot API."""
        client, owned = await self._get_client()
        try:
            response = await self._request(client, "GET", VERIFY_URL,
                headers=self._header_values(github_token), follow_redirects=False)
            if 300 <= response.status_code < 400:
                raise ProviderError("github-copilot: Copilot API returned an unexpected redirect")
            if response.status_code == 401:
                raise ProviderError("github-copilot: GitHub OAuth token was rejected; sign in again")
            if response.status_code == 403:
                raise ProviderError(_INCOMPATIBLE)
            if response.status_code >= 400:
                raise ProviderError("github-copilot: could not reach the Copilot API")
        except httpx.HTTPError:
            raise ProviderError("github-copilot: could not reach the Copilot API; check connectivity") from None
        finally:
            if owned:
                await client.aclose()

    async def headers(self) -> dict[str, str]:
        """Request headers for the stored GitHub token, re-read from the keychain each time."""
        operation_generation = self._generation
        record = await self._record()
        if operation_generation != self._generation or record is None:
            raise ProviderError(_LOGIN_REQUIRED)
        if record["expires_at"] is not None and record["expires_at"] <= self._now():
            raise ProviderError("github-copilot: GitHub OAuth login expired; sign in again")
        return self._header_values(record["github_token"])

    @staticmethod
    def _header_values(token: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {token}", "user-agent": USER_AGENT,
            "openai-intent": "conversation-edits", "copilot-integration-id": INTEGRATION_ID,
        }

    async def device_login(
        self, *, domain: str | None = None, on_code: Callable[[str, str], None] | None = None,
        cancel: Any | None = None,
    ) -> None:
        """Run GitHub.com device OAuth, then verify Copilot access before saving."""
        try:
            host = normalize_domain(domain)
        except ValueError as exc:
            raise ProviderError("github-copilot: sign-in is currently supported only on GitHub.com") from exc
        if host != DEFAULT_DOMAIN:
            raise ProviderError("github-copilot: sign-in is currently supported only on GitHub.com")
        self._generation += 1
        generation = self._generation
        client, owned = await self._get_client()
        try:
            start = await self._request(client, "POST",
                DEVICE_CODE_URL,
                data={"client_id": CLIENT_ID, "scope": "read:user"},
                headers={"accept": "application/json"},
                follow_redirects=False,
            )
            if 300 <= start.status_code < 400:
                raise ProviderError(_LOGIN_ERROR)
            if start.status_code >= 400:
                raise ProviderError(_LOGIN_ERROR)
            data = self._json(start, "github-copilot: device authorization returned an invalid response")
            code = self._token(data, "device_code", "github-copilot: device authorization returned an invalid response")
            user_code = self._token(data, "user_code", "github-copilot: device authorization returned an invalid response")
            verification_uri = data.get("verification_uri")
            if verification_uri not in (DEVICE_URL, f"{DEVICE_URL}/"):
                raise ProviderError("github-copilot: GitHub returned an unexpected device verification URL")
            interval = data.get("interval", 5)
            expires_in = data.get("expires_in", 900)
            if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval):
                raise ProviderError("github-copilot: device authorization returned an invalid interval")
            if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or not math.isfinite(expires_in) or expires_in <= 0:
                raise ProviderError("github-copilot: device authorization returned an invalid expiry")
            interval = min(max(float(interval), 1.0), 30.0)
            deadline = self._now() + min(max(float(expires_in), 1.0), 1800.0)
            if on_code is not None:
                on_code(verification_uri, user_code)
            while self._now() < deadline:
                if cancel is not None and cancel.is_set():
                    raise asyncio.CancelledError
                delay = min(interval, max(0.0, deadline - self._now()))
                await self._sleep(delay)
                if cancel is not None and cancel.is_set():
                    raise asyncio.CancelledError
                if self._now() >= deadline:
                    break
                response = await self._request(client, "POST",
                    ACCESS_TOKEN_URL,
                    data={"client_id": CLIENT_ID, "device_code": code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
                    headers={"accept": "application/json"},
                    follow_redirects=False,
                )
                if 300 <= response.status_code < 400:
                    raise ProviderError(_LOGIN_ERROR)
                if response.status_code >= 400:
                    raise ProviderError(_LOGIN_ERROR)
                result = self._json(response, "github-copilot: device authorization returned an invalid response")
                error = result.get("error")
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval = min(interval + 5.0, 60.0)
                    continue
                if error in ("access_denied", "authorization_declined"):
                    raise ProviderError("github-copilot: GitHub device authorization was denied")
                if error == "expired_token":
                    raise ProviderError("github-copilot: GitHub device authorization expired")
                if error is not None:
                    raise ProviderError(_LOGIN_ERROR)
                github_token = self._token(result, "access_token", "github-copilot: GitHub OAuth response did not contain a valid token")
                expires_at = None
                if "expires_in" in result:
                    value = result["expires_in"]
                    try:
                        ttl = float(value)
                    except (TypeError, ValueError, OverflowError):
                        raise ProviderError("github-copilot: GitHub OAuth response had an invalid expiry") from None
                    if isinstance(value, bool) or not math.isfinite(ttl) or ttl <= 0:
                        raise ProviderError("github-copilot: GitHub OAuth response had an invalid expiry")
                    expires_at = self._now() + min(ttl, 31_536_000.0)
                await self._verify(github_token)
                if cancel is not None and cancel.is_set():
                    raise asyncio.CancelledError
                if generation != self._generation:
                    raise ProviderError("github-copilot: device login was superseded by a newer login or logout")
                record: dict[str, Any] = {"v": 2, "github_token": github_token, "domain": DEFAULT_DOMAIN}
                if expires_at is not None:
                    record["expires_at"] = expires_at
                raw = json.dumps(record, separators=(",", ":"))
                async with self._refresh_lock:
                    if cancel is not None and cancel.is_set():
                        raise asyncio.CancelledError
                    if generation != self._generation:
                        raise ProviderError("github-copilot: device login was superseded by a newer login or logout")
                    if expires_at is not None and expires_at <= self._now():
                        raise ProviderError("github-copilot: GitHub OAuth login expired during Copilot verification; sign in again")
                    if cancel is not None and cancel.is_set():
                        raise asyncio.CancelledError
                    write_task = asyncio.create_task(self._store.write_secret(self.account, raw))
                    try:
                        await asyncio.shield(write_task)
                    except asyncio.CancelledError:
                        await write_task
                        if await self._store.read_secret(self.account) == raw:
                            await self._store.delete_secret(self.account)
                        raise
                    if cancel is not None and cancel.is_set() or generation != self._generation:
                        if await self._store.read_secret(self.account) == raw:
                            await self._store.delete_secret(self.account)
                        if cancel is not None and cancel.is_set():
                            raise asyncio.CancelledError
                        raise ProviderError("github-copilot: device login was superseded by a newer login or logout")
                return
            raise ProviderError("github-copilot: GitHub device authorization timed out")
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError:
            raise ProviderError("github-copilot: GitHub device sign-in failed; check connectivity") from None
        finally:
            if owned:
                await client.aclose()


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
    """OpenAIProvider auth strategy with Copilot-specific request headers."""

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


__all__ = ["DEFAULT_BASE_URL", "CopilotAuthManager", "CopilotHeaders", "api_base_url", "normalize_domain"]
