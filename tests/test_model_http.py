import logging

import httpx
import pytest

from nexus.errors import ProviderError
from nexus.model.http import (
    DONE_SENTINEL,
    HTTPTransport,
    RetryPolicy,
    SSEDecoder,
    SSEEvent,
)


class _Sleep:
    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)


def _transport(handler, *, retry=None, sleep=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HTTPTransport(
        client=client, retry=retry, sleep=sleep, jitter=lambda: 0.0
    )
    return transport, client


class _BrokenStream(httpx.AsyncByteStream):
    def __init__(self, prefix: bytes):
        self._prefix = prefix

    async def __aiter__(self):
        yield self._prefix
        raise httpx.ReadError("connection dropped")

    async def aclose(self):
        pass


# --------------------------------------------------------------------------
# SSE framing
# --------------------------------------------------------------------------


def test_sse_dispatch_basic_event():
    decoder = SSEDecoder()
    events = decoder.feed("event: message\ndata: hello\n\n")
    assert events == [SSEEvent(event="message", data="hello")]


def test_sse_multiline_data_is_concatenated_with_newline():
    decoder = SSEDecoder()
    events = decoder.feed("data: one\ndata: two\ndata: three\n\n")
    assert [event.data for event in events] == ["one\ntwo\nthree"]


def test_sse_comments_are_ignored_and_do_not_dispatch():
    decoder = SSEDecoder()
    assert decoder.feed(": keepalive\n\n") == []
    events = decoder.feed(": note\ndata: real\n\n")
    assert [event.data for event in events] == ["real"]


def test_sse_blank_line_without_data_does_not_dispatch():
    decoder = SSEDecoder()
    assert decoder.feed("\n") == []
    assert decoder.feed("event: orphan\n\n") == []


def test_sse_crlf_line_endings():
    decoder = SSEDecoder()
    events = decoder.feed("event: e\r\ndata: one\r\ndata: two\r\n\r\n")
    assert events == [SSEEvent(event="e", data="one\ntwo")]


def test_sse_cr_only_line_endings():
    decoder = SSEDecoder()
    events = decoder.feed("data: one\rdata: two\r\r")
    assert [event.data for event in events] == ["one\ntwo"]


def test_sse_id_and_retry_fields_are_tracked():
    decoder = SSEDecoder()
    events = decoder.feed("id: 42\nretry: 1500\ndata: x\n\n")
    assert events[0].id == "42"
    assert events[0].retry == 1500
    assert decoder.last_event_id == "42"
    assert decoder.retry_ms == 1500


def test_sse_retry_ignores_non_numeric_values():
    decoder = SSEDecoder()
    decoder.feed("retry: soon\ndata: x\n\n")
    assert decoder.retry_ms is None


def test_sse_value_strips_only_one_leading_space():
    decoder = SSEDecoder()
    events = decoder.feed("data:  two spaces\n\n")
    assert events[0].data == " two spaces"


def test_sse_line_split_across_chunks():
    decoder = SSEDecoder()
    assert decoder.feed("event: mess") == []
    assert decoder.feed("age\nda") == []
    events = decoder.feed("ta: hi\n\n")
    assert events == [SSEEvent(event="message", data="hi")]
    assert decoder.pending == ""


def test_sse_multibyte_character_split_across_chunks():
    decoder = SSEDecoder()
    encoded = "data: héllo\n\n".encode()
    assert decoder.feed_bytes(encoded[:8]) == []
    events = decoder.feed_bytes(encoded[8:])
    assert [event.data for event in events] == ["héllo"]


def test_sse_eof_discards_unterminated_event():
    decoder = SSEDecoder()
    assert decoder.feed("data: incomplete") == []
    assert decoder.close() == []


def test_sse_eof_after_terminated_event_is_clean():
    decoder = SSEDecoder()
    events = decoder.feed("data: complete\n\n")
    assert [event.data for event in events] == ["complete"]
    assert decoder.close() == []


def test_sse_crlf_split_across_chunks():
    decoder = SSEDecoder()
    assert decoder.feed("data: x\r") == []
    events = decoder.feed("\n\r\n")
    assert [event.data for event in events] == ["x"]


# --------------------------------------------------------------------------
# Retry and backoff
# --------------------------------------------------------------------------


async def test_request_retries_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"ok": True})

    sleeper = _Sleep()
    transport, client = _transport(
        handler,
        retry=RetryPolicy(max_attempts=3, base_delay=0.5, jitter_ratio=0.0),
        sleep=sleeper,
    )
    response = await transport.request("GET", "https://x/y")
    assert response.status_code == 200
    assert calls["n"] == 2
    assert sleeper.delays == [0.5]
    await client.aclose()


