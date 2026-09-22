import base64
import json
import logging
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from nexus.errors import ProviderError
from nexus.model.http import HTTPTransport, RetryPolicy
from nexus.model.message import (
    Document,
    Image,
    Message,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)
from nexus.model.providers.anthropic import (
    AnthropicProvider,
    build_request_body,
    normalize_stop_reason,
)
from nexus.model.request import ModelRequest, SamplingParams, ToolSchema
from nexus.model.stream import (
    MessageStart,
    MessageStop,
    TextDelta,
    ThinkingDelta,
    ThinkingEnd,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class _Sleep:
    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)


class _Server:
    """Callable MockTransport handler that records requests."""

    def __init__(self, *responses):
        self.requests = []
        self._responses = list(responses)

    def __call__(self, request):
        self.requests.append(request)
        if self._responses:
            item = self._responses.pop(0)
        else:
            item = httpx.Response(200, content=b"")
        return item(request) if callable(item) else item


def _make(
    *,
    content: bytes = b"",
    handler=None,
    api_key="test-key",
    model="claude-test",
    environ=None,
    retry=None,
    sleep=None,
):
    if handler is None:
        def handler(request):
            return httpx.Response(200, content=content)

    transport = HTTPTransport(
        base_url="https://api.anthropic.com",
        http_transport=httpx.MockTransport(handler),
        retry=retry,
        sleep=sleep,
        jitter=lambda: 0.0,
    )
    provider = AnthropicProvider(
        api_key=api_key, model=model, transport=transport, environ=environ
    )
    return provider, transport


def _dummy_transport() -> HTTPTransport:
    return HTTPTransport(
        base_url="https://api.anthropic.com",
        http_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )


async def _collect(provider, request):
    return [event async for event in provider.stream(request)]


# --------------------------------------------------------------------------
# Request translation
# --------------------------------------------------------------------------


def test_build_request_body_translates_all_block_types():
    request = ModelRequest(
        messages=[
            Message("user", [Text("hi")]),
            Message(
                "assistant",
                [
                    Text("hello"),
                    Thinking("why", signature="sig"),
                    ToolUse("t1", "Read", {"path": "a"}),
                ],
            ),
            Message(
                "user",
                [
                    ToolResult("t1", [Text("contents")], is_error=True),
                    Image("image/png", data=b"xx"),
                    Document("application/pdf", b"%PDF", title="doc"),
                ],
            ),
        ],
        system="sys",
        tools=[ToolSchema("Read", "reads files", {"type": "object"})],
        params=SamplingParams(
            temperature=0.2,
            max_output_tokens=100,
            top_p=0.9,
            stop_sequences=["STOP"],
        ),
    )
    body = build_request_body(request, model="claude-x")

    assert body["model"] == "claude-x"
    assert body["stream"] is True
    assert body["max_tokens"] == 100
    assert body["system"] == "sys"
    assert body["temperature"] == 0.2
    assert body["top_p"] == 0.9
    assert body["stop_sequences"] == ["STOP"]
    assert body["tools"] == [
        {
            "name": "Read",
            "description": "reads files",
            "input_schema": {"type": "object"},
        }
    ]

    assert body["messages"][0] == {
        "role": "user",
        "content": [{"type": "text", "text": "hi"}],
    }
    assistant = body["messages"][1]["content"]
    assert assistant[0] == {"type": "text", "text": "hello"}
    assert assistant[1] == {
        "type": "thinking",
        "thinking": "why",
        "signature": "sig",
    }
    assert assistant[2] == {
        "type": "tool_use",
        "id": "t1",
        "name": "Read",
        "input": {"path": "a"},
    }
    user = body["messages"][2]["content"]
    assert user[0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": [{"type": "text", "text": "contents"}],
        "is_error": True,
    }
    assert user[1]["type"] == "image"
    assert user[1]["source"]["type"] == "base64"
    assert user[1]["source"]["media_type"] == "image/png"
    assert user[1]["source"]["data"] == base64.b64encode(b"xx").decode("ascii")
    assert user[2]["type"] == "document"
    assert user[2]["title"] == "doc"


def test_build_request_body_drops_unsigned_thinking():
    request = ModelRequest(
        messages=[
            Message("assistant", [Thinking("secret reasoning"), Text("visible")])
        ]
    )
    body = build_request_body(request, model="m")
    assert body["messages"][0]["content"] == [{"type": "text", "text": "visible"}]
    assert "secret reasoning" not in json.dumps(body)


