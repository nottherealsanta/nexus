"""Experimental ChatGPT OAuth support for the private Codex Responses endpoint.

Protocol constants below are pinned to anomalyco/opencode commit
18ef3cc7c5a25b82114c953a80ccc09f4988f74e, codex.ts (MIT); see NOTICE.
JWTs are decoded only to select request routing metadata, never verified or used
as authorization.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import html
import hashlib
import json
import math
import secrets
import time
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.parse import parse_qs, urlsplit

import httpx

from ..errors import ProviderError
from .store import CredentialRecord, CredentialStore, KeyringCredentialStore, validate_profile

ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPES = "openid profile email offline_access"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEVICE_URL = "https://auth.openai.com/deviceauth"


def pkce() -> tuple[str, str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge, secrets.token_urlsafe(32)


def _claims(token: str | None) -> Mapping[str, Any]:
    if not isinstance(token, str) or len(token) > 16_384:
        return {}
    try:
        raw = token.split(".")[1]
        data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        result = json.loads(data)
    except (IndexError, ValueError, UnicodeDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def routing_metadata(id_token: str | None, access_token: str | None) -> tuple[str | None, str | None]:
    """Pinned account/residency precedence. This intentionally does not verify JWTs."""
    def string_claim(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    for token in (id_token, access_token):
        data = _claims(token)
        account = string_claim(data.get("chatgpt_account_id"))
        auth = data.get("https://api.openai.com/auth")
        if not account and isinstance(auth, Mapping):
            account = string_claim(auth.get("chatgpt_account_id"))
        if not account:
            orgs = data.get("organizations")
            if isinstance(orgs, list) and orgs and isinstance(orgs[0], Mapping):
                account = string_claim(orgs[0].get("id"))
        if account:
            break
    else:
        account = None

    # Residency is routing metadata from the current access token, independent
    # of where the account ID was selected.
    access_claims = _claims(access_token)
    auth = access_claims.get("https://api.openai.com/auth")
    residency = string_claim(auth.get("chatgpt_compute_residency")) if isinstance(auth, Mapping) else None
    if not residency:
        residency = string_claim(access_claims.get("chatgpt_compute_residency"))
    return account, residency if residency != "no_constraint" else None


@dataclass(frozen=True)
class _Access:
    token: str
    expires_at: float
    generation: int
    account_id: str | None
    residency: str | None


class CodexOAuthManager:
    def __init__(self, *, profile: str = "default", store: CredentialStore | None = None, client: Any | None = None, now=time.time) -> None:
        self.profile = validate_profile(profile)
        self._store = store or KeyringCredentialStore()
        self._client = client
        self._now = now
        self._access: _Access | None = None
        self._refresh_lock = asyncio.Lock()

    async def headers(self) -> dict[str, str]:
        # Read each request: a daemon observes external login/logout immediately.
        record = await self._store.read(self.profile)
        if record is None:
            self._access = None
            raise ProviderError("codex: ChatGPT OAuth login required; run `nexus auth codex login`")
        cached = self._access
        if cached and cached.generation == record.generation and cached.expires_at > self._now() + 60:
            return self._header_values(cached)
        async with self._refresh_lock:
            record = await self._store.read(self.profile)
            if record is None:
                self._access = None
                raise ProviderError("codex: ChatGPT OAuth login required; run `nexus auth codex login`")
            cached = self._access
            if cached and cached.generation == record.generation and cached.expires_at > self._now() + 60:
                return self._header_values(cached)
            async with self._store.lock(self.profile):
                # A separate process may have rotated/deleted it while waiting.
                record = await self._store.read(self.profile)
                if record is None:
                    self._access = None
                    raise ProviderError("codex: ChatGPT OAuth login required; run `nexus auth codex login`")
                token = await self._token({"grant_type": "refresh_token", "refresh_token": record.refresh_token})
                access = token.get("access_token")
                if not isinstance(access, str) or not access:
                    raise ProviderError("codex: OAuth token response did not contain an access token")
                ttl = self._ttl(token, message="codex: OAuth token response had an invalid expiry")
                refresh = token.get("refresh_token")
                if refresh is not None and (not isinstance(refresh, str) or not refresh): raise ProviderError("codex: OAuth token response had an invalid refresh token")
                account, residency = routing_metadata(token.get("id_token"), access)
                replacement = CredentialRecord(refresh_token=refresh or record.refresh_token, account_id=account or record.account_id, residency=residency, generation=record.generation + 1)
                await self._store.write(self.profile, replacement)
                self._access = _Access(access, self._now() + ttl, replacement.generation, replacement.account_id, replacement.residency)
                return self._header_values(self._access)

    def _header_values(self, access: _Access) -> dict[str, str]:
        values = {"authorization": f"Bearer {access.token}", "originator": "nexus", "user-agent": "Nexus/0.1"}
        if access.account_id:
            values["ChatGPT-Account-Id"] = access.account_id
        if access.residency:
            values["x-openai-internal-codex-residency"] = access.residency
        return values

    @staticmethod
    def _ttl(payload: Mapping[str, Any], *, message: str) -> float:
        try:
            value = payload.get("expires_in")
            ttl = float(value) if not isinstance(value, bool) else 0.0
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProviderError(message) from exc
        if not math.isfinite(ttl) or ttl <= 0:
            raise ProviderError(message)
        return min(ttl, 86_400.0)

    @staticmethod
    def _response_mapping(response: Any, message: str) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(message) from exc
        if not isinstance(payload, Mapping):
            raise ProviderError(message)
        return payload

    async def _token(self, data: dict[str, str]) -> Mapping[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=20)
        owned = self._client is None
        try:
            response = await client.post(f"{ISSUER}/oauth/token", data={**data, "client_id": CLIENT_ID})
            if response.status_code >= 400:
                raise ProviderError("codex: OAuth token refresh failed; log in again")
            return self._response_mapping(response, "codex: OAuth token response was invalid")
        except httpx.HTTPError as exc:
            raise ProviderError("codex: OAuth token refresh failed; check connectivity or log in again") from exc
        finally:
            if owned:
                await client.aclose()

    async def logout(self) -> None:
        async with self._store.lock(self.profile):
            await self._store.delete(self.profile)
            self._access = None

    async def status(self) -> bool:
        return await self._store.read(self.profile) is not None

    async def device_login(self, *, notify=print, cancel: asyncio.Event | None = None, on_code=None) -> None:
        """Device-code login; ``on_code(url, code)`` replaces the text notice when given."""
        client = self._client or httpx.AsyncClient(timeout=20)
        owned = self._client is None
        try:
            start = await client.post(f"{ISSUER}/api/accounts/deviceauth/usercode", json={"client_id": CLIENT_ID})
            if start.status_code >= 400: raise ProviderError("codex: could not start device login")
            data = self._response_mapping(start, "codex: device login returned an invalid response")
            code, device_id = data.get("user_code"), data.get("device_auth_id")
            if not isinstance(code, str) or not code or not isinstance(device_id, str) or not device_id: raise ProviderError("codex: device login returned an invalid response")
            if on_code is not None:
                on_code(DEVICE_URL, code)
            else:
                notify(f"Open {DEVICE_URL} and enter code {code}\n")
            interval = self._ttl({"expires_in": data.get("interval")}, message="codex: device login returned an invalid interval")
            deadline = self._now() + 900.0
            while self._now() < deadline:
                if cancel is not None and cancel.is_set(): raise asyncio.CancelledError
                await asyncio.sleep(interval)
                poll = await client.post(f"{ISSUER}/api/accounts/deviceauth/token", json={"device_auth_id": device_id, "user_code": code})
                if poll.status_code in (403, 404):
                    continue
                if poll.status_code >= 400: raise ProviderError("codex: device login failed")
                result = self._response_mapping(poll, "codex: device login returned an invalid response")
                authorization_code, verifier = result.get("authorization_code"), result.get("code_verifier")
                if not isinstance(authorization_code, str) or not authorization_code or not isinstance(verifier, str) or not verifier: raise ProviderError("codex: device login returned an invalid response")
                token = await self._token({"grant_type": "authorization_code", "code": authorization_code, "code_verifier": verifier, "redirect_uri": "https://auth.openai.com/deviceauth/callback"})
                await self._persist_login(token)
                return
            raise ProviderError("codex: device login timed out")
        except httpx.HTTPError as exc:
            raise ProviderError("codex: device login failed; check connectivity") from exc
        finally:
            if owned:
                await client.aclose()

    async def browser_login(self, *, notify=print, browser_open=webbrowser.open) -> None:
        verifier, challenge, state = pkce()
        result: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                parts = line.decode("ascii", "replace").split()
                target = parts[1] if len(parts) >= 2 and parts[0] == "GET" else ""
                parsed = urlsplit(target)
                query = parse_qs(parsed.query, keep_blank_values=True)
                code = query.get("code", [None])[0]
                received = query.get("state", [""])[0]
                valid_path = parsed.path == "/auth/callback" and parts[:1] == ["GET"]
                valid = valid_path and isinstance(code, str) and bool(code) and hmac.compare_digest(received, state)
                message = "Login complete. You can return to Nexus." if valid and not result.done() else "Login failed. Return to Nexus."
                if valid and not result.done():
                    result.set_result(code)
                elif valid_path and not result.done():
                    result.set_exception(ProviderError("codex: browser login was denied or returned an invalid callback"))
                page = f"<html><body><p>{html.escape(message)}</p></body></html>".encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: " + str(len(page)).encode() + b"\r\nConnection: close\r\n\r\n" + page)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(callback, host="127.0.0.1", port=1455)
        try:
            params = urlencode({"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "scope": SCOPES, "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "id_token_add_organizations": "true", "codex_cli_simplified_flow": "true", "originator": "nexus"})
            url = f"{ISSUER}/oauth/authorize?{params}"
            if not browser_open(url): notify(f"Open this URL in a browser:\n{url}\n")
            code = await asyncio.wait_for(result, timeout=300)
            token = await self._token({"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": REDIRECT_URI})
            await self._persist_login(token)
        except asyncio.TimeoutError as exc:
            raise ProviderError("codex: browser login timed out") from exc
        finally:
            server.close()
            await server.wait_closed()

    async def _persist_login(self, token: Mapping[str, Any]) -> None:
        refresh, access = token.get("refresh_token"), token.get("access_token")
        account, residency = routing_metadata(token.get("id_token"), access)
        if not all(isinstance(value, str) and value for value in (refresh, access)): raise ProviderError("codex: OAuth login response lacked required tokens")
        self._ttl(token, message="codex: OAuth login response had an invalid expiry")
        async with self._store.lock(self.profile):
            old = await self._store.read(self.profile)
            record = CredentialRecord(refresh_token=refresh, account_id=account, residency=residency, generation=(old.generation + 1 if old else 1))
            await self._store.write(self.profile, record)
            self._access = None


class ChatGPTOAuthHeaders:
    """Async OpenAIProvider auth-header strategy."""
    def __init__(self, manager: CodexOAuthManager) -> None:
        self._manager = manager
    async def headers(self) -> Mapping[str, str]:
        return await self._manager.headers()
