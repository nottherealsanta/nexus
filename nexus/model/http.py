"""Shared HTTP transport and SSE framing for provider adapters (plan section 8).

One pooled :class:`httpx.AsyncClient` per transport owns connection reuse,
bounded retry with jittered exponential backoff on transport errors and
429/5xx (honouring ``Retry-After``), and standards-shaped Server-Sent Events
parsing. Newline-delimited JSON (line framing, for providers that do not speak
SSE) is offered by the same wrapper. Adapters translate schemas in and streams
out; they never touch ``httpx`` directly.

Two deliberate policies live here rather than in adapters:

* **Retry boundary.** Retries happen only before the first SSE event is
  yielded. Once bytes have streamed to a consumer, a mid-stream failure is
  surfaced as a :class:`~nexus.errors.ProviderError` instead of being replayed,
  because replaying would duplicate already-delivered content.
* **``[DONE]`` sentinel.** ``data: [DONE]`` ends iteration by default; callers
  that want to observe the sentinel set ``stop_on_done=False``.

Nothing in this module logs headers, request bodies, or response bodies, so a
credential carried in a header cannot reach a log through the transport.
"""
from __future__ import annotations

import asyncio
import codecs
import json
import logging
import random
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
import msgspec

from ..errors import ProviderError
from ..util import redact_secrets, redact_url_userinfo

__all__ = [
    "DONE_SENTINEL",
    "HTTPTransport",
    "RetryPolicy",
    "SSEDecoder",
    "SSEEvent",
    "redact_secrets",
]

_logger = logging.getLogger(__name__)

DONE_SENTINEL = "[DONE]"

_DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class SSEEvent(msgspec.Struct, frozen=True):
    """One dispatched Server-Sent Event."""

    event: str | None = None
    data: str = ""
    id: str | None = None
    retry: int | None = None


def _take_field(line: str) -> tuple[str, str]:
    """Split an SSE line into ``(field, value)``; strips one leading space."""
    if ":" in line:
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        return field, value
    return line, ""