def test_build_request_body_coalesces_adjacent_same_role():
    request = ModelRequest(
        messages=[
            Message("assistant", [ToolUse("t1", "Read", {"path": "a"})]),
            Message("user", [ToolResult("t1", [Text("contents")], is_error=True)]),
            Message("user", [Text("next")]),
        ]
    )
    body = build_request_body(request, model="m")
    assert [m["role"] for m in body["messages"]] == ["assistant", "user"]
    content = body["messages"][-1]["content"]
    # A recovered tool_result stays ahead of the following user text.
    assert content[0]["type"] == "tool_result"
    assert content[0]["tool_use_id"] == "t1"
    assert content[-1] == {"type": "text", "text": "next"}


def test_build_request_body_keeps_alternating_messages_separate():
    request = ModelRequest(
        messages=[
            Message("user", [Text("a")]),
            Message("assistant", [Text("b")]),
            Message("user", [Text("c")]),
        ]
    )
    body = build_request_body(request, model="m")
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][0]["content"] == [{"type": "text", "text": "a"}]
    assert body["messages"][2]["content"] == [{"type": "text", "text": "c"}]


def test_build_request_body_thinking_budget_omits_sampling_overrides():
    request = ModelRequest(
        messages=[Message("user", [Text("hi")])],
        params=SamplingParams(temperature=0.5, top_p=0.5, thinking_budget=4096),
    )
    body = build_request_body(request, model="m")
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert "temperature" not in body
    assert "top_p" not in body


def test_build_request_body_url_image_uses_url_source():
    request = ModelRequest(
        messages=[Message("user", [Image("image/png", url="https://x/i.png")])]
    )
    body = build_request_body(request, model="m")
    assert body["messages"][0]["content"][0] == {
        "type": "image",
        "source": {"type": "url", "url": "https://x/i.png"},
    }


def test_normalize_stop_reason_maps_known_and_unknown():
    assert normalize_stop_reason("tool_use") == "tool_use"
    assert normalize_stop_reason("pause_turn") == "end_turn"
    assert normalize_stop_reason("brand_new_reason") == "end_turn"
    assert normalize_stop_reason(None) is None


# --------------------------------------------------------------------------
# Streaming translation
# --------------------------------------------------------------------------


async def test_text_stream_and_wire_request():
    server = _Server(httpx.Response(200, content=_fixture("text_stream.sse")))
    provider, _ = _make(handler=server)
    request = ModelRequest(
        messages=[Message("user", [Text("hi")])], system="sys", model="claude-test"
    )
    events = await _collect(provider, request)

    assert isinstance(events[0], MessageStart)
    assert events[0].provider == "anthropic"
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello"
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.input == 12
    assert usage.output == 7
    assert usage.cache_read == 3
    assert usage.cache_write == 0
    assert isinstance(events[-1], MessageStop)
    assert events[-1].stop_reason == "end_turn"

    sent = json.loads(server.requests[0].content)
    assert sent["model"] == "claude-test"
    assert sent["system"] == "sys"
    assert sent["stream"] is True
    assert server.requests[0].headers["x-api-key"] == "test-key"
    assert server.requests[0].headers["anthropic-version"] == "2023-06-01"


