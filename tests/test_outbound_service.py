from __future__ import annotations

import asyncio
import gzip
from collections.abc import Sequence

import httpcore
import httpx
import pytest

import nexus.net as outbound_api
from nexus.net.outbound import (
    OutboundDeadlineError,
    OutboundHTTPStatusError,
    OutboundNetworkError,
    OutboundPolicyError,
    OutboundRateLimitError,
    OutboundRedirectError,
    OutboundResponseTooLarge,
    OutboundUnsupportedStatusError,
    SafeOutboundHTTPService,
)


def _wire_response(status: int, headers: bytes = b"", body: bytes = b"") -> bytes:
    reason = {
        200: b"OK",
        302: b"Found",
        429: b"Too Many Requests",
        503: b"Unavailable",
    }.get(status, b"Response")
    return (
        b"HTTP/1.1 "
        + str(status).encode()
        + b" "
        + reason
        + b"\r\n"
        + headers.rstrip(b"\r\n")
        + (b"\r\n" if headers else b"")
        + b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )


class FakeStream(httpcore.AsyncNetworkStream):
    def __init__(self, peer: str, port: int, payload: bytes) -> None:
        self.peer = (peer, port)
        self.payload = payload
        self.closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        result, self.payload = self.payload[:max_bytes], self.payload[max_bytes:]
        return result

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(
        self,
        ssl_context,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return self

    def get_extra_info(self, info: str):
        return self.peer if info == "server_addr" else None


class FakeBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, responses: Sequence[bytes]) -> None:
        self.responses = list(responses)
        self.targets: list[tuple[str, int]] = []
        self.streams: list[FakeStream] = []
        self.requests: list[bytes] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Sequence[object] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.targets.append((host, port))
        payload = self.responses.pop(0)
        stream = FakeStream(host, port, payload)
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(self, *args, **kwargs):
        raise AssertionError("Unix socket connection was attempted")


async def _public(host: str) -> tuple[str, ...]:
    return ("8.8.8.8" if host.endswith("example") else "1.1.1.1",)


def _service(backend: FakeBackend, resolver=_public) -> SafeOutboundHTTPService:
    return SafeOutboundHTTPService(resolver=resolver, backend=backend)


def test_package_api_prominently_exports_hardened_service() -> None:
    assert outbound_api.SafeOutboundHTTPService is SafeOutboundHTTPService
    assert outbound_api.OutboundHTTPService is not None
    assert not hasattr(outbound_api, "OutboundHTTPServiceView")


@pytest.mark.asyncio
async def test_get_returns_requested_final_url_content_type_and_fixed_headers() -> None:
    backend = FakeBackend(
        [
            _wire_response(
                200,
                b"Content-Type: text/plain; charset=utf-8\r\n",
                b"hello",
            )
        ]
    )
    response = await _service(backend).get("https://www.example/start")

    assert response.requested_url == "https://www.example/start"
    assert response.final_url == response.requested_url
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.body == b"hello"
    assert not response.truncated
    assert backend.targets == [("8.8.8.8", 443)]


@pytest.mark.asyncio
async def test_redirect_checks_allowed_host_and_blocks_private_before_connection() -> (
    None
):
    backend = FakeBackend(
        [_wire_response(302, b"Location: http://192.168.1.10/admin\r\n")]
    )
    service = _service(backend)
    with pytest.raises(OutboundPolicyError):
        await service.get("https://search.example/", allowed_hosts={"search.example"})
    assert backend.targets == [("8.8.8.8", 443)]


@pytest.mark.asyncio
async def test_redirect_rechecks_allowlist_and_dns_on_every_hop() -> None:
    backend = FakeBackend(
        [
            _wire_response(302, b"Location: https://search2.example/next\r\n"),
            _wire_response(200, b"Content-Type: application/json\r\n", b"{}"),
        ]
    )
    resolutions: list[str] = []

    async def resolver(host: str):
        resolutions.append(host)
        return ("8.8.8.8",) if len(resolutions) == 1 else ("127.0.0.1",)

    with pytest.raises(OutboundPolicyError):
        await _service(backend, resolver).get(
            "https://search.example/",
            allowed_hosts={"search.example", "search2.example"},
        )
    assert resolutions == ["search.example", "search2.example"]
    assert backend.targets == [("8.8.8.8", 443)]


