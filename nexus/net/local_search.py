"""Fixed-destination HTTP client for the local search service."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

import httpx

from .outbound import (
    OutboundDeadlineError,
    OutboundHTTPResponse,
    OutboundHTTPStatusError,
    OutboundNetworkError,
    OutboundPolicyError,
    OutboundRateLimitError,
)

LOCAL_SEARCH_ORIGIN = "http://127.0.0.1:18765"
_MAX_RESPONSE_BYTES = 512_000
_DEADLINE_SECONDS = 10.0
_QUERY_LIMITS = {"q": 4096, "pageno": 1, "format": 4, "categories": 7, "language": 2}


class _CancelToken(Protocol):
    def is_set(self) -> bool: ...

    async def wait(self) -> object: ...


class LocalSearchHTTPService:
    """Perform bounded GET requests to the one configured local-search route."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=_DEADLINE_SECONDS,
        )

    async def aclose(self) -> None:
        """Close the service-owned client, if any."""
        if self._owns_client:
            await self._client.aclose()

    async def get(
        self,
        url: str | httpx.URL,
        *,
        cancel: _CancelToken | None = None,
        allowed_origins: Iterable[str] | None = None,
    ) -> OutboundHTTPResponse:
        """Fetch the exact local-search endpoint with bounded, redacted errors."""
        requested = str(url)
        _validate_request_url(requested, allowed_origins)
        if cancel is not None and cancel.is_set():
            raise asyncio.CancelledError

        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(_DEADLINE_SECONDS):
                request = self._client.build_request(
                    "GET",
                    requested,
                    headers={"Accept-Encoding": "identity"},
                )
                response = await _await_or_cancel(
                    self._client.send(request, stream=True, follow_redirects=False),
                    cancel,
                )

                status = response.status_code
                if 300 <= status < 400:
                    raise OutboundPolicyError("local search redirects are not allowed")
                if status == 429:
                    raise OutboundRateLimitError(status)
                if 400 <= status <= 599:
                    raise OutboundHTTPStatusError(status)
                if not 200 <= status < 300:
                    raise OutboundHTTPStatusError(status)

                body = bytearray()
                async for chunk in _iter_bytes_or_cancel(response, cancel):
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise OutboundNetworkError("local search response exceeded limit")
                    body.extend(chunk)

                content_type = response.headers.get("content-type")
                headers = httpx.Headers(
                    {"content-type": content_type} if content_type is not None else {}
                )
                return OutboundHTTPResponse(
                    status_code=status,
                    headers=headers,
                    body=bytes(body),
                    requested_url=requested,
                    final_url=requested,
                )
        except asyncio.CancelledError:
            raise
        except OutboundPolicyError:
            raise
        except OutboundRateLimitError:
            raise
        except OutboundHTTPStatusError:
            raise
        except TimeoutError as exc:
            raise OutboundDeadlineError() from exc
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise OutboundNetworkError("local search request failed") from exc
        finally:
            if response is not None:
                await response.aclose()


def _validate_request_url(
    value: str, allowed_origins: Iterable[str] | None
) -> None:
    try:
        origins = list(allowed_origins) if allowed_origins is not None else None
    except TypeError as exc:
        raise OutboundPolicyError("local search destination violates policy") from exc
    if origins != [LOCAL_SEARCH_ORIGIN]:
        raise OutboundPolicyError("local search destination violates policy")

    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value) or "#" in value:
        raise OutboundPolicyError("local search destination violates policy")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.netloc != "127.0.0.1:18765"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != 18765
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/search"
            or parsed.fragment
        ):
            raise ValueError("destination mismatch")
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=len(_QUERY_LIMITS),
        )
        seen: set[str] = set()
        for key, item in pairs:
            limit = _QUERY_LIMITS.get(key)
            if limit is None or key in seen or len(item) > limit:
                raise ValueError("query violates limits")
            seen.add(key)
            if key == "pageno" and item not in {"1", "2"}:
                raise ValueError("invalid page number")
            if key == "format" and item not in {"json", "html"}:
                raise ValueError("invalid format")
            if key == "categories" and item != "general":
                raise ValueError("invalid category")
            if key == "language" and item != "en":
                raise ValueError("invalid language")
        if seen != set(_QUERY_LIMITS) or not dict(pairs)["q"].strip():
            raise ValueError("missing search parameters")
    except (ValueError, UnicodeError) as exc:
        raise OutboundPolicyError("local search destination violates policy") from exc


async def _await_or_cancel(awaitable, cancel: _CancelToken | None):
    if cancel is None:
        return await awaitable
    operation = asyncio.create_task(awaitable)
    cancellation = asyncio.create_task(cancel.wait())
    try:
        done, _pending = await asyncio.wait(
            {operation, cancellation}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancellation in done and cancel.is_set():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise asyncio.CancelledError
        return operation.result()
    finally:
        for task in (operation, cancellation):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, cancellation, return_exceptions=True)


async def _iter_bytes_or_cancel(response: httpx.Response, cancel: _CancelToken | None):
    iterator = response.aiter_bytes().__aiter__()
    while True:
        try:
            chunk = await _await_or_cancel(iterator.__anext__(), cancel)
        except StopAsyncIteration:
            return
        yield chunk


__all__ = ["LOCAL_SEARCH_ORIGIN", "LocalSearchHTTPService"]