async def test_thinking_and_tool_stream():
    server = _Server(
        httpx.Response(200, content=_fixture("thinking_tool_stream.sse"))
    )
    provider, _ = _make(handler=server)
    events = await _collect(provider, ModelRequest(messages=[]))

    thinking = [e for e in events if isinstance(e, ThinkingDelta)]
    assert [e.text for e in thinking] == ["Let me check."]
    signature = next(e for e in events if isinstance(e, ThinkingEnd))
    assert signature.signature == "ErUBCkYIBRg"

    start = next(e for e in events if isinstance(e, ToolCallStart))
    assert start.id == "toolu_01"
    assert start.name == "Read"
    partials = [e.partial_json for e in events if isinstance(e, ToolCallDelta)]
    assert "".join(partials) == '{"path": "a.txt"}'
    end = next(e for e in events if isinstance(e, ToolCallEnd))
    assert end.input == {"path": "a.txt"}

    assert events[-1].stop_reason == "tool_use"
    # Thinking is finalized before the tool call begins.
    assert events.index(signature) < events.index(start)


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("end_turn", "end_turn"),
        ("tool_use", "tool_use"),
        ("max_tokens", "max_tokens"),
        ("stop_sequence", "stop_sequence"),
        ("pause_turn", "end_turn"),
    ],
)
async def test_stop_reason_normalization(wire, expected):
    content = (
        'data: {"type":"message_start","message":{"model":"m","usage":{}}}\n\n'
        f'data: {{"type":"message_delta","delta":{{"stop_reason":"{wire}"}},'
        '"usage":{"output_tokens":1}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    ).encode()
    provider, _ = _make(content=content)
    events = await _collect(provider, ModelRequest(messages=[]))
    assert isinstance(events[-1], MessageStop)
    assert events[-1].stop_reason == expected


async def test_crlf_stream_is_parsed():
    content = _fixture("text_stream.sse").replace(b"\n", b"\r\n")
    provider, _ = _make(content=content)
    events = await _collect(provider, ModelRequest(messages=[]))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello"
    assert events[-1].stop_reason == "end_turn"


async def test_ping_events_are_ignored():
    content = (
        b'data: {"type":"message_start","message":{"model":"m","usage":{}}}\n\n'
        b'event: ping\ndata: {"type":"ping"}\n\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    provider, _ = _make(content=content)
    events = await _collect(provider, ModelRequest(messages=[]))
    assert isinstance(events[0], MessageStart)
    assert isinstance(events[-1], MessageStop)


# --------------------------------------------------------------------------
# Retries and failure mapping
# --------------------------------------------------------------------------


async def test_stream_retries_429_and_honours_retry_after():
    server = _Server(
        httpx.Response(429, headers={"retry-after": "3"}, text="slow down"),
        httpx.Response(200, content=_fixture("text_stream.sse")),
    )
    sleeper = _Sleep()
    provider, _ = _make(
        handler=server,
        retry=RetryPolicy(max_attempts=3, base_delay=0.1, jitter_ratio=0.0),
        sleep=sleeper,
    )
    events = await _collect(provider, ModelRequest(messages=[]))
    assert len(server.requests) == 2
    assert sleeper.delays == [3.0]
    assert isinstance(events[-1], MessageStop)


async def test_malformed_midstream_payload_raises():
    content = (
        b'data: {"type":"message_start","message":{"model":"m","usage":{}}}\n\n'
        b"data: {not json}\n\n"
    )
    provider, _ = _make(content=content)
    with pytest.raises(ProviderError):
        await _collect(provider, ModelRequest(messages=[]))


async def test_stream_error_event_raises():
    content = (
        b'data: {"type":"error","error":'
        b'{"type":"overloaded_error","message":"Overloaded"}}\n\n'
    )
    provider, _ = _make(content=content)
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert "overloaded_error" in str(excinfo.value)


async def test_midstream_disconnect_raises_without_replay():
    prefix = (
        b'data: {"type":"message_start","message":{"model":"m","usage":{}}}\n\n'
    )

    class _Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield prefix
            raise httpx.ReadError("gone")

        async def aclose(self):
            pass

    server = _Server(httpx.Response(200, stream=_Broken()))
    provider, _ = _make(handler=server)
    seen = []
    with pytest.raises(ProviderError):
        async for event in provider.stream(ModelRequest(messages=[])):
            seen.append(event)
    assert any(isinstance(e, MessageStart) for e in seen)
    assert len(server.requests) == 1


async def test_http_error_is_mapped_with_status_code():
    server = _Server(
        httpx.Response(
            401, json={"error": {"type": "authentication_error", "message": "bad"}}
        )
    )
    provider, _ = _make(handler=server)
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert excinfo.value.status_code == 401


async def test_http_error_detail_does_not_echo_api_key():
    secret = "sk-ant-LEAKME0123456789abcdef"

    def handler(request):
        return httpx.Response(
            401,
            json={"error": {"type": "authentication_error", "message": f"bad key {secret}"}},
        )

    provider, _ = _make(handler=handler)
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert secret not in str(excinfo.value)
    assert secret not in repr(excinfo.value)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


async def test_direct_api_key_is_sent():
    server = _Server(httpx.Response(200, content=_fixture("text_stream.sse")))
    provider, _ = _make(handler=server, api_key="sk-direct")
    await _collect(provider, ModelRequest(messages=[]))
    assert server.requests[0].headers["x-api-key"] == "sk-direct"


async def test_env_reference_api_key_is_resolved_at_use_time():
    server = _Server(httpx.Response(200, content=_fixture("text_stream.sse")))
    provider, _ = _make(
        handler=server, api_key="${env:MY_KEY}", environ={"MY_KEY": "sk-env"}
    )
    await _collect(provider, ModelRequest(messages=[]))
    assert server.requests[0].headers["x-api-key"] == "sk-env"


async def test_missing_api_key_raises_before_request():
    provider = AnthropicProvider(
        api_key=None, model="m", transport=_dummy_transport()
    )
    with pytest.raises(ProviderError):
        await _collect(provider, ModelRequest(messages=[]))


async def test_unresolved_env_reference_names_variable_without_secret():
    provider = AnthropicProvider(
        api_key="${env:NOPE_MISSING}",
        model="m",
        environ={},
        transport=_dummy_transport(),
    )
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert "NOPE_MISSING" in str(excinfo.value)


async def test_unsupported_key_reference_is_rejected():
    provider = AnthropicProvider(
        api_key="${keychain:anthropic}", model="m", transport=_dummy_transport()
    )
    with pytest.raises(ProviderError):
        await _collect(provider, ModelRequest(messages=[]))


async def test_secret_never_appears_in_repr_logs_or_errors(caplog):
    server = _Server(httpx.Response(200, content=_fixture("text_stream.sse")))
    provider, _ = _make(handler=server, api_key="sk-ant-secret")
    assert "sk-ant-secret" not in repr(provider)
    with caplog.at_level(logging.DEBUG, logger="nexus.model.http"):
        await _collect(provider, ModelRequest(messages=[]))
    assert "sk-ant-secret" not in caplog.text

    error_server = _Server(
        httpx.Response(
            401, json={"error": {"type": "authentication_error", "message": "no"}}
        )
    )
    provider2, _ = _make(handler=error_server, api_key="sk-ant-secret")
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider2, ModelRequest(messages=[]))
    assert "sk-ant-secret" not in str(excinfo.value)


# --------------------------------------------------------------------------
# Capabilities and lifecycle
# --------------------------------------------------------------------------


async def test_capabilities_are_accurate():
    provider = AnthropicProvider(
        api_key="k",
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    caps = provider.capabilities("claude-sonnet")
    assert caps.tools is True
    assert caps.parallel_tool_calls is True
    assert caps.streaming is True
    assert caps.thinking is True
    assert caps.prompt_caching is True
    assert caps.vision is True
    assert caps.documents is True
    assert caps.max_context_tokens == 200_000
    assert caps.degradation == {"thinking": "drop"}
    assert provider.capabilities("claude-haiku").max_output_tokens == 8192
    await provider.aclose()


async def test_count_tokens_returns_none():
    provider = AnthropicProvider(
        api_key="k",
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    assert await provider.count_tokens(ModelRequest(messages=[])) is None
    await provider.aclose()


async def test_provider_closes_owned_client_idempotently():
    provider = AnthropicProvider(
        api_key="k",
        model="m",
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    client = provider.transport.client
    await provider.aclose()
    await provider.aclose()
    assert client.is_closed is True


async def test_provider_leaves_injected_transport_open():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    transport = HTTPTransport(client=client)
    provider = AnthropicProvider(api_key="k", model="m", transport=transport)
    await provider.aclose()
    assert transport.closed is False
    assert client.is_closed is False
    await client.aclose()


async def test_closed_provider_rejects_stream():
    provider = AnthropicProvider(
        api_key="k",
        model="m",
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    await provider.aclose()
    with pytest.raises(ProviderError):
        await _collect(provider, ModelRequest(messages=[]))


async def test_from_config_reads_provider_section():
    class _Section:
        api_key = "${env:FROM_CFG}"
        base_url = None

    class _V2:
        providers: ClassVar[dict] = {"anthropic": _Section()}

    class _Config:
        v2 = _V2()
        model = "anthropic/claude-from-config"

    server = _Server(httpx.Response(200, content=_fixture("text_stream.sse")))
    provider = AnthropicProvider.from_config(
        _Config(),
        environ={"FROM_CFG": "sk-cfg"},
        http_transport=httpx.MockTransport(server),
    )
    await _collect(provider, ModelRequest(messages=[]))
    assert server.requests[0].headers["x-api-key"] == "sk-cfg"
    sent = json.loads(server.requests[0].content)
    assert sent["model"] == "claude-from-config"
    await provider.aclose()
