"""The gateway's client of workspace daemons: UDS ``WebLaunch`` -> loopback browser surface.

For each allowed workspace the gateway asks the daemon over its Unix socket for a
one-use launch URL, validates that URL is strictly loopback, redeems the ticket
for a cookie and CSRF token exactly like a local browser, and then speaks the
daemon's ``/v1/web/*`` routes. Credentials are cached in memory only (a bounded
number of hosts) and are never persisted or sent anywhere but the validated
loopback origin. A POST is never retried once it may have reached the host;
reconnecting before anything was sent, or re-reading with a GET, is allowed.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import msgspec

COOKIE_NAME = "nexus_web"
#: The host's cookie lives 12 hours; renew earlier than that.
CREDENTIAL_TTL = 11 * 60 * 60.0
MAX_HOSTS = 8
MAX_UPSTREAM_BODY = 33 * 1024 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,256}\Z")

Launcher = Callable[[Path], Awaitable[str]]


class UpstreamError(Exception):
    """A host could not be reached or refused; ``code`` is safe to show a device."""

    def __init__(self, code: str, status: int = 502, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.message = message or _MESSAGES.get(code, "the workspace host failed")


_MESSAGES = {
    "host_unavailable": "the workspace host is unavailable",
    "upstream_auth": "the host credential expired; retry the request",
    "response_too_large": "the host response is too large",
    "bad_launch": "the host returned an invalid launch address",
}


@dataclass(slots=True)
class UpstreamResponse:
    status: int
    headers: httpx.Headers
    body: bytes


@dataclass(slots=True)
class _Endpoint:
    base: str
    origin: str
    cookie: str
    csrf: str
    expires: float


def parse_launch_url(url: str) -> tuple[str, str, str]:
    """Validate a daemon ``WebLaunch`` URL; return ``(base, origin, ticket)``.

    Only ``http://<loopback IP literal>:<port>[/w/<project>]/#ticket=<token>`` is
    accepted, so a compromised or confused daemon reply can never point the
    gateway (and the ticket it redeems) at another machine. The shared daemon
    serves each project under ``/w/<project>``; that prefix joins ``base`` only.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise UpstreamError("bad_launch", 502) from exc
    ticket = re.fullmatch(r"ticket=([A-Za-z0-9_-]{16,256})", parts.fragment)
    project = re.fullmatch(r"/w/([A-Za-z0-9_-]{1,64})/?", parts.path)
    if (parts.path in ("", "/") or project is not None) and (
        parts.scheme == "http" and address.is_loopback and ticket is not None
    ):
        prefix = f"/w/{project.group(1)}" if project else ""
    else:
        prefix = None
    if (
        parts.scheme != "http"
        or not address.is_loopback
        or port is None
        or not 1 <= port <= 65535
        or parts.username is not None
        or parts.password is not None
        or prefix is None
        or parts.query
        or ticket is None
    ):
        raise UpstreamError("bad_launch", 502)
    authority = f"[{address}]" if address.version == 6 else str(address)
    origin = f"http://{authority}:{port}"
    return origin + prefix, origin, ticket.group(1)


async def daemon_launcher(workspace: Path) -> str:
    """Ask the workspace daemon (starting it if needed) for a browser launch URL."""
    from ..host import protocol as p
    from ..host.daemon import ensure_daemon

    client = await ensure_daemon(workspace, client="gateway")
    try:
        result = await client.call(p.WebLaunch(), timeout=10.0)
    finally:
        with contextlib.suppress(Exception):
            await client.close()
    if not isinstance(result, p.WebLaunchResult) or not result.url:
        raise UpstreamError("host_unavailable", 502)
    return result.url


def workspace_state(workspace: Path) -> str:
    """``running`` when the workspace daemon socket exists, else ``stopped``."""
    from ..host.daemon import default_socket_path

    try:
        return "running" if default_socket_path(workspace).exists() else "stopped"
    except Exception:  # noqa: BLE001 - an unusable path is simply not running
        return "stopped"


