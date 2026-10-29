"""Outbound HTTP for `web_fetch`: a public-internet-only GET.

Adapted from the policy of old Nexus nexus/net/outbound.py, cut down to what one
GET needs. The model chooses the URL, so every hop is checked:

* http or https only, no credentials in the URL, a real host name or address;
* the host is resolved here and **every** address must be globally routable
  (loopback, private, link-local, metadata, multicast and IPv4-mapped forms of
  those are refused), then the connection is pinned to the checked address, so
  the name cannot resolve differently a moment later (DNS rebinding);
* redirects are followed only within the same origin, each hop checked again;
  a cross-origin redirect is reported to the model, which may fetch it itself;
* no proxy or environment configuration is read, and the body is read in chunks
  up to a byte limit, never in full.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2_000_000
USER_AGENT = "Nexus/0.10 (+web_fetch)"
ALLOW_NON_PUBLIC = False  # tests only: lets a local server stand in for the internet


class FetchError(Exception):
    """A fetch that was refused or failed; the message is safe to show the model."""


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: httpx.Headers
    body: bytes
    url: str  # the final URL
    truncated: bool  # the body was cut at the byte limit


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def origin(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port
    default = 443 if parts.scheme == "https" else 80
    authority = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{authority}" + (f":{port}" if port not in (None, default) else "")


def check_url(url: str) -> tuple[str, str, int]:
    """`(scheme, host, port)` of a URL this tool may fetch, or `FetchError`."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise FetchError("URL is not valid") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("URL must be an absolute http or https URL")
    if parts.username is not None or parts.password is not None:
        raise FetchError("URLs with credentials are not allowed")
    if any(ord(char) < 33 or ord(char) == 127 for char in url.strip()):
        raise FetchError("URL contains spaces or control characters")
    return parts.scheme, parts.hostname, port or (443 if parts.scheme == "https" else 80)


async def resolve(host: str, port: int) -> list[str]:
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        info = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError("host name could not be resolved") from exc
    return list(dict.fromkeys(item[4][0] for item in info))


async def _public_address(host: str, port: int) -> str:
    addresses = await resolve(host, port)
    if not addresses:
        raise FetchError("host name could not be resolved")
    if not ALLOW_NON_PUBLIC and not all(is_public(address) for address in addresses):
        raise FetchError("destination is not a public internet address")
    return addresses[0]


async def get(url: str, *, timeout: float = 15.0, max_bytes: int | None = None) -> Response:
    max_bytes = MAX_BODY_BYTES if max_bytes is None else max_bytes
    check_url(url)
    start = origin(url)
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout,
                                 headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _scheme, host, port = check_url(url)
            address = await _public_address(host, port)
            parts = urlsplit(url)
            literal = f"[{address}]" if ":" in address else address
            target = parts._replace(netloc=f"{literal}:{port}", fragment="").geturl()
            try:
                request = client.build_request("GET", target, headers={"Host": parts.netloc.rpartition("@")[2]},
                                               extensions={"sni_hostname": host})
                response = await client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise FetchError("request timed out") from exc
            except (httpx.HTTPError, OSError) as exc:
                raise FetchError("network request failed") from exc
            try:
                if response.status_code in (301, 302, 303, 307, 308) and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    if origin(url) != start:
                        raise FetchError(f"redirected to a different origin ({origin(url)}); "
                                         "fetch that URL directly if you want it")
                    continue
                body = bytearray()
                truncated = False
                try:
                    async for chunk in response.aiter_bytes():
                        room = max_bytes - len(body)
                        body.extend(chunk[:room])
                        if len(chunk) > room:
                            truncated = True
                            break
                except httpx.TimeoutException as exc:
                    raise FetchError("request timed out") from exc
                except (httpx.HTTPError, OSError) as exc:
                    raise FetchError("network request failed") from exc
                return Response(response.status_code, response.headers, bytes(body), url, truncated)
            finally:
                await response.aclose()
    raise FetchError(f"more than {MAX_REDIRECTS} redirects")