class SSEDecoder:
    """Incremental, standards-shaped SSE framer.

    Feed it arbitrary byte chunks (which may split lines, events, or multi-byte
    UTF-8 sequences) and it returns the events that became complete. Blank lines
    dispatch; ``:`` lines are comments; ``data`` fields concatenate with a
    newline; ``event``/``id``/``retry`` are tracked. As the SSE specification
    requires, an event that is not terminated by a blank line is discarded at
    EOF.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._data: list[str] = []
        self._event: str | None = None
        self._last_event_id: str | None = None
        self._retry_ms: int | None = None
        self._skip_lf = False
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    @property
    def pending(self) -> str:
        """Text buffered for the next line; empty when no partial line is held."""
        return self._buffer

    @property
    def last_event_id(self) -> str | None:
        return self._last_event_id

    @property
    def retry_ms(self) -> int | None:
        return self._retry_ms

    def feed_bytes(self, chunk: bytes) -> list[SSEEvent]:
        if not chunk:
            return []
        return self.feed(self._decoder.decode(chunk))

    def feed(self, text: str) -> list[SSEEvent]:
        if not text:
            return []
        self._buffer += text
        events: list[SSEEvent] = []
        for line in self._take_lines():
            self._process_line(line, events)
        return events

    def close(self) -> list[SSEEvent]:
        """Flush the incremental decoder and discard any unterminated event."""
        events: list[SSEEvent] = []
        tail = self._decoder.decode(b"", final=True)
        if tail:
            self._buffer += tail
        if self._buffer:
            line = self._buffer
            self._buffer = ""
            self._process_line(line, events)
        # Per spec: a pending data buffer without a terminating blank line is
        # never dispatched.
        self._data = []
        self._event = None
        self._skip_lf = False
        return events

    def _take_lines(self) -> list[str]:
        lines: list[str] = []
        buf = self._buffer
        i = 0
        start = 0
        while i < len(buf):
            ch = buf[i]
            if ch == "\n":
                if self._skip_lf:
                    self._skip_lf = False
                    i += 1
                    start = i
                    continue
                lines.append(buf[start:i])
                i += 1
                start = i
            elif ch == "\r":
                lines.append(buf[start:i])
                self._skip_lf = True
                i += 1
                start = i
            else:
                self._skip_lf = False
                i += 1
        self._buffer = buf[start:]
        return lines

    def _process_line(self, line: str, events: list[SSEEvent]) -> None:
        if line == "":
            events.extend(self._dispatch())
            return
        if line.startswith(":"):
            return  # comment
        field, value = _take_field(line)
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        elif field == "id":
            if "\x00" not in value:
                self._last_event_id = value
        elif field == "retry" and value.isdigit():
            self._retry_ms = int(value)

    def _dispatch(self) -> list[SSEEvent]:
        if not self._data:
            self._event = None
            return []
        event = SSEEvent(
            event=self._event,
            data="\n".join(self._data),
            id=self._last_event_id,
            retry=self._retry_ms,
        )
        self._data = []
        self._event = None
        return [event]


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry configuration. One attempt is the initial request."""

    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 30.0
    jitter_ratio: float = 0.25
    retry_statuses: frozenset[int] = field(
        default_factory=lambda: frozenset({408, 409, 425, 429, 500, 502, 503, 504})
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

    def should_retry_status(self, status: int) -> bool:
        return status in self.retry_statuses


def _safe_url(url: object) -> str:
    """URL string safe for a log or an error: query and userinfo stripped.

    A query string can carry a token and a configured URL can carry
    ``user:password@`` userinfo; both are removed before the URL reaches an
    error detail or a debug log.
    """
    try:
        parsed = httpx.URL(str(url))
    except Exception:  # noqa: BLE001 - never let logging raise
        return "<invalid-url>"
    if parsed.query:
        parsed = parsed.copy_with(query=None)
    return redact_url_userinfo(str(parsed))


#: Maximum length of a rendered error detail.
_DETAIL_LIMIT = 500


def _response_detail(raw: bytes | str) -> str:
    """Best-effort, bounded, sanitized error text. Never echoes credentials."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = raw
    text = text.strip()
    if not text:
        return "no response body"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("type")
            if message:
                return redact_secrets(str(message))[:_DETAIL_LIMIT]
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return redact_secrets(value)[:_DETAIL_LIMIT]
    return redact_secrets(text.replace("\n", " "))[:_DETAIL_LIMIT]


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class HTTPTransport:
    """A thin, retrying wrapper over one ``httpx.AsyncClient``.

    Ownership is explicit: a client the transport constructs is closed by
    :meth:`aclose`; an injected client is not, unless ``owns_client=True`` is
    passed. ``aclose`` is idempotent.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        owns_client: bool | None = None,
        base_url: str = "",
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], object] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        if client is not None and http_transport is not None:
            raise ValueError("pass either client or http_transport, not both")
        self._retry = retry or RetryPolicy()
        self._sleep = sleep or asyncio.sleep
        self._jitter = jitter or random.random
        self._default_headers = dict(headers or {})
        self._closed = False
        if client is None:
            self._client = httpx.AsyncClient(
                base_url=base_url,
                transport=http_transport,
                headers=self._default_headers or None,
                timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT,
            )
            self._owns_client = True if owns_client is None else owns_client
        else:
            self._client = client
            self._owns_client = False if owns_client is None else owns_client

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    @property
    def owns_client(self) -> bool:
        return self._owns_client

    @property
    def closed(self) -> bool:
        return self._closed

    def _merged_headers(
        self, headers: Mapping[str, str] | None
    ) -> dict[str, str] | None:
        if not self._default_headers and not headers:
            return None
        merged = dict(self._default_headers)
        if headers:
            merged.update(headers)
        return merged

    def _delay_for(
        self, attempt: int, response: httpx.Response | None = None
    ) -> float:
        if response is not None:
            retry_after = _retry_after_seconds(response)
            if retry_after is not None:
                return min(retry_after, self._retry.max_delay)
        exponent = max(0, attempt - 1)
        base = min(
            self._retry.base_delay * (2**exponent), self._retry.max_delay
        )
        delay = base * (1.0 + self._retry.jitter_ratio * self._jitter())
        return min(delay, self._retry.max_delay)

    def _http_error(self, status: int, url: object, detail: str) -> ProviderError:
        error = ProviderError(f"HTTP {status} from {_safe_url(url)}: {detail}")
        error.status_code = status  # type: ignore[attr-defined]
        return error

    def _transport_error(self, exc: BaseException, url: object = None) -> ProviderError:
        host = ""
        if url is not None:
            try:
                host = httpx.URL(str(url)).host or ""
            except Exception:  # noqa: BLE001
                host = ""
        where = f" talking to {host}" if host else ""
        return ProviderError(f"transport error{where}: {type(exc).__name__}")

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: object | None = None,
        params: Mapping[str, object] | None = None,
        content: bytes | str | None = None,
    ) -> httpx.Response:
        """Send a non-streaming request, retrying per policy on failure."""
        if self._closed:
            raise ProviderError("HTTP transport is closed")
        headers = self._merged_headers(headers)
        attempt = 0
        while True:
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers=headers,
                    json=json,
                    params=params,
                    content=content,
                )
            except httpx.TransportError as exc:
                if attempt + 1 < self._retry.max_attempts:
                    attempt += 1
                    await self._sleep(self._delay_for(attempt))
                    _logger.debug(
                        "http %s %s retry %d/%d after transport error",
                        method,
                        _safe_url(url),
                        attempt,
                        self._retry.max_attempts,
                    )
                    continue
                raise self._transport_error(exc, url) from exc
            if response.status_code >= 400:
                if (
                    self._retry.should_retry_status(response.status_code)
                    and attempt + 1 < self._retry.max_attempts
                ):
                    attempt += 1
                    delay = self._delay_for(attempt, response)
                    await self._sleep(delay)
                    _logger.debug(
                        "http %s %s retry %d/%d after HTTP %d",
                        method,
                        _safe_url(url),
                        attempt,
                        self._retry.max_attempts,
                        response.status_code,
                    )
                    continue
                raise self._http_error(
                    response.status_code, url, _response_detail(response.text)
                )
            _logger.debug(
                "http %s %s -> %d", method, _safe_url(url), response.status_code
            )
            return response

    async def aiter_sse(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: object | None = None,
        params: Mapping[str, object] | None = None,
        stop_on_done: bool = True,
    ) -> AsyncIterator[SSEEvent]:
        """Stream Server-Sent Events, retrying only before the first event."""
        if self._closed:
            raise ProviderError("HTTP transport is closed")
        headers = self._merged_headers(headers)
        attempt = 0
        while True:
            emitted = False
            decoder = SSEDecoder()
            try:
                async with self._client.stream(
                    method, url, headers=headers, json=json, params=params
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        if (
                            self._retry.should_retry_status(response.status_code)
                            and attempt + 1 < self._retry.max_attempts
                        ):
                            attempt += 1
                            delay = self._delay_for(attempt, response)
                            await self._sleep(delay)
                            _logger.debug(
                                "http %s %s retry %d/%d after HTTP %d",
                                method,
                                _safe_url(url),
                                attempt,
                                self._retry.max_attempts,
                                response.status_code,
                            )
                            continue
                        raise self._http_error(
                            response.status_code,
                            url,
                            _response_detail(body),
                        )
                    _logger.debug(
                        "http %s %s -> %d (stream)",
                        method,
                        _safe_url(url),
                        response.status_code,
                    )
                    async for chunk in response.aiter_bytes():
                        for event in decoder.feed_bytes(chunk):
                            if stop_on_done and event.data == DONE_SENTINEL:
                                return
                            emitted = True
                            yield event
                    for event in decoder.close():
                        if stop_on_done and event.data == DONE_SENTINEL:
                            return
                        emitted = True
                        yield event
                    return
            except httpx.TransportError as exc:
                if not emitted and attempt + 1 < self._retry.max_attempts:
                    attempt += 1
                    await self._sleep(self._delay_for(attempt))
                    _logger.debug(
                        "http %s %s retry %d/%d after transport error",
                        method,
                        _safe_url(url),
                        attempt,
                        self._retry.max_attempts,
                    )
                    continue
                raise self._transport_error(exc, url) from exc

    async def aiter_lines(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: object | None = None,
        params: Mapping[str, object] | None = None,
        stop_on_done: bool = True,
    ) -> AsyncIterator[str]:
        """Stream newline-delimited text lines, retrying before the first line.

        The line-framed sibling of :meth:`aiter_sse`, for wire protocols that
        are newline-delimited JSON (Ollama's native ``/api/chat``) rather than
        Server-Sent Events. Lines are yielded without their terminator; blank
        lines are skipped; the ``[DONE]`` sentinel ends iteration by default.
        The same retry boundary applies: retries happen only before the first
        line is yielded, and a mid-stream failure is surfaced, never replayed.
        """
        if self._closed:
            raise ProviderError("HTTP transport is closed")
        headers = self._merged_headers(headers)
        attempt = 0
        while True:
            emitted = False
            try:
                async with self._client.stream(
                    method, url, headers=headers, json=json, params=params
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        if (
                            self._retry.should_retry_status(response.status_code)
                            and attempt + 1 < self._retry.max_attempts
                        ):
                            attempt += 1
                            delay = self._delay_for(attempt, response)
                            await self._sleep(delay)
                            _logger.debug(
                                "http %s %s retry %d/%d after HTTP %d",
                                method,
                                _safe_url(url),
                                attempt,
                                self._retry.max_attempts,
                                response.status_code,
                            )
                            continue
                        raise self._http_error(
                            response.status_code,
                            url,
                            _response_detail(body),
                        )
                    _logger.debug(
                        "http %s %s -> %d (lines)",
                        method,
                        _safe_url(url),
                        response.status_code,
                    )
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if stop_on_done and line.strip() == DONE_SENTINEL:
                            return
                        emitted = True
                        yield line
                    return
            except httpx.TransportError as exc:
                if not emitted and attempt + 1 < self._retry.max_attempts:
                    attempt += 1
                    await self._sleep(self._delay_for(attempt))
                    _logger.debug(
                        "http %s %s retry %d/%d after transport error",
                        method,
                        _safe_url(url),
                        attempt,
                        self._retry.max_attempts,
                    )
                    continue
                raise self._transport_error(exc, url) from exc

    async def aclose(self) -> None:
        """Close the client only when this transport owns it. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()
