"""OAuth for remote MCP servers (authorization code with PKCE, through the SDK's `OAuthClientProvider`).

Two ways to use a provider:

- **Connecting** (`Connection._run`) is never interactive. It sends the stored token, refreshes it when it
  has expired, and when the server still answers 401 it raises `AuthRequired`: the server shows
  `auth: "required"` and waits for the user to sign in. A turn never opens a browser.
- **Signing in** (`login`) is the only interactive path, started by the user (`POST …/login`, or
  `nexus mcp login`). The daemon never launches a browser: the sign-in URL goes to the client, which opens
  it. A loopback listener on `127.0.0.1` takes the redirect, the SDK exchanges the code, and the tokens and
  the registered client are stored in `auth.json` (`oauth_store.py`), never in `config.toml`.

A server that does not publish OAuth metadata, or does not register clients dynamically, is signed in with a
pre-registered client: `oauth = { client_id = "…", client_secret = "…", redirect_port = 18791 }` (register
`http://127.0.0.1:<redirect_port>/callback` with the provider).
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import oauth_store
from .config import ServerConfig

LOGIN_TIMEOUT = 300.0
DEFAULT_REDIRECT_PORT = 18791  # only for a pre-registered client, whose redirect URI must be known in advance
CLIENT_NAME = "Nexus"


class AuthRequired(Exception):
    """The server wants OAuth and no usable token is stored."""


class LoginError(Exception):
    """A sign-in did not finish; the message is safe to show."""


class Storage:
    """The SDK's `TokenStorage`, backed by one `auth.json` entry."""

    def __init__(self, entry_key: str, preset: Any = None) -> None:
        self.entry_key = entry_key
        self.preset = preset  # a pre-registered client from the entry's `oauth` table

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken
        raw = (await asyncio.to_thread(oauth_store.read, self.entry_key)).get("tokens")
        if not isinstance(raw, dict):
            return None
        try:
            return OAuthToken.model_validate(raw)
        except ValueError:
            return None

    async def set_tokens(self, tokens: Any) -> None:
        data = tokens.model_dump(mode="json", exclude_none=True)
        expires = time.time() + tokens.expires_in if tokens.expires_in is not None else None
        await asyncio.to_thread(oauth_store.update, self.entry_key, tokens=data, expires_at=expires)

    async def get_client_info(self) -> Any:
        if self.preset is not None:
            return self.preset
        from mcp.shared.auth import OAuthClientInformationFull
        raw = (await asyncio.to_thread(oauth_store.read, self.entry_key)).get("client")
        if not isinstance(raw, dict):
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except ValueError:
            return None

    async def set_client_info(self, client_info: Any) -> None:
        await asyncio.to_thread(oauth_store.update, self.entry_key,
                                client=client_info.model_dump(mode="json", exclude_none=True))

    async def expires_at(self) -> float | None:
        value = (await asyncio.to_thread(oauth_store.read, self.entry_key)).get("expires_at")
        return float(value) if isinstance(value, int | float) else None


def redirect_port(config: ServerConfig) -> int:
    """0 (any free port) for a registered-on-the-fly client; the configured port for a pre-registered one."""
    table = config.oauth or {}
    if "redirect_port" in table:
        return int(table["redirect_port"])
    return DEFAULT_REDIRECT_PORT if "client_id" in table else 0


def make_provider(scope: str, config: ServerConfig, *, redirect_uri: str,
                  redirect_handler: Callable[[str], Awaitable[None]],
                  callback_handler: Callable[[], Awaitable[Any]]) -> Any:
    """An SDK provider for one server. Imported here so the SDK loads only when OAuth is used."""
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
    table = config.oauth or {}
    preset = None
    if "client_id" in table:
        preset = OAuthClientInformationFull(
            client_id=table["client_id"], client_secret=table.get("client_secret"), redirect_uris=[redirect_uri],
            token_endpoint_auth_method="client_secret_post" if table.get("client_secret") else "none")
    storage = Storage(oauth_store.key(scope, config), preset)
    metadata = OAuthClientMetadata(
        client_name=CLIENT_NAME, redirect_uris=[redirect_uri], grant_types=["authorization_code", "refresh_token"],
        response_types=["code"], token_endpoint_auth_method="none", scope=table.get("scope"))

    class Provider(OAuthClientProvider):
        async def _initialize(self) -> None:
            await super()._initialize()
            # The SDK only learns an expiry from a token response in this process; after a restart the stored
            # one is the only record, and without it an expired token would be sent and answered 401.
            self.context.token_expiry_time = await storage.expires_at()

    return Provider(config.url, metadata, storage, redirect_handler, callback_handler)


def unauthorized_probe(on_required: Callable[[], None]) -> Any:
    """A plain `httpx2.Auth` for a connection with no stored sign-in: it sends nothing extra and notes a 401.

    The SDK reports a 401 as a generic error, so this is how "the server wants a sign-in" is told from
    "the server is down". It never registers a client or opens a browser.
    """
    import httpx2

    class Probe(httpx2.Auth):
        async def async_auth_flow(self, request: Any) -> Any:
            response = yield request
            if response.status_code == 401:
                on_required()

    return Probe()


def connection_auth(scope: str, config: ServerConfig, on_required: Callable[[], None]) -> Any:
    """The non-interactive provider a connection uses: it refreshes, and otherwise reports `AuthRequired`."""
    async def redirect(_url: str) -> None:
        on_required()
        raise AuthRequired("sign-in required: sign in to this server in Settings, or run `nexus mcp login`")

    async def callback() -> Any:  # pragma: no cover - the redirect handler always raises first
        raise AuthRequired("sign-in required")

    return make_provider(scope, config, redirect_uri=f"http://127.0.0.1:{redirect_port(config) or DEFAULT_REDIRECT_PORT}/callback",
                         redirect_handler=redirect, callback_handler=callback)


