"""ChatGPT OAuth for the Codex backend: browser (PKCE) and device-code login.

Ported from old Nexus `nexus/auth/codex.py`; protocol constants are pinned to
anomalyco/opencode codex.ts (MIT). Only the refresh token and routing metadata
are stored (`auth.json`, entry `codex`). Access tokens live in memory and are
refreshed under the store lock, because OpenAI rotates refresh tokens.
JWTs are decoded only to read routing claims, never trusted for authorization.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import secrets
import time
import webbrowser
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .errors import AuthError
from .store import CredentialStore

ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPES = "openid profile email offline_access"
DEVICE_URL = "https://auth.openai.com/deviceauth"
ENTRY = "codex"


def _claims(token: str | None) -> Mapping[str, Any]:
    if not isinstance(token, str) or len(token) > 16_384:
        return {}
    try:
        raw = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (IndexError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def routing_metadata(id_token: str | None, access_token: str | None) -> tuple[str | None, str | None]:
    """(ChatGPT account id, compute residency) from token claims."""
    account = None
    for token in (id_token, access_token):
        data = _claims(token)
        auth = data.get("https://api.openai.com/auth")
        account = data.get("chatgpt_account_id") or (auth.get("chatgpt_account_id") if isinstance(auth, Mapping) else None)
        if not account:
            orgs = data.get("organizations")
            if isinstance(orgs, list) and orgs and isinstance(orgs[0], Mapping):
                account = orgs[0].get("id")
        if account:
            break
    access = _claims(access_token)
    auth = access.get("https://api.openai.com/auth")
    residency = (auth.get("chatgpt_compute_residency") if isinstance(auth, Mapping) else None) or access.get("chatgpt_compute_residency")
    return (account if isinstance(account, str) else None), (residency if isinstance(residency, str) and residency != "no_constraint" else None)


class CodexAuth:
    """ChatGPT sign-in and request headers for the Codex backend."""

    methods = ("browser", "device")

    def __init__(self, store: CredentialStore | None = None) -> None:
        self.store = store or CredentialStore()
        self._access: str | None = None
        self._expires_at = 0.0
        self._account: str | None = None
        self._residency: str | None = None

    def logged_in(self) -> bool:
        return self.store.read(ENTRY) is not None

    def logout(self) -> None:
        with self.store.locked():
            self.store.write(ENTRY, None)
        self._access = None

    async def headers(self) -> dict[str, str]:
        if self._access is None or self._expires_at < time.time() + 60:
            await self._refresh()
        values = {"authorization": f"Bearer {self._access}", "originator": "nexus", "user-agent": "nexus/0.10"}
        if self._account:
            values["chatgpt-account-id"] = self._account
        if self._residency:
            values["x-openai-internal-codex-residency"] = self._residency
        return values

    async def _refresh(self) -> None:
        with self.store.locked():
            entry = self.store.read(ENTRY)
            if not entry or not entry.get("refresh_token"):
                raise AuthError("codex: not logged in; run `nexus login codex`")
            token = await _token({"grant_type": "refresh_token", "refresh_token": entry["refresh_token"]})
            account, residency = routing_metadata(token.get("id_token"), token.get("access_token"))
            entry = {
                "refresh_token": token.get("refresh_token") or entry["refresh_token"],
                "account_id": account or entry.get("account_id"),
                "residency": residency,
            }
            self.store.write(ENTRY, entry)
        self._access = token["access_token"]
        self._expires_at = time.time() + min(float(token.get("expires_in") or 3600), 86_400.0)
        self._account, self._residency = entry["account_id"], entry["residency"]

    def _persist(self, token: Mapping[str, Any]) -> None:
        if not token.get("refresh_token") or not token.get("access_token"):
            raise AuthError("codex: login response lacked tokens")
        account, residency = routing_metadata(token.get("id_token"), token.get("access_token"))
        with self.store.locked():
            self.store.write(ENTRY, {"refresh_token": token["refresh_token"], "account_id": account, "residency": residency})
        self._access = None

    async def login(self, method: str = "browser", notify: Callable[[str], None] = print) -> None:
        await (self.device_login(notify) if method == "device" else self.browser_login(notify))

    async def browser_login(self, notify: Callable[[str], None] = print) -> None:
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        result: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                parts = (await asyncio.wait_for(reader.readline(), timeout=5)).decode("ascii", "replace").split()
                parsed = urlsplit(parts[1] if len(parts) >= 2 and parts[0] == "GET" else "")
                query = parse_qs(parsed.query)
                code = query.get("code", [""])[0]
                ok = parsed.path == "/auth/callback" and bool(code) and hmac.compare_digest(query.get("state", [""])[0], state)
                if parsed.path == "/auth/callback" and not result.done():
                    result.set_result(code) if ok else result.set_exception(AuthError("codex: login was denied or invalid"))
                page = f"<html><body><p>{html.escape('Login complete. Return to Nexus.' if ok else 'Login failed.')}</p></body></html>".encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: " + str(len(page)).encode() + b"\r\n\r\n" + page)
                await writer.drain()
            finally:
                writer.close()

        server = await asyncio.start_server(callback, host="127.0.0.1", port=1455)
        try:
            params = urlencode({
                "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "scope": SCOPES,
                "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
                "id_token_add_organizations": "true", "codex_cli_simplified_flow": "true", "originator": "nexus",
            })
            url = f"{ISSUER}/oauth/authorize?{params}"
            if not webbrowser.open(url):
                notify(f"Open this URL in a browser:\n{url}")
            else:
                notify("Opened the browser for ChatGPT login…")
            code = await asyncio.wait_for(result, timeout=300)
        except TimeoutError as exc:
            raise AuthError("codex: browser login timed out") from exc
        finally:
            server.close()
            await server.wait_closed()
        self._persist(await _token({"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": REDIRECT_URI}))

    async def device_login(self, notify: Callable[[str], None] = print) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            start = await client.post(f"{ISSUER}/api/accounts/deviceauth/usercode", json={"client_id": CLIENT_ID})
            if start.status_code >= 400:
                raise AuthError(f"codex: could not start device login (HTTP {start.status_code})")
            data = start.json()
            code, device_id = data.get("user_code"), data.get("device_auth_id")
            notify(f"Open {DEVICE_URL} and enter code {code}")
            interval = max(1.0, float(data.get("interval") or 5))
            deadline = time.time() + 900
            while time.time() < deadline:
                await asyncio.sleep(interval)
                poll = await client.post(f"{ISSUER}/api/accounts/deviceauth/token", json={"device_auth_id": device_id, "user_code": code})
                if poll.status_code in (403, 404):
                    continue
                if poll.status_code >= 400:
                    raise AuthError(f"codex: device login failed (HTTP {poll.status_code})")
                result = poll.json()
                token = await _token({
                    "grant_type": "authorization_code", "code": result["authorization_code"],
                    "code_verifier": result["code_verifier"], "redirect_uri": f"{ISSUER}/deviceauth/callback",
                })
                self._persist(token)
                return
        raise AuthError("codex: device login timed out")


async def _token(data: dict[str, str]) -> Mapping[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{ISSUER}/oauth/token", data={**data, "client_id": CLIENT_ID})
    except httpx.HTTPError as exc:
        raise AuthError("codex: token request failed; check connectivity") from exc
    if response.status_code >= 400:
        raise AuthError(f"codex: token request failed (HTTP {response.status_code}); log in again")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise AuthError("codex: token response had no access token")
    return payload