@pytest.mark.asyncio
async def test_redirect_loop_and_missing_location_are_errors() -> None:
    service = _service(FakeBackend([_wire_response(302, b"Location: /same\r\n")]))
    with pytest.raises(OutboundRedirectError, match="loop"):
        await service.get("https://search.example/same")

    service = _service(FakeBackend([_wire_response(302)]))
    with pytest.raises(OutboundRedirectError, match="no location"):
        await service.get("https://search.example/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [(429, OutboundRateLimitError), (503, OutboundHTTPStatusError), (204, None)],
)
async def test_http_statuses_are_not_silently_success(status, error) -> None:
    service = _service(FakeBackend([_wire_response(status)]))
    if error is None:
        # 204 is a successful status and therefore is supported.
        response = await service.get("https://search.example/")
        assert response.status_code == 204
    else:
        with pytest.raises(error) as raised:
            await service.get("https://search.example/")
        if status == 429:
            assert raised.value.status_code == 429


@pytest.mark.asyncio
async def test_other_non_success_status_has_distinct_unsupported_category() -> None:
    service = _service(FakeBackend([_wire_response(304)]))
    with pytest.raises(OutboundUnsupportedStatusError):
        await service.get("https://search.example/")


@pytest.mark.asyncio
async def test_compressed_decoded_body_is_bounded_and_marks_truncation() -> None:
    payload = gzip.compress(b"z" * (4 * 1024 * 1024 + 1))
    service = _service(
        FakeBackend(
            [
                _wire_response(
                    200,
                    b"Content-Encoding: gzip\r\nContent-Type: text/plain\r\n",
                    payload,
                )
            ]
        )
    )
    response = await service.get("https://search.example/")
    assert len(response.body) == 4 * 1024 * 1024
    assert response.body == b"z" * (4 * 1024 * 1024)
    assert response.truncated
    assert "content-encoding" not in response.headers


@pytest.mark.asyncio
async def test_wire_cap_truncates_body_and_legacy_transport_still_raises() -> None:
    body = b"a" * (2 * 1024 * 1024 + 1)
    service = _service(FakeBackend([_wire_response(200, b"", body)]))
    response = await service.get("https://search.example/")
    assert len(response.body) == 2 * 1024 * 1024
    assert response.truncated


@pytest.mark.asyncio
async def test_cancel_interrupts_response_read_and_closes_stream() -> None:
    class BlockingBackend(FakeBackend):
        async def connect_tcp(self, *args, **kwargs):
            stream = await super().connect_tcp(*args, **kwargs)
            original_read = stream.read

            async def blocking_read(
                max_bytes: int, timeout: float | None = None
            ) -> bytes:
                if stream.payload:
                    await asyncio.Event().wait()
                return await original_read(max_bytes, timeout)

            stream.read = blocking_read
            return stream

    backend = BlockingBackend([_wire_response(200, body=b"wait")])
    cancel = asyncio.Event()
    task = asyncio.create_task(
        _service(backend).get("https://search.example/", cancel=cancel)
    )
    for _ in range(100):
        if backend.streams:
            break
        await asyncio.sleep(0)
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.streams[0].closed