class _Callback:
    """One loopback listener that takes the provider's redirect."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.result: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.server: asyncio.Server | None = None

    async def start(self) -> str:
        try:
            self.server = await asyncio.start_server(self._handle, host="127.0.0.1", port=self.port)
        except OSError as exc:
            raise LoginError(f"port {self.port} is in use; set oauth.redirect_port to a free port"
                             if self.port else "could not listen for the sign-in redirect") from exc
        self.port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}/callback"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        from mcp.shared.auth import AuthorizationCodeResult
        ok = False
        try:
            parts = (await asyncio.wait_for(reader.readline(), timeout=5)).decode("ascii", "replace").split()
            parsed = urlsplit(parts[1] if len(parts) >= 2 and parts[0] == "GET" else "")
            query = parse_qs(parsed.query)
            if parsed.path == "/callback" and not self.result.done():
                error, code = query.get("error", [""])[0], query.get("code", [""])[0]
                if error or not code:
                    self.result.set_exception(LoginError(f"the server refused the sign-in ({error or 'no code'})"))
                else:
                    ok = True
                    self.result.set_result(AuthorizationCodeResult(
                        code=code, state=query.get("state", [None])[0], iss=query.get("iss", [None])[0]))
            message = "Signed in. Return to Nexus." if ok else "Sign-in failed. Return to Nexus."
            page = f"<html><body><p>{html.escape(message)}</p></body></html>".encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n"
                         b"Content-Length: " + str(len(page)).encode() + b"\r\n\r\n" + page)
            await writer.drain()
        except (TimeoutError, ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def wait(self) -> Any:
        return await self.result

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


async def login(scope: str, config: ServerConfig, notify_url: Callable[[str], None]) -> None:
    """Sign in to one server: hand the sign-in URL to `notify_url`, wait for the redirect, store the tokens.

    Earlier tokens are dropped first, and a dynamically registered client is registered again, because its
    redirect URI carries this run's port. Raises `LoginError` with a message that names no secret.
    """
    from . import manager
    manager._load_mcp()
    entry_key = oauth_store.key(scope, config)
    await asyncio.to_thread(oauth_store.clear, entry_key)
    listener = _Callback(redirect_port(config))
    redirect_uri = await listener.start()

    asked = 0

    async def redirect(url: str) -> None:
        nonlocal asked
        asked += 1
        if asked > 1:
            # The server answered 401 again after it issued a token: signing in again cannot fix that.
            raise LoginError("the server did not accept the token it issued; check the address and the scopes")
        notify_url(url)

    provider = make_provider(scope, config, redirect_uri=redirect_uri, redirect_handler=redirect,
                             callback_handler=listener.wait)
    from mcp import Client
    try:
        async with asyncio.timeout(LOGIN_TIMEOUT):
            async with Client(manager._transport(config, None, auth=provider),
                              read_timeout_seconds=config.connect_timeout):
                pass
    except TimeoutError as exc:
        raise LoginError("sign-in timed out") from exc
    except LoginError:
        raise
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 - an ExceptionGroup from the transport's task group
        raise LoginError(_reason(exc)) from None
    finally:
        await listener.close()
    if not oauth_store.signed_in(scope, config):
        raise LoginError("the server did not return a token")


def _reason(exc: BaseException) -> str:
    from .bridge import sanitize
    if isinstance(exc, LoginError):
        return str(exc)
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(dict.fromkeys(_reason(e) for e in exc.exceptions))
    text = sanitize(str(exc), keep_newlines=False).strip()[:200]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class Logins:
    """The daemon's running and finished sign-ins, one per (scope, server)."""

    def __init__(self, publish: Callable[[str, dict[str, Any]], Any]) -> None:
        self.publish = publish
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.states: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _id(scope: str, name: str) -> str:
        return f"{scope}:{name}"

    def state(self, scope: str, name: str) -> dict[str, Any]:
        return self.states.get(self._id(scope, name), {"status": "idle"})

    def running(self, scope: str, name: str) -> bool:
        task = self.tasks.get(self._id(scope, name))
        return task is not None and not task.done()

    def start(self, scope: str, config: ServerConfig, extra: dict[str, Any],
              done: Callable[[], Awaitable[None]]) -> dict[str, Any]:
        ident = self._id(scope, config.name)
        state: dict[str, Any] = {"server": config.name, "status": "pending", "message": None, **extra}
        self.states[ident] = state

        def notify(url: str) -> None:
            state.update(verification_uri=url, message="Open the link to sign in.")
            self.publish("mcp.login", dict(state))

        async def run() -> None:
            try:
                await login(scope, config, notify)
            except asyncio.CancelledError:
                raise
            except LoginError as exc:
                state.update(status="failed", message=str(exc), verification_uri=None)
            except Exception:  # noqa: BLE001 - never leak a provider failure or a credential
                state.update(status="failed", message="Sign-in failed; try again.", verification_uri=None)
            else:
                # Reconnect before reporting success, so a client that sees `signed_in` also sees the server's
                # state after the sign-in, never the `connecting` in between.
                with contextlib.suppress(Exception):
                    await done()
                state.update(status="signed_in", message=None, verification_uri=None)
            self.publish("mcp.login", dict(state))

        self.tasks[ident] = asyncio.create_task(run(), name=f"mcp-login-{config.name}")
        return dict(state)

    async def cancel(self, scope: str, name: str) -> None:
        ident = self._id(scope, name)
        task = self.tasks.pop(ident, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.states.pop(ident, None)