async def test_request_honours_retry_after_header():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "7"}, text="slow")
        return httpx.Response(200)

    sleeper = _Sleep()
    transport, client = _transport(
        handler,
        retry=RetryPolicy(max_attempts=3, base_delay=0.5, jitter_ratio=0.0),
        sleep=sleeper,
    )
    response = await transport.request("GET", "https://x/y")
    assert response.status_code == 200
    assert sleeper.delays == [7.0]
    await client.aclose()


async def test_request_retries_transport_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("nope")
        return httpx.Response(200)

    sleeper = _Sleep()
    transport, client = _transport(
        handler,
        retry=RetryPolicy(max_attempts=2, base_delay=0.25, jitter_ratio=0.0),
        sleep=sleeper,
    )
    response = await transport.request("GET", "https://x/y")
    assert response.status_code == 200
    assert sleeper.delays == [0.25]
    await client.aclose()


async def test_request_exhausts_retries_and_raises():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    sleeper = _Sleep()
    transport, client = _transport(
        handler,
        retry=RetryPolicy(max_attempts=2, base_delay=0.1, jitter_ratio=0.0),
        sleep=sleeper,
    )
    with pytest.raises(ProviderError) as excinfo:
        await transport.request("GET", "https://x/y")
    assert excinfo.value.status_code == 503
    assert calls["n"] == 2
    await client.aclose()


async def test_request_does_not_retry_client_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    sleeper = _Sleep()
    transport, client = _transport(
        handler,
        retry=RetryPolicy(max_attempts=3, base_delay=0.1, jitter_ratio=0.0),
        sleep=sleeper,
    )
    with pytest.raises(ProviderError) as excinfo:
        await transport.request("GET", "https://x/y")
    assert excinfo.value.status_code == 400
    assert calls["n"] == 1
    assert sleeper.delays == []
    await client.aclose()


# --------------------------------------------------------------------------
# SSE retry and mid-stream failures
# --------------------------------------------------------------------------


async def test_aiter_sse_retries_before_first_event():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, content=b"data: hi\n\n")

    sleeper = _Sleep()
    transport, client = _transport(
        handler,
        retry=RetryPolicy(max_attempts=3, base_delay=0.5, jitter_ratio=0.0),
        sleep=sleeper,
    )
    events = [event async for event in transport.aiter_sse("POST", "https://x/s")]
    assert [event.data for event in events] == ["hi"]
    assert calls["n"] == 2
    await client.aclose()


async def test_aiter_sse_does_not_replay_after_streaming():
    calls = {"n": 0}
    prefix = b"data: one\n\n"

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, stream=_BrokenStream(prefix))

    transport, client = _transport(
        handler,
        retry=RetryPolicy(max_attempts=3, base_delay=0.1, jitter_ratio=0.0),
        sleep=_Sleep(),
    )
    seen = []
    with pytest.raises(ProviderError):
        async for event in transport.aiter_sse("POST", "https://x/s"):
            seen.append(event)
    assert [event.data for event in seen] == ["one"]
    assert calls["n"] == 1
    await client.aclose()


async def test_aiter_sse_stops_on_done_sentinel():
    content = b"data: a\n\ndata: [DONE]\n\ndata: b\n\n"

    def handler(request):
        return httpx.Response(200, content=content)

    transport, client = _transport(handler)
    events = [event async for event in transport.aiter_sse("POST", "https://x/s")]
    assert [event.data for event in events] == ["a"]
    await client.aclose()


async def test_aiter_sse_can_observe_done_sentinel():
    content = b"data: a\n\ndata: [DONE]\n\ndata: b\n\n"

    def handler(request):
        return httpx.Response(200, content=content)

    transport, client = _transport(handler)
    events = [
        event
        async for event in transport.aiter_sse(
            "POST", "https://x/s", stop_on_done=False
        )
    ]
    assert [event.data for event in events] == ["a", DONE_SENTINEL, "b"]
    await client.aclose()


# --------------------------------------------------------------------------
# Ownership, close semantics, and logging
# --------------------------------------------------------------------------