@pytest.mark.asyncio
async def test_cancel_interrupts_dns_resolution() -> None:
    entered = asyncio.Event()

    async def stalled_resolver(_host: str):
        entered.set()
        await asyncio.Event().wait()

    cancel = asyncio.Event()
    task = asyncio.create_task(
        SafeOutboundHTTPService(resolver=stalled_resolver, backend=FakeBackend([])).get(
            "https://search.example/", cancel=cancel
        )
    )
    await entered.wait()
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancel_interrupts_numeric_connect() -> None:
    entered = asyncio.Event()

    class BlockingConnectBackend(FakeBackend):
        async def connect_tcp(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    cancel = asyncio.Event()
    task = asyncio.create_task(
        SafeOutboundHTTPService(
            resolver=_public,
            backend=BlockingConnectBackend([]),
        ).get("https://search.example/", cancel=cancel)
    )
    await entered.wait()
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_read_timeout_closes_stream_and_raises_deadline_error() -> None:
    class BlockingReadBackend(FakeBackend):
        async def connect_tcp(self, *args, **kwargs):
            stream = await super().connect_tcp(*args, **kwargs)

            async def blocking_read(
                _max_bytes: int, timeout: float | None = None
            ) -> bytes:
                await asyncio.Event().wait()

            stream.read = blocking_read
            return stream

    backend = BlockingReadBackend([_wire_response(200, body=b"wait")])
    service = SafeOutboundHTTPService(resolver=_public, backend=backend)
    service._deadline_seconds = 0.01
    with pytest.raises(OutboundDeadlineError):
        await service.get("https://search.example/")
    assert backend.streams[0].closed


@pytest.mark.asyncio
async def test_deflate_is_decoded_with_decoded_size_limit() -> None:
    import zlib

    payload = zlib.compress(b"deflated")
    response = await _service(
        FakeBackend(
            [
                _wire_response(
                    200,
                    b"Content-Encoding: deflate\r\nContent-Type: text/plain\r\n",
                    payload,
                )
            ]
        )
    ).get("https://search.example/")
    assert response.body == b"deflated"
    assert not response.truncated


@pytest.mark.asyncio
async def test_deadline_covers_waiting_dns() -> None:
    async def stalled_resolver(_host: str):
        await asyncio.Event().wait()

    service = SafeOutboundHTTPService(
        resolver=stalled_resolver, backend=FakeBackend([])
    )
    service._deadline_seconds = 0.01
    with pytest.raises(OutboundDeadlineError):
        await service.get("https://search.example/")


@pytest.mark.asyncio
async def test_rate_limiter_allows_burst_then_refills_at_thirty_per_minute() -> None:
    now = 0.0
    sleeps: list[float] = []

    def clock() -> float:
        return now

    async def sleeper(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    backend = FakeBackend([_wire_response(200)] * 8)
    service = SafeOutboundHTTPService(
        resolver=_public,
        backend=backend,
        clock=clock,
        sleeper=sleeper,
    )
    for _ in range(30):
        await service._bucket.acquire(now + 20)
    await service._bucket.acquire(now + 20)
    assert sleeps == [2.0]
    assert now == 2.0


@pytest.mark.asyncio
async def test_shared_concurrency_is_bounded_at_eight() -> None:
    now = 0.0

    def clock() -> float:
        return now

    async def sleeper(delay: float) -> None:
        nonlocal now
        now += delay

    service = SafeOutboundHTTPService(clock=clock, sleeper=sleeper)
    arrivals = 0
    active = 0
    maximum = 0
    all_arrived = asyncio.Event()

    async def fake_hop(_url, _remaining, _cancel):
        nonlocal arrivals, active, maximum
        arrivals += 1
        active += 1
        maximum = max(maximum, active)
        if arrivals == 8:
            all_arrived.set()
        await all_arrived.wait()
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(200, content=b"ok"), httpx.AsyncClient()

    service._request_hop = fake_hop
    responses = await asyncio.gather(
        *(service.get(f"https://search.example/{index}") for index in range(10))
    )
    assert len(responses) == 10
    assert maximum == 8


@pytest.mark.asyncio
async def test_fixed_headers_do_not_forward_cookies_or_authentication() -> None:
    class RecordingStream(FakeStream):
        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            self.backend.requests.append(buffer)

    class RecordingBackend(FakeBackend):
        async def connect_tcp(self, host, port, **kwargs):
            self.targets.append((host, port))
            stream = RecordingStream(host, port, self.responses.pop(0))
            stream.backend = self
            self.streams.append(stream)
            return stream

    backend = RecordingBackend([_wire_response(200, body=b"ok")])
    await _service(backend).get("https://search.example/")
    request_bytes = b"".join(backend.requests).lower()
    assert b"user-agent: nexus-outbound/1.0" in request_bytes
    assert b"accept: */*" in request_bytes
    assert b"accept-encoding: gzip, deflate" in request_bytes
    assert b"cookie:" not in request_bytes
    assert b"authorization:" not in request_bytes
    assert b"proxy-authorization:" not in request_bytes


@pytest.mark.asyncio
async def test_allowlist_rejects_nonlisted_requested_host() -> None:
    backend = FakeBackend([])
    with pytest.raises(OutboundPolicyError):
        await _service(backend).get(
            "https://other.example/", allowed_hosts={"search.example"}
        )
    assert backend.targets == []


@pytest.mark.asyncio
async def test_allowed_hosts_are_host_only_and_allow_other_ports() -> None:
    backend = FakeBackend([_wire_response(200, body=b"ok")])
    response = await _service(backend).get(
        "https://search.example:8443/", allowed_hosts={"search.example"}
    )
    assert response.body == b"ok"
    assert backend.targets == [("8.8.8.8", 8443)]


@pytest.mark.asyncio
async def test_origin_allowlist_rejects_redirect_to_same_host_other_port() -> None:
    backend = FakeBackend(
        [_wire_response(302, b"Location: https://search.example:8080/next\r\n")]
    )

    with pytest.raises(OutboundPolicyError):
        await _service(backend).get(
            "https://search.example/",
            allowed_origins={"https://search.example"},
        )

    assert backend.targets == [("8.8.8.8", 443)]


@pytest.mark.asyncio
async def test_origin_allowlist_rejects_https_downgrade_before_connection() -> None:
    backend = FakeBackend([_wire_response(302, b"Location: http://search.example/next\r\n")])

    with pytest.raises(OutboundPolicyError):
        await _service(backend).get(
            "https://search.example/",
            allowed_origins={"https://search.example"},
        )

    assert backend.targets == [("8.8.8.8", 443)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "origins", "expected_hosts"),
    [
        (
            "https://search.example:443/next",
            {"https://search.example"},
            ["search.example", "search.example"],
        ),
        (
            "https://search2.example/next",
            {"https://search.example", "https://search2.example:443"},
            ["search.example", "search2.example"],
        ),
    ],
)
async def test_origin_allowlist_permits_configured_https_redirects_and_redns(
    location, origins, expected_hosts
) -> None:
    backend = FakeBackend(
        [
            _wire_response(302, f"Location: {location}\r\n".encode()),
            _wire_response(200, body=b"ok"),
        ]
    )
    resolutions: list[str] = []

    async def resolver(host: str):
        resolutions.append(host)
        return ("8.8.8.8",) if len(resolutions) == 1 else ("1.1.1.1",)

    response = await _service(backend, resolver).get(
        "https://search.example/", allowed_origins=origins
    )

    assert response.body == b"ok"
    assert resolutions == expected_hosts
    assert [host for host, _port in backend.targets] == ["8.8.8.8", "1.1.1.1"]
    assert backend.targets == [("8.8.8.8", 443), ("1.1.1.1", 443)]


@pytest.mark.asyncio
async def test_origin_allowlist_rejects_wrong_direct_origin_before_connection() -> None:
    backend = FakeBackend([])

    with pytest.raises(OutboundPolicyError):
        await _service(backend).get(
            "https://search.example/", allowed_origins={"https://other.example"}
        )

    assert backend.targets == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origins",
    [
        set(),
        {"http://search.example"},
        {"https://search.example/search"},
        {"https://user@search.example"},
        {"https://search.example:0"},
        {None},
    ],
)
async def test_invalid_origin_allowlist_fails_before_connection(origins) -> None:
    backend = FakeBackend([])

    with pytest.raises(OutboundPolicyError):
        await _service(backend).get(
            "https://search.example/", allowed_origins=origins
        )

    assert backend.targets == []