class Upstream:
    def __init__(
        self,
        *,
        launcher: Launcher = daemon_launcher,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_hosts: int = MAX_HOSTS,
    ) -> None:
        self._launcher = launcher
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=3.0, write=15.0, pool=5.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
        )
        self._clock = clock
        self._max_hosts = max_hosts
        self._endpoints: OrderedDict[str, _Endpoint] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Future[None]] = set()

    # -- credentials -------------------------------------------------------

    async def _endpoint(self, workspace: str, *, fresh: bool = False) -> _Endpoint:
        lock = self._locks.setdefault(workspace, asyncio.Lock())
        async with lock:
            current = self._endpoints.get(workspace)
            if current is not None and not fresh and current.expires > self._clock():
                self._endpoints.move_to_end(workspace)
                return current
            if current is not None:
                self._forget(workspace, current)
            endpoint = await self._establish(workspace)
            self._endpoints[workspace] = endpoint
            while len(self._endpoints) > self._max_hosts:
                old_workspace, old = self._endpoints.popitem(last=False)
                self._locks.pop(old_workspace, None)
                task = asyncio.ensure_future(self._logout(old))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
            return endpoint

    async def _establish(self, workspace: str) -> _Endpoint:
        try:
            url = await self._launcher(Path(workspace))
        except UpstreamError:
            raise
        except Exception as exc:  # noqa: BLE001 - daemon start failures are one generic error
            raise UpstreamError("host_unavailable", 502) from exc
        base, origin, ticket = parse_launch_url(url)
        try:
            response = await self._client.post(
                f"{base}/v1/web/ticket/redeem",
                content=msgspec.json.encode({"ticket": ticket}),
                headers={"Origin": origin, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise UpstreamError("host_unavailable", 502) from exc
        if response.status_code != 200:
            raise UpstreamError("host_unavailable", 502)
        try:
            csrf = msgspec.json.decode(response.content)["csrf"]
        except (msgspec.DecodeError, KeyError, TypeError) as exc:
            raise UpstreamError("host_unavailable", 502) from exc
        cookie = _cookie_value(response.headers.get_list("set-cookie"), _cookie_name(base))
        if not cookie or not isinstance(csrf, str) or not _TOKEN.fullmatch(csrf):
            raise UpstreamError("host_unavailable", 502)
        return _Endpoint(base, origin, cookie, csrf, self._clock() + CREDENTIAL_TTL)

    def _forget(self, workspace: str, endpoint: _Endpoint) -> None:
        if self._endpoints.get(workspace) is endpoint:
            del self._endpoints[workspace]

    async def _logout(self, endpoint: _Endpoint) -> None:
        with contextlib.suppress(Exception):
            await self._client.post(
                f"{endpoint.base}/v1/web/logout", headers=self._headers(endpoint, post=True), timeout=3.0
            )

    @staticmethod
    def _headers(endpoint: _Endpoint, *, post: bool, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Cookie": f"{_cookie_name(endpoint.base)}={endpoint.cookie}", "Origin": endpoint.origin}
        if post:
            headers["X-CSRF-Token"] = endpoint.csrf
        headers.update(extra or {})
        return headers

    # -- requests ----------------------------------------------------------

    async def request(
        self,
        workspace: str,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        content_type: str = "application/json",
        idempotent: bool,
        max_body: int = MAX_UPSTREAM_BODY,
    ) -> UpstreamResponse:
        """One request. Retries only what cannot have been delivered twice."""
        post = method != "GET"
        for attempt in (0, 1):
            endpoint = await self._endpoint(workspace, fresh=attempt == 1)
            extra = {"Content-Type": content_type} if content is not None else None
            try:
                async with self._client.stream(
                    method,
                    endpoint.base + path,
                    params=params,
                    content=content,
                    headers=self._headers(endpoint, post=post, extra=extra),
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > max_body:
                            raise UpstreamError("response_too_large", 502)
                    status, headers = response.status_code, response.headers
            except httpx.ConnectError as exc:
                # Nothing was sent: the host restarted on another port or is gone.
                self._forget(workspace, endpoint)
                if attempt == 0:
                    continue
                raise UpstreamError("host_unavailable", 502) from exc
            except httpx.HTTPError as exc:
                self._forget(workspace, endpoint)
                if idempotent and attempt == 0:
                    continue
                raise UpstreamError("host_unavailable", 502) from exc
            if status == 401:
                self._forget(workspace, endpoint)
                if (idempotent or not post) and attempt == 0:
                    continue
                raise UpstreamError("upstream_auth", 503)
            return UpstreamResponse(status, headers, bytes(body))
        raise UpstreamError("host_unavailable", 502)  # pragma: no cover - loop always returns

    @contextlib.asynccontextmanager
    async def stream(
        self, workspace: str, path: str, params: dict[str, str]
    ) -> AsyncIterator[httpx.Response]:
        """Open a GET stream (SSE); a stale credential or dead port is re-established once."""
        for attempt in (0, 1):
            endpoint = await self._endpoint(workspace, fresh=attempt == 1)
            manager = self._client.stream(
                "GET",
                endpoint.base + path,
                params=params,
                headers=self._headers(endpoint, post=False, extra={"Accept": "text/event-stream"}),
                timeout=httpx.Timeout(None, connect=3.0, read=45.0, write=5.0, pool=5.0),
            )
            try:
                response = await manager.__aenter__()
            except httpx.HTTPError as exc:
                self._forget(workspace, endpoint)
                if attempt == 0:
                    continue
                raise UpstreamError("host_unavailable", 502) from exc
            if response.status_code == 401 and attempt == 0:
                await manager.__aexit__(None, None, None)
                self._forget(workspace, endpoint)
                continue
            if response.status_code != 200:
                status = response.status_code
                await manager.__aexit__(None, None, None)
                if status in (400, 404):
                    raise UpstreamError("session_unavailable", 404, "the session is unavailable")
                raise UpstreamError("host_unavailable", 502)
            try:
                yield response
            finally:
                await manager.__aexit__(None, None, None)
            return
        raise UpstreamError("host_unavailable", 502)  # pragma: no cover

    async def aclose(self) -> None:
        endpoints = list(self._endpoints.values())
        self._endpoints.clear()
        await asyncio.gather(*(self._logout(e) for e in endpoints), return_exceptions=True)
        if self._owns_client:
            await self._client.aclose()


def _cookie_name(base: str) -> str:
    """The host suffixes its cookie with the project key when served under ``/w/<key>``."""
    key = urlsplit(base).path.removeprefix("/w/")
    return f"{COOKIE_NAME}_{key}" if key else COOKIE_NAME


def _cookie_value(headers: list[str], name: str = COOKIE_NAME) -> str | None:
    for header in headers:
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(header)
        except CookieError:
            continue
        morsel = jar.get(name)
        if morsel is not None and _TOKEN.fullmatch(morsel.value):
            return morsel.value
    return None


__all__ = [
    "COOKIE_NAME",
    "Upstream",
    "UpstreamError",
    "UpstreamResponse",
    "daemon_launcher",
    "parse_launch_url",
    "workspace_state",
]
