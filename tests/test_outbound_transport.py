from __future__ import annotations

import gzip
import ipaddress
from collections.abc import Sequence

import httpcore
import httpx
import pytest

from nexus.net.outbound import (
    AddressPolicyError,
    OutboundResponseTooLarge,
    OutboundTransport,
    PinnedAsyncNetworkBackend,
    URLValidationError,
    _OutboundHTTPServiceView,
    canonicalize_url,
)


class FakeStream(httpcore.AsyncNetworkStream):
    def __init__(self, peer: str, port: int, response: bytes | None = None) -> None:
        self.peer = (peer, port)
        self.tls_names: list[str | None] = []
        self.writes: list[bytes] = []
        self.closed = False
        self._response = (
            response
            if response is not None
            else b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        )

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        response, self._response = self._response[:max_bytes], self._response[max_bytes:]
        return response

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.append(buffer)

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(
        self,
        ssl_context,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.tls_names.append(server_hostname)
        return self

    def get_extra_info(self, info: str):
        if info == "server_addr":
            return self.peer
        return None


class FakeBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, peer: str | None = None, response: bytes | None = None) -> None:
        self.peer = peer
        self.response = response
        self.targets: list[tuple[str, int]] = []
        self.timeouts: list[float | None] = []
        self.streams: list[FakeStream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Sequence[object] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.targets.append((host, port))
        self.timeouts.append(timeout)
        stream = FakeStream(self.peer or host, port, self.response)
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(self, *args, **kwargs):
        raise AssertionError("Unix sockets must not be used")


def test_canonical_url_rejects_unsafe_or_ambiguous_authorities() -> None:
    for value in (
        "file:///etc/passwd",
        "https://user:password@example.com/",
        "https://localhost/",
        "https://127.0.0.1/",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::ffff:8.8.8.8]/",
        "https://metadata.google.internal/",
        "https://example.com\\@127.0.0.1/",
    ):
        with pytest.raises((ValueError, AddressPolicyError)):
            canonicalize_url(value)


def test_public_url_is_canonicalized() -> None:
    canonical = canonicalize_url("HTTPS://ExAmPlE.com:443/path?q=1")
    assert str(canonical.url) == "https://example.com/path?q=1"
    assert canonical.host == "example.com"
    assert canonical.port == 443


def test_canonical_url_rejects_port_zero_and_oversized_hostname() -> None:
    with pytest.raises(URLValidationError, match="port 0"):
        canonicalize_url("https://example.com:0/")
    with pytest.raises(URLValidationError):
        canonicalize_url(f"https://{'a' * 254}/")


@pytest.mark.asyncio
async def test_dns_is_vetted_once_and_numeric_address_is_connected() -> None:
    answers = iter((("8.8.8.8",), ("127.0.0.1",)))
    lookups: list[str] = []

    async def resolver(host: str):
        lookups.append(host)
        return next(answers)

    backend = FakeBackend()
    pinned = PinnedAsyncNetworkBackend(resolver=resolver, backend=backend)
    stream = await pinned.connect_tcp("service.example", 8443)

    assert backend.targets == [("8.8.8.8", 8443)]
    assert lookups == ["service.example"]
    assert stream.get_extra_info("server_addr") == ("8.8.8.8", 8443)


@pytest.mark.asyncio
async def test_mixed_public_private_dns_answer_fails_before_connect() -> None:
    backend = FakeBackend()

    async def resolver(host: str):
        return ("8.8.8.8", "10.0.0.8")

    pinned = PinnedAsyncNetworkBackend(resolver=resolver, backend=backend)
    with pytest.raises(AddressPolicyError):
        await pinned.connect_tcp("mixed.example", 443)
    assert backend.targets == []


@pytest.mark.asyncio
async def test_connected_peer_is_verified_against_pinned_address() -> None:
    backend = FakeBackend(peer="1.1.1.1")

    async def resolver(host: str):
        return ("8.8.8.8",)

    pinned = PinnedAsyncNetworkBackend(resolver=resolver, backend=backend)
    with pytest.raises(Exception, match="differs from pinned target"):
        await pinned.connect_tcp("peer.example", 443)
    assert backend.streams[0].closed


@pytest.mark.asyncio
async def test_transport_connects_numerically_and_preserves_tls_sni() -> None:
    calls: list[str] = []

    async def resolver(host: str):
        calls.append(host)
        return (ipaddress.ip_address("8.8.8.8"),)

    backend = FakeBackend()
    transport = OutboundTransport(resolver=resolver, backend=backend)
    request = httpx.Request("GET", "https://service.example/resource")
    response = await transport.handle_async_request(request)
    try:
        assert response.status_code == 200
        assert await response.aread() == b"ok"
    finally:
        await response.aclose()
        await transport.aclose()

    assert calls == ["service.example"]
    assert backend.targets == [("8.8.8.8", 443)]
    assert backend.streams[0].tls_names == ["service.example"]


@pytest.mark.asyncio
async def test_transport_ignores_untrusted_extensions_rewrites_host_and_defaults_timeout() -> None:
    async def resolver(host: str):
        return ("8.8.8.8",)

    backend = FakeBackend()
    transport = OutboundTransport(resolver=resolver, backend=backend)
    request = httpx.Request(
        "GET",
        "https://service.example:8443/resource",
        headers={"Host": "attacker.example"},
        extensions={
            "sni_hostname": "attacker.example",
            "target": (b"evil.example", 443),
            "timeout": None,
        },
    )
    response = await transport.handle_async_request(request)
    try:
        assert await response.aread() == b"ok"
    finally:
        await response.aclose()
        await transport.aclose()

    request_bytes = b"".join(backend.streams[0].writes)
    assert b"host: service.example:8443" in request_bytes.lower()
    assert b"attacker.example" not in request_bytes
    assert backend.targets == [("8.8.8.8", 8443)]
    assert backend.timeouts == [10.0]
    assert backend.streams[0].tls_names == ["service.example"]