@pytest.mark.asyncio
async def test_host_and_origin_allowlists_intersect() -> None:
    backend = FakeBackend([])

    with pytest.raises(OutboundPolicyError):
        await _service(backend).get(
            "https://search.example/",
            allowed_hosts={"search.example"},
            allowed_origins={"https://other.example"},
        )

    assert backend.targets == []


@pytest.mark.asyncio
async def test_oversized_response_has_network_classification() -> None:
    service = _service(FakeBackend([]))

    async def oversized(*_args, **_kwargs):
        raise OutboundResponseTooLarge("detail must not escape")

    service._request_hop = oversized
    with pytest.raises(OutboundNetworkError, match="outbound HTTP request failed") as error:
        await service.get("https://search.example/")
    assert "detail must not escape" not in str(error.value)


@pytest.mark.asyncio
async def test_malformed_timeout_type_error_is_sanitized() -> None:
    service = _service(FakeBackend([]))

    async def malformed_timeout(*_args, **_kwargs):
        raise TypeError("timeout secret")

    service._request_hop = malformed_timeout
    with pytest.raises(OutboundNetworkError, match="outbound HTTP request failed") as error:
        await service.get("https://search.example/")
    assert "timeout secret" not in str(error.value)


@pytest.mark.asyncio
async def test_rate_wait_beyond_remaining_deadline_fails_without_sleeping() -> None:
    now = 0.0

    def clock() -> float:
        return now

    async def sleeper(delay: float) -> None:
        raise AssertionError(f"unexpected sleep {delay}")

    service = SafeOutboundHTTPService(clock=clock, sleeper=sleeper)
    service._bucket._tokens = 0
    service._bucket._updated = now
    with pytest.raises(OutboundDeadlineError):
        await service._bucket.acquire(now + 1)