async def test_injected_client_is_not_closed():
    transport, client = _transport(lambda request: httpx.Response(200))
    assert transport.owns_client is False
    await transport.aclose()
    assert transport.closed is True
    assert client.is_closed is False
    await client.aclose()


async def test_owned_client_is_closed_idempotently():
    transport = HTTPTransport(
        http_transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    assert transport.owns_client is True
    client = transport.client
    await transport.aclose()
    await transport.aclose()
    assert client.is_closed is True


async def test_request_after_close_raises():
    transport = HTTPTransport(
        http_transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    await transport.aclose()
    with pytest.raises(ProviderError):
        await transport.request("GET", "https://x/y")
    with pytest.raises(ProviderError):
        [event async for event in transport.aiter_sse("GET", "https://x/y")]


async def test_transport_logging_does_not_leak_secrets(caplog):
    def handler(request):
        return httpx.Response(200)

    transport, client = _transport(handler)
    with caplog.at_level(logging.DEBUG, logger="nexus.model.http"):
        await transport.request(
            "POST",
            "https://x/y?api_key=sk-query-secret",
            headers={"x-api-key": "sk-header-secret"},
            json={"prompt": "sk-body-secret"},
        )
    assert "sk-header-secret" not in caplog.text
    assert "sk-body-secret" not in caplog.text
    assert "sk-query-secret" not in caplog.text
    await client.aclose()


# --------------------------------------------------------------------------
# Error details must not echo credentials
# --------------------------------------------------------------------------


async def test_error_detail_redacts_echoed_bearer_token():
    secret = "sk-live-BEARERTOKEN0123456789"

    def handler(request):
        return httpx.Response(401, text=f"unauthorized: Bearer {secret}")

    transport, client = _transport(handler)
    with pytest.raises(ProviderError) as excinfo:
        await transport.request("GET", "https://x/y")

    assert excinfo.value.status_code == 401
    assert secret not in str(excinfo.value)
    assert secret not in repr(excinfo.value)
    assert "Bearer" in str(excinfo.value)  # actionability retained
    await client.aclose()


async def test_error_detail_redacts_echoed_api_key_value():
    secret = "SECRET-API-KEY-abcdef0123456789"

    def handler(request):
        return httpx.Response(
            403, json={"error": {"message": f"bad x-api-key: {secret}"}}
        )

    transport, client = _transport(handler)
    with pytest.raises(ProviderError) as excinfo:
        await transport.request("GET", "https://x/y")

    assert secret not in str(excinfo.value)
    assert secret not in repr(excinfo.value)
    assert "x-api-key" in str(excinfo.value)
    await client.aclose()


async def test_error_detail_redacts_raw_body_token():
    secret = "sk-ant-RAWTOKEN0123456789abcdef"

    def handler(request):
        return httpx.Response(400, text=f"key={secret} rejected")

    transport, client = _transport(handler)
    with pytest.raises(ProviderError) as excinfo:
        await transport.request("GET", "https://x/y")

    assert secret not in str(excinfo.value)
    assert secret not in repr(excinfo.value)
    assert "rejected" in str(excinfo.value)
    await client.aclose()


# --------------------------------------------------------------------------
# URL userinfo must never reach an error or a debug log
# --------------------------------------------------------------------------


def test_safe_url_strips_userinfo_and_query():
    from nexus.model.http import _safe_url

    safe = _safe_url("https://user:hunter2@gateway.test/v1?token=sk-secret")
    assert "hunter2" not in safe
    assert "sk-secret" not in safe
    assert "gateway.test" in safe
    assert safe.startswith("https://")


async def test_provider_error_redacts_url_userinfo():
    def handler(request):
        return httpx.Response(401, text="unauthorized")

    transport, client = _transport(handler)
    with pytest.raises(ProviderError) as excinfo:
        await transport.request(
            "GET", "https://user:hunter2@gateway.test/v1/models"
        )
    assert "hunter2" not in str(excinfo.value)
    assert "hunter2" not in repr(excinfo.value)
    assert "gateway.test" in str(excinfo.value)
    await client.aclose()


async def test_debug_log_redacts_url_userinfo(caplog):
    def handler(request):
        return httpx.Response(200)

    transport, client = _transport(handler)
    with caplog.at_level(logging.DEBUG, logger="nexus.model.http"):
        await transport.request(
            "GET", "https://user:hunter2@gateway.test/v1/models"
        )
    assert "hunter2" not in caplog.text
    await client.aclose()