@pytest.mark.asyncio
async def test_transport_rejects_caller_supplied_authorization() -> None:
    transport = OutboundTransport(backend=FakeBackend())
    with pytest.raises(ValueError, match="sensitive outbound request headers") as error:
        await transport.handle_async_request(
            httpx.Request(
                "GET", "https://service.example/", headers={"Authorization": "Bearer secret"}
            )
        )
    assert "secret" not in str(error.value)
    await transport.aclose()


@pytest.mark.asyncio
async def test_transport_formats_ipv6_host_with_explicit_port() -> None:
    backend = FakeBackend()
    transport = OutboundTransport(backend=backend)
    response = await transport.handle_async_request(
        httpx.Request("GET", "https://[2606:4700:4700::1111]:8443/resource")
    )
    try:
        assert await response.aread() == b"ok"
    finally:
        await response.aclose()
        await transport.aclose()

    request_bytes = b"".join(backend.streams[0].writes).lower()
    assert b"host: [2606:4700:4700::1111]:8443" in request_bytes
    assert backend.targets == [("2606:4700:4700::1111", 8443)]


def _raw_response(headers: bytes, body: bytes) -> bytes:
    return b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n\r\n" + body


@pytest.mark.asyncio
async def test_service_view_caps_decoded_gzip_bomb_and_closes_response() -> None:
    compressed = gzip.compress(b"x" * (4 * 1024 * 1024 + 1))
    backend = FakeBackend(
        response=_raw_response(
            f"Content-Encoding: gzip\r\nContent-Length: {len(compressed)}".encode(),
            compressed,
        )
    )
    view = _OutboundHTTPServiceView(
        resolver=lambda _host: _public_resolution(), backend=backend
    )
    with pytest.raises(OutboundResponseTooLarge, match="decoded byte limit"):
        await view.request("GET", "https://service.example/")
    assert backend.streams[0].closed
    await view.aclose()


async def _public_resolution() -> tuple[str, ...]:
    return ("8.8.8.8",)


@pytest.mark.asyncio
async def test_service_view_caps_chunked_wire_bytes() -> None:
    body = b"x" * (2 * 1024 * 1024 + 1)
    chunked = b"".join(
        f"{len(body[offset:offset + 4096]):X}\r\n".encode()
        + body[offset : offset + 4096]
        + b"\r\n"
        for offset in range(0, len(body), 4096)
    ) + b"0\r\n\r\n"
    backend = FakeBackend(
        response=_raw_response(b"Transfer-Encoding: chunked", chunked)
    )
    view = _OutboundHTTPServiceView(
        resolver=lambda _host: _public_resolution(), backend=backend
    )
    with pytest.raises(OutboundResponseTooLarge, match="wire byte limit"):
        await view.request("GET", "https://service.example/")
    assert backend.streams[0].closed
    await view.aclose()


@pytest.mark.asyncio
async def test_service_view_prechecks_oversized_content_length() -> None:
    backend = FakeBackend(
        response=_raw_response(b"Content-Length: 2097153", b"not read")
    )
    view = _OutboundHTTPServiceView(
        resolver=lambda _host: _public_resolution(), backend=backend
    )
    with pytest.raises(OutboundResponseTooLarge, match="wire byte limit"):
        await view.request("GET", "https://service.example/")
    assert backend.streams[0].closed
    await view.aclose()


@pytest.mark.asyncio
async def test_service_view_prechecks_compressed_wire_content_length() -> None:
    backend = FakeBackend(
        response=_raw_response(
            b"Content-Encoding: gzip\r\nContent-Length: 2097153",
            b"not read",
        )
    )
    view = _OutboundHTTPServiceView(
        resolver=lambda _host: _public_resolution(), backend=backend
    )
    with pytest.raises(OutboundResponseTooLarge, match="wire byte limit"):
        await view.request("GET", "https://service.example/")
    assert backend.streams[0].closed
    await view.aclose()


@pytest.mark.asyncio
async def test_service_view_does_not_persist_response_cookies() -> None:
    backend = FakeBackend(
        response=_raw_response(b"Set-Cookie: session=secret\r\nContent-Length: 2", b"ok")
    )
    view = _OutboundHTTPServiceView(
        resolver=lambda _host: _public_resolution(), backend=backend
    )
    await view.request("GET", "https://service.example/first")
    await view.request("GET", "https://service.example/second")
    assert len(backend.streams) == 2
    requests = [b"".join(stream.writes).lower() for stream in backend.streams]
    assert all(b"cookie:" not in request for request in requests)
    await view.aclose()


@pytest.mark.asyncio
async def test_private_redirect_destination_is_rejected_at_url_boundary() -> None:
    async def resolver(host: str):
        return ("8.8.8.8",)

    backend = FakeBackend()
    transport = OutboundTransport(resolver=resolver, backend=backend)
    response = await transport.handle_async_request(
        httpx.Request("GET", "https://service.example/start")
    )
    try:
        assert response.status_code == 200
        # This packet intentionally does not implement redirect handling. The
        # next-hop URL enters through the same strict URL boundary.
        with pytest.raises(AddressPolicyError):
            canonicalize_url("http://192.168.1.20/admin")
    finally:
        await response.aclose()
        await transport.aclose()
