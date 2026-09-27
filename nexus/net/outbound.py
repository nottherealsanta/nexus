"""Pinned, public-address-only HTTP transport for outbound service calls.

The transport validates each request URL and resolves hostnames exactly once
when a new socket is opened. The vetted numeric address is passed to httpcore's
network backend, while httpcore retains the original URL host for HTTP Host,
TLS SNI, and certificate hostname verification.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import socket
import ssl
import time
import zlib
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from typing import Any, Protocol, Self
from urllib.parse import urlsplit

import httpcore
import httpx


class OutboundError(Exception):
    """Base class for outbound transport failures."""


class URLValidationError(OutboundError, ValueError):
    """The supplied URL is not an acceptable canonical HTTP(S) URL."""


class ResolutionError(OutboundError):
    """DNS resolution failed or returned no usable addresses."""


class AddressPolicyError(OutboundError):
    """A host resolved to an address outside the public-address policy."""


class PeerVerificationError(OutboundError):
    """The connected socket peer differs from its pinned destination."""


class OutboundResponseTooLarge(OutboundError):
    """An outbound response exceeded a configured byte limit."""


class OutboundServiceError(OutboundError):
    """A redacted, categorized failure from the bounded GET service."""

    category = "outbound"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.category.replace("_", " "))


class OutboundPolicyError(OutboundServiceError):
    """The URL or one of its redirect targets violates outbound policy."""

    category = "policy"


class OutboundNetworkError(OutboundServiceError):
    """DNS, connection, protocol, or response decoding failed."""

    category = "network"


class OutboundDeadlineError(OutboundServiceError, TimeoutError):
    """The complete outbound operation exceeded its deadline."""

    category = "timeout"


class OutboundRedirectError(OutboundServiceError):
    """A redirect chain was malformed, cyclic, or too long."""

    category = "redirect"


class OutboundRateLimitError(OutboundServiceError):
    """The remote server returned HTTP 429."""

    category = "rate_limited"

    def __init__(self, status_code: int = 429) -> None:
        self.status_code = status_code
        super().__init__("remote server rate limited the request")


class OutboundHTTPStatusError(OutboundServiceError):
    """The remote server returned a non-success HTTP status."""

    category = "http_status"

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"remote server returned HTTP {status_code}")


class OutboundUnsupportedStatusError(OutboundServiceError):
    """The remote server returned a status the GET service does not handle."""

    category = "unsupported_status"

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"remote server returned unsupported HTTP {status_code}")


_DEFAULT_TIMEOUT = 10.0
_MAX_TIMEOUT = 30.0
_SERVICE_TIMEOUT = 15.0
_MAX_WIRE_BYTES = 2 * 1024 * 1024
_MAX_DECODED_BYTES = 4 * 1024 * 1024
_WIRE_CHUNK_BYTES = 256
_SERVICE_MAX_REDIRECTS = 5
_SERVICE_DEADLINE = 20.0
_SERVICE_CONNECT_TIMEOUT = 5.0
_SERVICE_READ_TIMEOUT = 10.0
_SERVICE_CONCURRENCY = 8
_SERVICE_RATE = 30.0 / 60.0

_SENSITIVE_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "proxy-connection",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


@dataclass(frozen=True, slots=True)
class CanonicalURL:
    url: httpx.URL
    scheme: str
    host: str
    port: int


def _canonical_host(host: str) -> str:
    if not host or len(host) > 253 or "%" in host or host.endswith("."):
        raise URLValidationError("host is empty, scoped, or has a trailing dot")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Reject legacy numeric IPv4 forms that some resolvers interpret as
        # addresses even though ipaddress correctly refuses to parse them.
        if re.fullmatch(r"[0-9.]+", host) or re.fullmatch(r"0[xX][0-9a-fA-F.]+", host):
            raise URLValidationError("non-canonical numeric host") from None
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise URLValidationError("invalid internationalized hostname") from exc
        if len(ascii_host) > 253:
            raise URLValidationError("hostname is too long")
        labels = ascii_host.split(".")
        if any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in labels
        ):
            raise URLValidationError("hostname is not a canonical DNS name")
        return ascii_host

    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None or address.scope_id is not None:
            raise URLValidationError("IPv4-mapped and scoped IPv6 hosts are forbidden")
        return address.compressed
    if str(address) != host:
        raise URLValidationError("IPv4 host must use canonical dotted-decimal form")
    return str(address)


def _is_forbidden_metadata_name(host: str) -> bool:
    name = host.rstrip(".").lower()
    return name in {
        "metadata",
        "metadata.google.internal",
        "metadata.google",
        "metadata.azure.internal",
        "metadata.azure.com",
        "instance-data",
        "instance-data.ec2.internal",
    } or name.endswith(".metadata")


def canonicalize_url(value: str | httpx.URL) -> CanonicalURL:
    """Validate and canonicalize an absolute HTTP(S) URL without resolving it."""
    raw = str(value)
    if not raw or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in raw):
        raise URLValidationError("URL contains whitespace or control characters")
    if "\\" in raw:
        raise URLValidationError("backslashes are forbidden in URLs")
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"} or not parts.netloc:
            raise URLValidationError("only absolute HTTP and HTTPS URLs are allowed")
        if parts.fragment:
            raise URLValidationError("URL fragments are forbidden")
        if "@" in parts.netloc:
            raise URLValidationError("URL userinfo is forbidden")
        host = parts.hostname
        if host is None:
            raise URLValidationError("URL must include a hostname")
        host = _canonical_host(host)
        if host in {
            "localhost",
            "localhost.localdomain",
            "localhost6",
            "localhost6.localdomain6",
            "ip6-localhost",
            "ip6-loopback",
        } or host.endswith(".localhost"):
            raise AddressPolicyError("localhost destinations are forbidden")
        if _is_forbidden_metadata_name(host):
            raise AddressPolicyError("metadata service hostnames are forbidden")
        port = (
            parts.port if parts.port is not None else (443 if scheme == "https" else 80)
        )
        if port == 0:
            raise URLValidationError("port 0 is forbidden")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            _parse_address(literal)
    except URLValidationError:
        raise
    except (ValueError, UnicodeError) as exc:
        raise URLValidationError("malformed URL") from exc

    # urlsplit accepts several ambiguous authority forms. Requiring its parsed
    # authority to consist solely of the canonical host and optional port keeps
    # these from being interpreted differently by an HTTP parser.
    authority_host = f"[{host}]" if ":" in host else host
    explicit_port = parts.port
    canonical_authority = authority_host
    if explicit_port is not None:
        canonical_authority += f":{explicit_port}"
    if parts.netloc.lower() != canonical_authority.lower():
        raise URLValidationError("URL authority is not canonical")

    path = parts.path or "/"
    canonical = f"{scheme}://{canonical_authority}{path}"
    if parts.query:
        canonical += f"?{parts.query}"
    try:
        url = httpx.URL(canonical)
    except (TypeError, ValueError) as exc:
        raise URLValidationError("malformed URL") from exc
    return CanonicalURL(url=url, scheme=scheme, host=host, port=port)


def _bounded_timeout(extensions: Mapping[str, Any]) -> dict[str, float]:
    """Return only validated, finite, bounded httpcore timeout values."""
    value = extensions.get("timeout")
    if value is None:
        supplied: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        supplied = value
    else:
        raise ValueError("outbound timeout extension must be a mapping")
    if set(supplied) - {"connect", "read", "write", "pool"}:
        raise ValueError("outbound timeout contains unsupported phases")

    result: dict[str, float] = {}
    for phase in ("connect", "read", "write", "pool"):
        timeout = supplied.get(phase)
        if timeout is None:
            result[phase] = _DEFAULT_TIMEOUT
            continue
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("outbound timeout values must be finite numbers")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("outbound timeout values must be positive and finite")
        result[phase] = min(float(timeout), _MAX_TIMEOUT)
    return result


def _request_headers(
    request: httpx.Request, canonical: CanonicalURL
) -> list[tuple[bytes, bytes]]:
    connection_tokens: set[bytes] = set()
    for key, value in request.headers.raw:
        if key.lower() == b"connection":
            connection_tokens.update(
                token.strip().lower() for token in value.split(b",") if token.strip()
            )

    sensitive = {b"authorization", b"proxy-authorization", b"cookie"}
    if any(key.lower() in sensitive for key, _ in request.headers.raw):
        raise ValueError("sensitive outbound request headers are forbidden")

    headers = [
        (key, value)
        for key, value in request.headers.raw
        if key.lower() != b"host"
        and key.lower()
        not in {name.encode("ascii") for name in _SENSITIVE_REQUEST_HEADERS}
        and not key.lower().startswith(b"proxy-")
        and key.lower() not in connection_tokens
    ]
    authority_host = f"[{canonical.host}]" if ":" in canonical.host else canonical.host
    default_port = 443 if canonical.scheme == "https" else 80
    authority = authority_host
    if canonical.port != default_port:
        authority += f":{canonical.port}"
    headers.append((b"host", authority.encode("ascii")))
    return headers


def _parse_address(value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        address = value
    else:
        try:
            address = ipaddress.ip_address(str(value))
        except ValueError as exc:
            raise ResolutionError(
                f"resolver returned an invalid IP address: {value!r}"
            ) from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        raise AddressPolicyError(f"IPv4-mapped IPv6 address is forbidden: {address}")
    if not address.is_global or address.is_loopback or address.is_link_local:
        raise AddressPolicyError(
            f"non-public destination address is forbidden: {address}"
        )
    return address


Resolver = Callable[
    [str],
    Awaitable[Sequence[str | ipaddress.IPv4Address | ipaddress.IPv6Address]],
]


async def _system_resolver(host: str) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ResolutionError(f"could not resolve outbound host {host!r}") from exc
    return tuple(record[4][0] for record in records)


class _PeerCheckedStream(httpcore.AsyncNetworkStream):
    def __init__(
        self,
        stream: httpcore.AsyncNetworkStream,
        expected: ipaddress.IPv4Address | ipaddress.IPv6Address,
        port: int,
    ) -> None:
        self._stream = stream
        self._expected = expected
        self._port = port

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Deliberately forward httpcore's origin hostname unchanged: it drives
        # both SNI and certificate hostname verification after numeric connect.
        secure_stream = await self._stream.start_tls(
            ssl_context, server_hostname=server_hostname, timeout=timeout
        )
        return _PeerCheckedStream(secure_stream, self._expected, self._port)

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class PinnedAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve, vet, and connect numerically through a public httpcore hook."""

    def __init__(
        self,
        resolver: Resolver | None = None,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolver = resolver or _system_resolver
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        canonical_host = _canonical_host(host)
        if _is_forbidden_metadata_name(canonical_host):
            raise AddressPolicyError(
                f"metadata hostname is forbidden: {canonical_host}"
            )

        try:
            literal = ipaddress.ip_address(canonical_host)
        except ValueError:
            try:
                if timeout is None:
                    resolved = await self._resolver(canonical_host)
                else:
                    async with asyncio.timeout(timeout):
                        resolved = await self._resolver(canonical_host)
            except TimeoutError:
                raise
            except OutboundError:
                raise
            except Exception as exc:
                raise ResolutionError(
                    f"could not resolve outbound host {canonical_host!r}"
                ) from exc
            if not resolved:
                raise ResolutionError(
                    f"host {canonical_host!r} resolved to no addresses"
                )
            # Vet the entire answer set before opening any socket. Mixed public
            # and private answers are rejected, not partially accepted.
            addresses = tuple(_parse_address(address) for address in resolved)
        else:
            addresses = (_parse_address(literal),)

        # One connection uses one deterministic vetted address. No hostname is
        # passed to the connector, so its own resolver cannot rebind the target.
        selected = addresses[0]
        stream = await self._backend.connect_tcp(
            str(selected),
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        try:
            peer = stream.get_extra_info("server_addr")
            if not isinstance(peer, tuple) or not peer:
                raise PeerVerificationError(
                    "connected stream did not expose its peer address"
                )
            actual = ipaddress.ip_address(str(peer[0]))
            peer_port = int(peer[1]) if len(peer) > 1 else port
            if actual != selected or peer_port != port:
                raise PeerVerificationError(
                    f"connected peer {actual}:{peer_port} differs from pinned target {selected}:{port}"
                )
        except (ValueError, TypeError) as exc:
            await stream.aclose()
            raise PeerVerificationError(
                "connected stream exposed an invalid peer address"
            ) from exc
        except BaseException:
            await stream.aclose()
            raise
        return _PeerCheckedStream(stream, selected, port)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise AddressPolicyError("Unix-domain outbound sockets are forbidden")


class _HTTPXResponseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        stream: httpcore.AsyncByteStream,
        *,
        max_bytes: int | None = None,
        decoded_limit: int | None = None,
        content_encoding: str = "identity",
        truncate: bool = False,
        original_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._decoded_limit = decoded_limit
        self._content_encoding = content_encoding
        self._truncate = truncate
        self.truncated = False
        self.original_headers = original_headers

    async def __aiter__(self) -> AsyncIterator[bytes]:
        wire_received = 0
        decoded_received = 0
        if self._content_encoding == "gzip":
            decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
        elif self._content_encoding == "deflate":
            decoder = zlib.decompressobj()
        else:
            decoder = None
        async for chunk in self._stream:
            for offset in range(0, len(chunk), _WIRE_CHUNK_BYTES):
                part = chunk[offset : offset + _WIRE_CHUNK_BYTES]
                if self._max_bytes is not None:
                    wire_remaining = self._max_bytes - wire_received
                    if len(part) > wire_remaining:
                        if not self._truncate:
                            raise OutboundResponseTooLarge(
                                "outbound response exceeded wire byte limit"
                            )
                        part = part[:wire_remaining]
                        self.truncated = True
                    wire_received += len(part)

                if decoder is None:
                    decoded = part
                else:
                    try:
                        decoded = decoder.decompress(
                            part,
                            (self._decoded_limit or _MAX_DECODED_BYTES)
                            - decoded_received
                            + 1,
                        )
                    except zlib.error as exc:
                        raise ValueError(
                            "invalid compressed outbound response"
                        ) from exc
                if self._decoded_limit is not None:
                    if decoded_received + len(decoded) > self._decoded_limit:
                        if not self._truncate:
                            raise OutboundResponseTooLarge(
                                "outbound response exceeded decoded byte limit"
                            )
                        decoded = decoded[: self._decoded_limit - decoded_received]
                        self.truncated = True
                    decoded_received += len(decoded)
                if decoded:
                    yield decoded
                if self.truncated:
                    return

        if decoder is not None:
            remaining = (self._decoded_limit or _MAX_DECODED_BYTES) - decoded_received
            try:
                flushed = decoder.flush(remaining + 1)
            except zlib.error as exc:
                raise ValueError("invalid compressed outbound response") from exc
            if self._decoded_limit is not None and len(flushed) > remaining:
                if not self._truncate:
                    raise OutboundResponseTooLarge(
                        "outbound response exceeded decoded byte limit"
                    )
                flushed = flushed[:remaining]
                self.truncated = True
            if flushed:
                yield flushed

    async def aclose(self) -> None:
        await self._stream.aclose()


class OutboundTransport(httpx.AsyncBaseTransport):
    """HTTPX transport backed by a no-proxy, pinned httpcore connection pool."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        backend: httpcore.AsyncNetworkBackend | None = None,
        max_connections: int = 10,
        max_keepalive_connections: int = 5,
        max_response_bytes: int | None = None,
        truncate_response: bool = False,
    ) -> None:
        self._max_response_bytes = max_response_bytes
        self._truncate_response = truncate_response
        network_backend = PinnedAsyncNetworkBackend(resolver=resolver, backend=backend)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            proxy=None,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            http1=True,
            http2=False,
            retries=0,
            network_backend=network_backend,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        canonical = canonicalize_url(request.url)
        timeout = _bounded_timeout(request.extensions)
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=canonical.url.raw_scheme,
                host=canonical.url.raw_host,
                port=canonical.url.port,
                target=canonical.url.raw_path,
            ),
            headers=_request_headers(request, canonical),
            content=request.stream,
            extensions={"timeout": timeout},
        )
        response = await self._pool.handle_async_request(core_request)
        response_headers = response.headers
        content_encoding = "identity"
        if self._max_response_bytes is not None:
            content_encoding = (
                httpx.Headers(response.headers)
                .get("content-encoding", "identity")
                .lower()
            )
            if content_encoding not in {"identity", "gzip", "deflate"}:
                await response.aclose()
                raise ValueError("unsupported outbound response content encoding")
            if content_encoding != "identity":
                response_headers = [
                    (key, value)
                    for key, value in response.headers
                    if key.lower() not in {b"content-encoding", b"content-length"}
                ]
        body_stream = _HTTPXResponseStream(
            response.stream,
            max_bytes=self._max_response_bytes,
            decoded_limit=(
                _MAX_DECODED_BYTES if self._max_response_bytes is not None else None
            ),
            content_encoding=content_encoding,
            truncate=self._truncate_response,
            original_headers=(
                list(response.headers) if self._max_response_bytes is not None else None
            ),
        )
        return httpx.Response(
            status_code=response.status,
            headers=response_headers,
            stream=body_stream,
            extensions={"outbound_stream": body_stream},
            request=request,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


@dataclass(frozen=True, slots=True)
class OutboundHTTPResponse:
    """A bounded, materialized outbound HTTP response."""

    status_code: int
    headers: httpx.Headers
    body: bytes
    requested_url: str = ""
    final_url: str = ""
    truncated: bool = False


class OutboundHTTPService(Protocol):
    """Public interface implemented by the hardened outbound HTTP service."""

    async def get(
        self,
        url: str | httpx.URL,
        *,
        cancel: asyncio.Event | None = None,
        allowed_hosts: Iterable[str] | None = None,
        allowed_origins: Iterable[str] | None = None,
    ) -> OutboundHTTPResponse: ...


class _TokenBucket:
    def __init__(
        self, clock: Callable[[], float], sleeper: Callable[[float], Awaitable[None]]
    ):
        self._clock = clock
        self._sleeper = sleeper
        self._tokens = 30.0
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self, deadline: float) -> None:
        while True:
            async with self._lock:
                now = self._clock()
                self._tokens = min(
                    30.0,
                    self._tokens + max(0.0, now - self._updated) * _SERVICE_RATE,
                )
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                delay = (1 - self._tokens) / _SERVICE_RATE
            remaining = deadline - self._clock()
            if remaining <= 0 or delay >= remaining:
                raise OutboundDeadlineError()
            await self._sleeper(delay)


class SafeOutboundHTTPService:
    """Bounded GET-only service with per-hop DNS pinning and request limits.

    Concurrency and the token bucket are shared by requests made through this
    service instance. Injected clock and sleeper make throttling deterministic
    in tests.
    """

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        backend: httpcore.AsyncNetworkBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._resolver = resolver
        self._backend = backend
        self._clock = clock
        self._sleeper = sleeper
        self._deadline_seconds = _SERVICE_DEADLINE
        self._semaphore = asyncio.Semaphore(_SERVICE_CONCURRENCY)
        self._bucket = _TokenBucket(clock, sleeper)

    async def get(
        self,
        url: str | httpx.URL,
        *,
        cancel: asyncio.Event | None = None,
        allowed_hosts: Iterable[str] | None = None,
        allowed_origins: Iterable[str] | None = None,
    ) -> OutboundHTTPResponse:
        """GET a public HTTP(S) URL, following at most five validated redirects.

        ``allowed_hosts`` restricts hostnames across the initial request and
        every redirect. ``allowed_origins`` restricts scheme, IDNA-canonical
        hostname, and effective port (including default-port normalization)
        across the initial request and every redirect. When both are supplied,
        both allowlists must match.
        """
        started = self._clock()
        deadline = started + self._deadline_seconds
        try:
            current = canonicalize_url(url)
            requested = str(current.url)
            host_allowlist = self._canonical_allowlist(allowed_hosts)
            origin_allowlist = self._canonical_origin_allowlist(allowed_origins)
            self._check_destination(current, host_allowlist, origin_allowlist)
        except (URLValidationError, AddressPolicyError) as exc:
            raise OutboundPolicyError(
                "outbound URL violates destination policy"
            ) from exc

        seen = {str(current.url)}
        redirect_count = 0
        try:
            async with asyncio.timeout(self._deadline_seconds):
                await self._acquire_slot(cancel, deadline)
                try:
                    while True:
                        self._check_cancel(cancel)
                        await self._wait_or_cancel(
                            self._bucket.acquire(deadline),
                            cancel,
                            max(0.0, deadline - self._clock()),
                        )
                        self._check_cancel(cancel)
                        self._check_destination(
                            current, host_allowlist, origin_allowlist
                        )
                        remaining = deadline - self._clock()
                        if remaining <= 0:
                            raise OutboundDeadlineError()
                        response, client = await self._request_hop(
                            current, remaining, cancel
                        )
                        status = response.status_code
                        if status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            await response.aclose()
                            await client.aclose()
                            if not location:
                                raise OutboundRedirectError(
                                    "redirect response has no location"
                                )
                            if redirect_count >= _SERVICE_MAX_REDIRECTS:
                                raise OutboundRedirectError("redirect limit exceeded")
                            try:
                                redirected = canonicalize_url(
                                    current.url.join(location)
                                )
                                self._check_destination(
                                    redirected, host_allowlist, origin_allowlist
                                )
                            except (
                                URLValidationError,
                                AddressPolicyError,
                                ValueError,
                            ) as exc:
                                raise OutboundPolicyError(
                                    "redirect destination violates outbound policy"
                                ) from exc
                            target = str(redirected.url)
                            if target in seen:
                                raise OutboundRedirectError("redirect loop detected")
                            seen.add(target)
                            current = redirected
                            redirect_count += 1
                            continue

                        if status == 429:
                            await response.aclose()
                            await client.aclose()
                            raise OutboundRateLimitError(status)
                        if status >= 400:
                            await response.aclose()
                            await client.aclose()
                            raise OutboundHTTPStatusError(status)
                        if not 200 <= status < 300:
                            await response.aclose()
                            await client.aclose()
                            raise OutboundUnsupportedStatusError(status)

                        content_type = response.headers.get("content-type")
                        safe_headers = httpx.Headers(
                            {"content-type": content_type}
                            if content_type is not None
                            else {}
                        )
                        body = bytearray()
                        try:
                            body_bytes = await self._read_body(
                                response, cancel, min(remaining, _SERVICE_READ_TIMEOUT)
                            )
                            body.extend(body_bytes)
                            stream = response.extensions.get("outbound_stream")
                            truncated = bool(getattr(stream, "truncated", False))
                        finally:
                            await response.aclose()
                            await client.aclose()
                        return OutboundHTTPResponse(
                            status_code=status,
                            headers=safe_headers,
                            body=bytes(body),
                            requested_url=requested,
                            final_url=str(current.url),
                            truncated=truncated,
                        )
                finally:
                    self._semaphore.release()
        except TimeoutError as exc:
            if isinstance(exc, OutboundServiceError):
                raise
            raise OutboundDeadlineError() from exc
        except asyncio.CancelledError:
            raise
        except OutboundServiceError:
            raise
        except (URLValidationError, AddressPolicyError) as exc:
            raise OutboundPolicyError("outbound destination violates policy") from exc
        except httpx.TimeoutException as exc:
            raise OutboundDeadlineError() from exc
        except OutboundResponseTooLarge as exc:
            raise OutboundNetworkError("outbound HTTP request failed") from exc
        except OutboundError as exc:
            if isinstance(exc, AddressPolicyError):
                raise OutboundPolicyError(
                    "outbound destination violates policy"
                ) from exc
            raise OutboundNetworkError("outbound HTTP request failed") from exc
        except (
            httpx.HTTPError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise OutboundNetworkError("outbound HTTP request failed") from exc

    async def _request_hop(
        self,
        url: CanonicalURL,
        remaining: float,
        cancel: asyncio.Event | None,
    ) -> tuple[httpx.Response, httpx.AsyncClient]:
        transport = OutboundTransport(
            resolver=self._resolver,
            backend=self._backend,
            max_connections=1,
            max_keepalive_connections=0,
            max_response_bytes=_MAX_WIRE_BYTES,
            truncate_response=True,
        )
        timeout = min(remaining, self._deadline_seconds)
        client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            cookies=None,
            timeout=httpx.Timeout(
                timeout,
                connect=min(timeout, _SERVICE_CONNECT_TIMEOUT),
                read=min(timeout, _SERVICE_READ_TIMEOUT),
            ),
            headers={
                "User-Agent": "Nexus-Outbound/1.0",
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate",
            },
        )
        try:
            async with asyncio.timeout(timeout):
                response = await self._wait_or_cancel(
                    client.send(client.build_request("GET", url.url), stream=True),
                    cancel,
                    timeout,
                )
            return response, client
        except BaseException:
            await client.aclose()
            raise

    async def _read_body(
        self,
        response: httpx.Response,
        cancel: asyncio.Event | None,
        timeout: float,
    ) -> bytes:
        async def read() -> bytes:
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=_WIRE_CHUNK_BYTES):
                body.extend(chunk)
            return bytes(body)

        return await self._wait_or_cancel(read(), cancel, timeout)

    async def _acquire_slot(
        self, cancel: asyncio.Event | None, deadline: float
    ) -> None:
        acquire = asyncio.create_task(self._semaphore.acquire())
        cancellation = (
            asyncio.create_task(cancel.wait()) if cancel is not None else None
        )
        acquired = False
        try:
            waiters = {acquire}
            if cancellation is not None:
                waiters.add(cancellation)
            done, _pending = await asyncio.wait(
                waiters,
                timeout=max(0.0, deadline - self._clock()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation in done and cancel is not None and cancel.is_set():
                raise asyncio.CancelledError
            if acquire not in done:
                raise OutboundDeadlineError()
            acquire.result()
            acquired = True
        finally:
            if not acquired:
                if acquire.done() and not acquire.cancelled() and acquire.result():
                    self._semaphore.release()
                else:
                    acquire.cancel()
                    await asyncio.gather(acquire, return_exceptions=True)
            if cancellation is not None:
                cancellation.cancel()
                await asyncio.gather(cancellation, return_exceptions=True)

    @staticmethod
    async def _wait_or_cancel(
        awaitable: Awaitable[Any], cancel: asyncio.Event | None, timeout: float
    ):
        if cancel is None:
            async with asyncio.timeout(timeout):
                return await awaitable
        operation = asyncio.create_task(awaitable)
        cancellation = asyncio.create_task(cancel.wait())
        try:
            done, _pending = await asyncio.wait(
                {operation, cancellation},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation in done and cancel.is_set():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                raise asyncio.CancelledError
            if operation not in done:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                raise TimeoutError
            return operation.result()
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)

    @staticmethod
    def _canonical_allowlist(
        allowed_hosts: Iterable[str] | None,
    ) -> frozenset[str] | None:
        if allowed_hosts is None:
            return None
        try:
            hosts = frozenset(_canonical_host(host.lower()) for host in allowed_hosts)
        except (AttributeError, TypeError, URLValidationError) as exc:
            raise OutboundPolicyError("allowed host list is invalid") from exc
        if not hosts:
            raise OutboundPolicyError("allowed host list must not be empty")
        return hosts

    @staticmethod
    def _canonical_origin_allowlist(
        allowed_origins: Iterable[str] | None,
    ) -> frozenset[tuple[str, str, int]] | None:
        if allowed_origins is None:
            return None
        try:
            origins: set[tuple[str, str, int]] = set()
            for value in allowed_origins:
                if not isinstance(value, str):
                    raise URLValidationError("origin must be a string")
                origin = canonicalize_url(value)
                parts = urlsplit(value)
                if (
                    origin.scheme != "https"
                    or parts.path not in {"", "/"}
                    or "?" in value
                    or "#" in value
                ):
                    raise URLValidationError("origin must be an HTTPS origin")
                origins.add((origin.scheme, origin.host, origin.port))
        except (TypeError, URLValidationError, AddressPolicyError) as exc:
            raise OutboundPolicyError("allowed origin list is invalid") from exc
        if not origins:
            raise OutboundPolicyError("allowed origin list must not be empty")
        return frozenset(origins)

    @staticmethod
    def _check_destination(
        url: CanonicalURL,
        allowed_hosts: frozenset[str] | None,
        allowed_origins: frozenset[tuple[str, str, int]] | None,
    ) -> None:
        if allowed_hosts is not None and url.host not in allowed_hosts:
            raise OutboundPolicyError("destination host is not allowed")
        if (
            allowed_origins is not None
            and (url.scheme, url.host, url.port) not in allowed_origins
        ):
            raise OutboundPolicyError("destination origin is not allowed")

    @staticmethod
    def _check_cancel(cancel: asyncio.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise asyncio.CancelledError

    async def aclose(self) -> None:
        """Compatibility no-op; each request owns and closes its transport."""
        return

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


class _OutboundHTTPServiceView:
    """Minimal HTTP service view using the pinned outbound transport."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolver = resolver
        self._backend = backend

    async def request(
        self,
        method: str,
        url: str | httpx.URL,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
        timeout: float | None = None,
    ) -> OutboundHTTPResponse:
        canonical = canonicalize_url(url)
        if timeout is None:
            request_timeout = _SERVICE_TIMEOUT
        elif (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("outbound service timeout must be positive and finite")
        else:
            request_timeout = min(float(timeout), _SERVICE_TIMEOUT)
        request_headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() != "accept-encoding"
        }
        request_headers["Accept-Encoding"] = "gzip, identity"
        transport = OutboundTransport(
            resolver=self._resolver,
            backend=self._backend,
            max_response_bytes=_MAX_WIRE_BYTES,
        )
        async with asyncio.timeout(request_timeout):
            async with httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(request_timeout),
            ) as client:
                async with client.stream(
                    method,
                    canonical.url,
                    headers=request_headers,
                    content=content,
                    timeout=httpx.Timeout(request_timeout),
                ) as response:
                    outbound_stream = response.extensions.get("outbound_stream")
                    original_headers = httpx.Headers(
                        getattr(outbound_stream, "original_headers", None)
                        or response.headers
                    )
                    content_length = original_headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared_length = int(content_length)
                        except ValueError:
                            declared_length = -1
                        if declared_length > _MAX_WIRE_BYTES:
                            raise OutboundResponseTooLarge(
                                "outbound response exceeded wire byte limit"
                            )
                    encoding = original_headers.get(
                        "content-encoding", "identity"
                    ).lower()
                    if encoding not in {"identity", "gzip"}:
                        raise ValueError(
                            "unsupported outbound response content encoding"
                        )

                    body = bytearray()
                    async for chunk in response.aiter_bytes(
                        chunk_size=_WIRE_CHUNK_BYTES
                    ):
                        if len(body) + len(chunk) > _MAX_DECODED_BYTES:
                            raise OutboundResponseTooLarge(
                                "outbound response exceeded decoded byte limit"
                            )
                        body.extend(chunk)
                    return OutboundHTTPResponse(
                        status_code=response.status_code,
                        headers=original_headers,
                        body=bytes(body),
                    )

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
