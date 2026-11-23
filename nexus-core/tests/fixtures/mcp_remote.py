"""A remote MCP server behind real OAuth (SDK authorization server, in-memory provider) for tests.

`serve(oauth=True)` starts uvicorn on a free loopback port in a thread and returns a handle. The provider
approves every authorization at once, so a test's "browser" is a GET on the sign-in link. `serve(oauth=False)`
checks one static bearer token instead (the `headers` path). Tokens last `ttl` seconds so a test can expire one.
"""
from __future__ import annotations

import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenVerifier,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp_types import ToolAnnotations


class Provider:
    def __init__(self, ttl: int) -> None:
        self.ttl = ttl
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}
        self.registrations = 0
        self.accept = True  # False: every token it issues is refused at the resource
        self.refreshes = 0

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.registrations += 1
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 300, client_id=client.client_id or "",
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, code: str) -> AuthorizationCode | None:
        return self.codes.get(code)

    def _issue(self, client_id: str, scopes: list[str], resource: str | None = None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.access[access] = AccessToken(token=access, client_id=client_id, scopes=scopes,
                                          expires_at=int(time.time()) + self.ttl, resource=resource)
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes, resource=resource)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=self.ttl,
                          scope=" ".join(scopes) or None, refresh_token=refresh)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id or "", authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        self.refreshes += 1
        self.refresh.pop(refresh_token.token, None)
        return self._issue(client.client_id or "", scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        found = self.access.get(token)
        return found if self.accept and found and (found.expires_at or 0) > time.time() else None

    async def revoke_token(self, token: Any) -> None:
        self.access.pop(getattr(token, "token", ""), None)
        self.refresh.pop(getattr(token, "token", ""), None)


class StaticToken(TokenVerifier):
    def __init__(self, token: str) -> None:
        self.token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        return AccessToken(token=token, client_id="static", scopes=[]) if token == self.token else None


@dataclass
class Remote:
    url: str
    base: str
    provider: Provider | None
    server: uvicorn.Server
    thread: threading.Thread
    seen: list[str] = field(default_factory=list)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(*, oauth: bool = True, ttl: int = 3600, static_token: str = "s3cret", registration: bool = True,
          accept_tokens: bool = True) -> Remote:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    provider = Provider(ttl) if oauth else None
    if provider is not None:
        provider.accept = accept_tokens
    kwargs: dict[str, Any]
    if oauth:
        kwargs = {"auth_server_provider": provider, "auth": AuthSettings(
            issuer_url=base, resource_server_url=f"{base}/mcp", validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(enabled=registration, valid_scopes=["read"],
                                                                  default_scopes=["read"]))}
    else:
        kwargs = {"token_verifier": StaticToken(static_token), "auth": AuthSettings(
            issuer_url=base, resource_server_url=f"{base}/mcp")}
    app = MCPServer("remote-fixture", **kwargs)

    @app.tool(annotations=ToolAnnotations(read_only_hint=True))
    def echo(text: str) -> str:
        """Echo the text back, upper-cased."""
        return text.upper()

    config = uvicorn.Config(app.streamable_http_app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("the fixture server did not start")
    return Remote(url=f"{base}/mcp", base=base, provider=provider, server=server, thread=thread)
