"""Nexus-owned OAuth, with read-only access to an explicitly selected CLI profile."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ...claude_usage import oauth_credentials
from .claude import child_env
from .claude_direct_store import DirectStore
from .errors import AuthError
from .store import CredentialError, CredentialStore

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
ENTRY = "claude-direct"


def valid_token(token: object) -> bool:
    return isinstance(token, str) and token.startswith("sk-ant-oat") and len(token) <= 16384 and not any(c.isspace() for c in token)


class ClaudeDirectAuth:
    methods = ("browser", "browser-long", "cli-profile")

    def __init__(self, store: CredentialStore | None = None, *, client: httpx.AsyncClient | None = None,
                 callback_port: int = 54545) -> None:
        self.store = DirectStore(store.path) if store else DirectStore()
        self.client = client
        self.callback_port = callback_port

    def _token(self) -> str | None:
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if token is not None:
            if not valid_token(token):
                return None
            return token
        entry = self.store.read(ENTRY)
        if entry:
            if entry.get("source") == "cli-profile":
                env = child_env()
                if entry.get("profile") != {k: env.get(k) for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR")}:
                    return None
                credential = oauth_credentials(env)
                return credential[0] if credential and valid_token(credential[0]) else None
            token = entry.get("access_token")
            return token if valid_token(token) and entry.get("expires_at", 0) > time.time() + 60 else None
        return None

    def logged_in(self) -> bool:
        try:
            entry = self.store.read(ENTRY)
            if not entry:
                return False
            if entry.get("source") == "cli-profile" or entry.get("grant") == "long-lived":
                return self._token() is not None
            expiry = entry.get("expires_at")
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
                return False
            return bool(entry.get("refresh_token")) or self._token() is not None
        except (CredentialError, OSError, ValueError, TypeError):
            return False

    def logout(self) -> None:
        if "CLAUDE_CODE_OAUTH_TOKEN" in os.environ:
            raise AuthError("claude-direct: unset CLAUDE_CODE_OAUTH_TOKEN to disconnect this environment credential")
        with self.store.locked():
            entry = self.store.read(ENTRY)
            if entry:
                self.store.write(ENTRY, None)
                return
        if self._token():
            raise AuthError("claude-direct: CLI credentials are read-only; disconnect the selected profile in Claude Code")

    async def _exchange(self, body: dict, *, refresh: bool = False) -> dict:
        client = self.client or await asyncio.to_thread(httpx.AsyncClient, timeout=20, follow_redirects=False)
        try:
            async with client.stream("POST", REFRESH_URL if refresh else TOKEN_URL,
                                     json={**body, "client_id": CLIENT_ID}, headers={"user-agent": "nexus/0.10"}) as response:
                if response.status_code != 200:
                    raise AuthError(f"claude-direct: OAuth HTTP {response.status_code}; sign in again")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise AuthError("claude-direct: OAuth response too large")
                data = json.loads(raw)
                if not isinstance(data, dict) or not valid_token(data.get("access_token")):
                    raise AuthError("claude-direct: OAuth response missing a usable access token")
                return data
        except (httpx.HTTPError, ValueError) as exc:
            raise AuthError("claude-direct: OAuth request failed") from exc
        finally:
            if self.client is None:
                await client.aclose()

    def _entry(self, data: dict, previous: dict | None = None, *, long_lived: bool = False) -> dict:
        previous = previous or {}
        refresh = data.get("refresh_token") or previous.get("refresh_token")
        if not long_lived and (not isinstance(refresh, str) or not refresh or len(refresh) > 16384 or any(c.isspace() for c in refresh)):
            raise AuthError("claude-direct: OAuth response missing a usable refresh token")
        expiry = data.get("expires_in")
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not 0 < expiry <= (31536000 if long_lived else 86400):
            raise AuthError("claude-direct: OAuth response has invalid expiry")
        entry = {"access_token": data["access_token"], "refresh_token": refresh, "expires_at": time.time() + expiry,
                 "authorized_at": previous.get("authorized_at", time.time())}
        if long_lived:
            entry["refresh_token"] = ""
            entry["grant"] = "long-lived"
        for key in ("account", "organization"):
            old_identity, new_identity = previous.get(key), data.get(key)
            if (isinstance(old_identity, dict) and isinstance(new_identity, dict)
                    and old_identity.get("uuid") and new_identity.get("uuid")
                    and old_identity["uuid"] != new_identity["uuid"]):
                raise AuthError("claude-direct: OAuth refresh identity changed; sign in again")
            value = previous.get(key) if previous else data.get(key)
            if isinstance(value, dict):
                fields = ("uuid", "email_address") if key == "account" else ("uuid", "name")
                identity = {k: v for k, v in value.items() if k in fields and isinstance(v, str) and 0 < len(v) <= 1024}
                if identity:
                    entry[key] = identity
        scope = data.get("scope", previous.get("scope"))
        if isinstance(scope, str) and len(scope) <= 4096:
            entry["scope"] = scope
        return entry

    async def login(self, method: str = "browser", notify: Callable[[str], None] = print,
                    browser_open: Callable[[str], bool] | None = None) -> None:
        if method in {"cli", "cli-profile"}:
            if await asyncio.to_thread(self.store.read, ENTRY):
                raise AuthError("claude-direct: Nexus browser credentials still take precedence; disconnect them before using CLI sign-in")
            env = child_env()
            credential = await asyncio.to_thread(oauth_credentials, env)
            if not credential or not valid_token(credential[0]):
                raise AuthError("claude-direct: no readable CLI OAuth file; sign in with Claude Code separately or use Nexus browser sign-in")
            async with self.store.alocked():
                self.store.write(ENTRY, {"source": "cli-profile", "profile": {
                    k: env.get(k) for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR")}})
            return
        if method not in {"browser", "browser-long"}:
            raise AuthError("claude-direct: unsupported sign-in method")
        long_lived = method == "browser-long"
        scopes = "user:inference user:profile" if long_lived else SCOPES
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        result = asyncio.get_running_loop().create_future()

        async def callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                line = await asyncio.wait_for(reader.readline(), 5)
                parts = line.decode("ascii", "replace").split()
                parsed = urlsplit(parts[1] if len(parts) >= 2 and parts[0] == "GET" else "")
                query = parse_qs(parsed.query)
                ok = (parsed.path == "/callback" and bool(query.get("code")) and not query.get("error")
                      and hmac.compare_digest(query.get("state", [""])[0], state))
                if ok and not result.done():
                    result.set_result(query["code"][0])
                page = b"Login complete. Return to Nexus." if ok else b"Invalid callback. Return to Nexus."
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\nContent-Length: " + str(len(page)).encode() + b"\r\n\r\n" + page)
                await writer.drain()
            except (TimeoutError, ConnectionError, ValueError):
                pass
            finally:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()

        server = await asyncio.start_server(callback, "127.0.0.1", self.callback_port)
        port = server.sockets[0].getsockname()[1]
        redirect = f"http://localhost:{port}/callback"
        url = "https://claude.ai/oauth/authorize?" + urlencode({"response_type": "code", "client_id": CLIENT_ID,
            "redirect_uri": redirect, "scope": scopes, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": state, "code": "true"})
        try:
            if not (browser_open or webbrowser.open)(url):
                notify(f"Open {url} to sign in to Claude.")
            code = await asyncio.wait_for(result, 300)
            data = await self._exchange({"grant_type": "authorization_code", "code": code,
                                         "code_verifier": verifier, "redirect_uri": redirect, "state": state,
                                         **({"expires_in": 31536000} if long_lived else {})})
            entry = self._entry(data, long_lived=long_lived)
            async with self.store.alocked():
                self.store.write(ENTRY, entry)
        except TimeoutError:
            raise AuthError("claude-direct: browser sign-in timed out") from None
        finally:
            server.close()
            await server.wait_closed()

    async def headers(self) -> dict[str, str]:
        if "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ:
            async with self.store.alocked():
                entry = self.store.read(ENTRY)
                if (entry and entry.get("source") != "cli-profile" and entry.get("expires_at", 0) <= time.time() + 300
                        and entry.get("grant") != "long-lived"):
                    if not entry.get("refresh_token"):
                        raise AuthError("claude-direct: stored login expired; sign in again")
                    data = await self._exchange({"grant_type": "refresh_token", "refresh_token": entry["refresh_token"],
                                                 "scope": SCOPES}, refresh=True)
                    self.store.write(ENTRY, self._entry(data, entry))
        token = await asyncio.to_thread(self._token)
        if token is None:
            raise AuthError("claude-direct: no usable OAuth access token; use Nexus browser sign-in or set CLAUDE_CODE_OAUTH_TOKEN. Nexus does not refresh CLI credentials.")
        return {"authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
                "user-agent": "nexus/0.10"}
