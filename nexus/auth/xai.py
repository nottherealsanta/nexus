"""SuperGrok subscription sign-in for xAI: device-code OAuth and token refresh.

Protocol constants (issuer, client id, scope, grant types, polling rules) are
adapted from OpenCode's xAI plugin, ``packages/core/src/plugin/provider/xai.ts``
(anomalyco/opencode commit 6a7cedcbf1823eeb1c8dfc4baf5a7951854f578b, MIT; see
``NOTICE``). The OAuth access token is the Bearer for ``https://cli-chat-proxy.grok.com/v1``,
exactly as an API key would be (OpenCode's ``model-resolver.ts`` passes it as
``apiKey``).

Storage follows the ChatGPT route (plan section 7): only the refresh token is
persisted, in the private credential file; the short-lived access token is
cached in memory and refreshed five minutes before it expires, under a
cross-process lock so a rotated refresh token is never lost to a race. Every
response body is bounded and every error is redacted.

Not verified live: no SuperGrok account was available. Whether xAI accepts
Nexus's own ``referrer`` value (OpenCode sends ``opencode``) is not known.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..errors import ProviderError
from .store import FileSecretStore, SecretStore, validate_profile

PROVIDER = "xai"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
ISSUER = "https://auth.x.ai/oauth2"
DEVICE_CODE_URL = f"{ISSUER}/device/code"
TOKEN_URL = f"{ISSUER}/token"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFERRER = "nexus"
USER_AGENT = "Nexus/0.1"
#: Refresh this long before expiry (OpenCode uses the same five minutes).
REFRESH_MARGIN_S = 300.0
#: Added to every poll interval; OpenCode adds 3 s to stay clear of ``slow_down``.
POLL_SAFETY_S = 3.0
_MAX_RESPONSE = 64 * 1024
_MAX_TOKEN = 4096
_LOGIN_REQUIRED = "xai: SuperGrok sign-in required; connect xAI in Settings → Providers"
_LOGIN_ERROR = "xai: SuperGrok sign-in failed; check connectivity and try again"
_INVALID = "xai: the xAI sign-in service returned an invalid response"


@dataclass(frozen=True, repr=False)
class _Access:
    token: str
    expires_at: float
    refresh: str


def _valid_token(value: object) -> str | None:
    if isinstance(value, str) and value and len(value) <= _MAX_TOKEN and not any(ch.isspace() for ch in value):
        return value
    return None


def _jwt_expiry(token: str) -> float | None:
    """The ``exp`` claim of a JWT access token, read without verification."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError):
        return None
    exp = claims.get("exp") if isinstance(claims, Mapping) else None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
        return None
    return float(exp)


