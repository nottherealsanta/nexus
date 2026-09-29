from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import httpx
import pytest

from nexus.net.local_search import LOCAL_SEARCH_ORIGIN, LocalSearchHTTPService
from nexus.net.outbound import (
    OutboundDeadlineError,
    OutboundHTTPStatusError,
    OutboundNetworkError,
    OutboundPolicyError,
    OutboundRateLimitError,
)


def _valid_url(
    *,
    origin: str = LOCAL_SEARCH_ORIGIN,
    path: str = "/search",
    omit: str | None = None,
    **overrides: str,
) -> str:
    """Build a URL containing every valid local-search query parameter."""
    query = {
        "q": "hello",
        "pageno": "1",
        "format": "json",
        "categories": "general",
        "language": "en",
    }
    query.update(overrides)
    if omit is not None:
        query.pop(omit)
    return f"{origin}{path}?{urlencode(query)}"


def _service(handler) -> tuple[LocalSearchHTTPService, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LocalSearchHTTPService(client), client


@pytest.mark.asyncio
async def test_successful_json_response_is_returned_with_bounded_metadata() -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(200, json={"results": []}, headers={"x-private": "hidden"})

    service, client = _service(handler)
    try:
        result = await service.get(
            _valid_url(),
            allowed_origins=[LOCAL_SEARCH_ORIGIN],
        )
    finally:
        await client.aclose()

    assert result.status_code == 200
    assert result.body == b'{"results":[]}'
    assert result.headers.get("x-private") is None
    assert result.requested_url == result.final_url
    assert requested[0].url.host == "127.0.0.1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        _valid_url(origin="http://localhost:18765"),
        _valid_url(origin="http://127.1:18765"),
        _valid_url(origin="http://2130706433:18765"),
        _valid_url(origin="http://127.0.0.1:18764"),
        _valid_url(origin="http://127.0.0.1"),
        _valid_url(origin="http://user@127.0.0.1:18765"),
        _valid_url(path="/other"),
        _valid_url() + "#fragment",
        _valid_url() + "&admin=true",
        _valid_url(format="xml"),
        _valid_url(categories="files"),
        _valid_url(language="fr"),
        _valid_url() + "&q=duplicate",
        _valid_url(q=""),
        _valid_url(pageno="3"),
        _valid_url(q="x" * 4097),
        _valid_url(omit="q"),
        _valid_url(omit="pageno"),
        _valid_url(omit="format"),
        _valid_url(omit="categories"),
        _valid_url(omit="language"),
    ],
)
async def test_rejects_single_invalid_destination_condition(url: str) -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    service, client = _service(handler)
    try:
        with pytest.raises(OutboundPolicyError):
            await service.get(url, allowed_origins=[LOCAL_SEARCH_ORIGIN])
    finally:
        await client.aclose()
    assert not called


@pytest.mark.asyncio
async def test_allowed_origins_must_be_the_exact_singleton() -> None:
    service, client = _service(lambda _request: httpx.Response(200))
    try:
        with pytest.raises(OutboundPolicyError):
            await service.get(
                _valid_url(), allowed_origins=["http://localhost:18765"]
            )
        with pytest.raises(OutboundPolicyError):
            await service.get(_valid_url())
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_redirect_is_blocked_without_following_target() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(302, headers={"location": "http://127.0.0.1:18765/other"})

    service, client = _service(handler)
    try:
        with pytest.raises(OutboundPolicyError):
            await service.get(
                _valid_url(), allowed_origins=[LOCAL_SEARCH_ORIGIN]
            )
    finally:
        await client.aclose()
    assert called


@pytest.mark.asyncio
async def test_streaming_response_limit_is_enforced() -> None:
    service, client = _service(lambda _request: httpx.Response(200, content=b"x" * 512_001))
    try:
        with pytest.raises(OutboundNetworkError, match="exceeded limit"):
            await service.get(
                _valid_url(), allowed_origins=[LOCAL_SEARCH_ORIGIN]
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"), [(429, OutboundRateLimitError), (503, OutboundHTTPStatusError)]
)
async def test_error_statuses_use_outbound_exception_types(status, error) -> None:
    service, client = _service(lambda _request: httpx.Response(status))
    try:
        with pytest.raises(error):
            await service.get(
                _valid_url(), allowed_origins=[LOCAL_SEARCH_ORIGIN]
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cancellation_interrupts_pending_request() -> None:
    entered = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200)

    service, client = _service(handler)
    cancel = asyncio.Event()
    task = asyncio.create_task(
        service.get(
            _valid_url(), cancel=cancel,
            allowed_origins=[LOCAL_SEARCH_ORIGIN],
        )
    )
    try:
        await entered.wait()
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await client.aclose()


@pytest.mark.asyncio
async def test_overall_deadline_is_redacted(monkeypatch) -> None:
    from nexus.net import local_search

    async def stuck(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200)

    monkeypatch.setattr(local_search, "_DEADLINE_SECONDS", 0.01)
    service, client = _service(stuck)
    try:
        with pytest.raises(OutboundDeadlineError) as raised:
            await service.get(
                _valid_url(), allowed_origins=[LOCAL_SEARCH_ORIGIN]
            )
        assert str(raised.value) == "timeout"
    finally:
        await client.aclose()