def _seconds(value: object, default: float, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return default
    return min(max(float(value), low), high)


def _xai_url(value: object) -> str | None:
    """A verification URL on ``x.ai`` over HTTPS, or ``None``."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or not (host == "x.ai" or host.endswith(".x.ai")):
        return None
    return value


class XAIOAuthManager:
    """SuperGrok device sign-in and Bearer headers for the xAI API."""

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
        self._store = store or FileSecretStore()
        self._client = client
        self._now = now or time.time
        self._sleep = sleep or asyncio.sleep
        self._access: _Access | None = None
        self._refresh_lock = asyncio.Lock()

    @property
    def account(self) -> str:
        return f"{PROVIDER}:{self.profile}"

    # -- stored record -------------------------------------------------------

    async def _refresh_token(self) -> str | None:
        raw = await self._store.read_secret(self.account)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or set(data) != {"v", "refresh_token"} or data.get("v") != 1:
            return None
        return _valid_token(data.get("refresh_token"))

    async def _save(self, refresh: str) -> None:
        await self._store.write_secret(self.account, json.dumps({"v": 1, "refresh_token": refresh}, separators=(",", ":")))

    def _process_lock(self) -> Any:
        """The store's cross-process lock when it has one (the file store does)."""
        lock = getattr(self._store, "lock", None)
        return lock(f"xai-{self.profile}") if callable(lock) else _NullLock()

    # -- public API ------------------------------------------------------------

    async def status(self) -> bool:
        return await self._refresh_token() is not None

    async def logout(self) -> None:
        async with self._refresh_lock:
            async with self._process_lock():
                await self._store.delete_secret(self.account)
            self._access = None

    async def headers(self) -> dict[str, str]:
        """``Authorization: Bearer <access token>``, refreshed when near expiry.

        The stored refresh token is re-read on every call, so a logout or a
        sign-in from another process applies to the next request.
        """
        refresh = await self._refresh_token()
        if refresh is None:
            self._access = None
            raise ProviderError(_LOGIN_REQUIRED)
        cached = self._access
        if cached is not None and cached.refresh == refresh and cached.expires_at > self._now() + REFRESH_MARGIN_S:
            return self._header_values(cached.token)
        async with self._refresh_lock:
            async with self._process_lock():
                # Another task or process may have rotated it while we waited.
                refresh = await self._refresh_token()
                if refresh is None:
                    self._access = None
                    raise ProviderError(_LOGIN_REQUIRED)
                cached = self._access
                if cached is not None and cached.refresh == refresh and cached.expires_at > self._now() + REFRESH_MARGIN_S:
                    return self._header_values(cached.token)
                payload = await self._post(TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": refresh,
                                                      "client_id": CLIENT_ID})
                if payload is None:
                    raise ProviderError("xai: SuperGrok session could not be refreshed; sign in again")
                access = self._remember(payload, refresh)
                if access.refresh != refresh:
                    await self._save(access.refresh)
                return self._header_values(access.token)

    async def device_login(
        self,
        *,
        on_code: Callable[[str, str], Any] | None = None,
        cancel: asyncio.Event | None = None,
    ) -> None:
        """Run the device-code flow and store the refresh token on success."""
        start = await self._post(DEVICE_CODE_URL, {"client_id": CLIENT_ID, "scope": SCOPE, "referrer": REFERRER})
        if start is None:
            raise ProviderError(_LOGIN_ERROR)
        device_code = _valid_token(start.get("device_code"))
        user_code = start.get("user_code")
        base_url = _xai_url(start.get("verification_uri"))
        if device_code is None or not isinstance(user_code, str) or not 1 <= len(user_code) <= 64 or base_url is None:
            raise ProviderError(_INVALID)
        url = _xai_url(start.get("verification_uri_complete")) or base_url
        interval = _seconds(start.get("interval"), 5.0, low=1.0, high=60.0)
        deadline = self._now() + _seconds(start.get("expires_in"), 300.0, low=1.0, high=1800.0)
        if on_code is not None:
            on_code(url, user_code)
        while self._now() < deadline:
            if cancel is not None and cancel.is_set():
                raise asyncio.CancelledError
            await self._sleep(interval + POLL_SAFETY_S)
            if cancel is not None and cancel.is_set():
                raise asyncio.CancelledError
            status, payload = await self._post_raw(TOKEN_URL, {"grant_type": DEVICE_GRANT, "client_id": CLIENT_ID,
                                                               "device_code": device_code})
            if status < 300 and payload is not None:
                refresh = _valid_token(payload.get("refresh_token"))
                if refresh is None:
                    raise ProviderError("xai: SuperGrok sign-in returned no refresh token")
                access = self._remember(payload, refresh)
                if cancel is not None and cancel.is_set():
                    raise asyncio.CancelledError
                async with self._refresh_lock:
                    async with self._process_lock():
                        await self._save(refresh)
                    self._access = access
                return
            error = payload.get("error") if payload is not None else None
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval = min(interval + 5.0, 60.0)
                continue
            if error in ("access_denied", "authorization_denied"):
                raise ProviderError("xai: SuperGrok sign-in was denied")
            if error == "expired_token":
                raise ProviderError("xai: the sign-in code expired; start again")
            raise ProviderError(_LOGIN_ERROR)
        raise ProviderError("xai: SuperGrok sign-in timed out")

    # -- internals -------------------------------------------------------------

    def _remember(self, payload: Mapping[str, Any], current_refresh: str) -> _Access:
        token = _valid_token(payload.get("access_token"))
        if token is None:
            raise ProviderError(_INVALID)
        rotated = payload.get("refresh_token")
        refresh = current_refresh if rotated is None else _valid_token(rotated)
        if refresh is None:
            raise ProviderError(_INVALID)
        expires_in = payload.get("expires_in")
        if expires_in is not None:
            expires_at = self._now() + _seconds(expires_in, 3600.0, low=1.0, high=86_400.0)
        else:
            expires_at = _jwt_expiry(token) or self._now() + 3600.0
        self._access = _Access(token, expires_at, refresh)
        return self._access

    @staticmethod
    def _header_values(token: str) -> dict[str, str]:
        return {"authorization": f"Bearer {token}", "user-agent": USER_AGENT}

    async def _post(self, url: str, form: Mapping[str, str]) -> Mapping[str, Any] | None:
        status, payload = await self._post_raw(url, form)
        return payload if status < 300 else None

    async def _post_raw(self, url: str, form: Mapping[str, str]) -> tuple[int, Mapping[str, Any] | None]:
        """POST a form; ``(status, JSON object or None)`` with a bounded body."""
        client = self._client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        owned = self._client is None
        try:
            async with client.stream("POST", url, data=dict(form), follow_redirects=False,
                                     headers={"accept": "application/json", "user-agent": USER_AGENT}) as response:
                if 300 <= response.status_code < 400:
                    raise ProviderError("xai: the sign-in service returned an unexpected redirect")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE:
                        raise ProviderError("xai: the sign-in service returned an oversized response")
                status = response.status_code
        except httpx.HTTPError:
            raise ProviderError(_LOGIN_ERROR) from None
        finally:
            if owned:
                await client.aclose()
        try:
            payload = json.loads(bytes(body))
        except (ValueError, TypeError):
            payload = None
        return status, payload if isinstance(payload, Mapping) else None


class _NullLock:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class XAIHeaders:
    """``OpenAIProvider`` auth strategy backed by the SuperGrok sign-in."""

    def __init__(self, manager: XAIOAuthManager) -> None:
        self._manager = manager

    async def headers(self) -> Mapping[str, str]:
        return await self._manager.headers()


__all__ = ["CLIENT_ID", "ISSUER", "XAIHeaders", "XAIOAuthManager"]
