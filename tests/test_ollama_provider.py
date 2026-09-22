import base64
import json
import logging
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from nexus.errors import ConfigError, MalformedToolCall, ProviderError
from nexus.model.capabilities import CAPABILITY_FEATURES, Capabilities
from nexus.model.http import HTTPTransport, RetryPolicy
from nexus.model.message import (
    Image,
    Message,
    MessageMeta,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)
from nexus.model.providers.ollama import (
    DEFAULT_BASE_URL,
    MODE_OLLAMA,
    MODE_OPENAI,
    SYNTHETIC_CALL_PREFIX,
    OllamaProvider,
    build_native_request_body,
    build_openai_request_body,
    build_request_body,
    normalize_done_reason,
    normalize_finish_reason,
)
from nexus.model.request import ModelRequest, SamplingParams, ToolSchema
from nexus.model.stream import (
    MessageStart,
    MessageStop,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ollama"

READ_TOOL = ToolSchema(
    name="Read",
    description="reads files",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)


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
    api_key=None,
    model="qwen3:32b",
    api=MODE_OLLAMA,
    base_url=None,
    environ=None,
    retry=None,
    sleep=None,
    capabilities=None,
    capability_source=None,
):
    if handler is None:

        def handler(request):
            return httpx.Response(200, content=content)

    transport = HTTPTransport(
        base_url=base_url or DEFAULT_BASE_URL,
        http_transport=httpx.MockTransport(handler),
        retry=retry,
        sleep=sleep,
        jitter=lambda: 0.0,
    )
    provider = OllamaProvider(
        api_key=api_key,
        model=model,
        api=api,
        base_url=base_url,
        transport=transport,
        environ=environ,
        capabilities=capabilities,
        capability_source=capability_source,
    )
    return provider, transport


def _dummy_transport() -> HTTPTransport:
    return HTTPTransport(
        base_url=DEFAULT_BASE_URL,
        http_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )


async def _collect(provider, request):
    return [event async for event in provider.stream(request)]


# --------------------------------------------------------------------------
# Request translation
# --------------------------------------------------------------------------


def test_native_request_translates_system_messages_images_tools_and_options():
    request = ModelRequest(
        messages=[
            Message("user", [Text("hi")]),
            Message(
                "assistant",
                [Thinking("why"), ToolUse("call_1", "Read", {"path": "a"})],
                meta=MessageMeta(provider="ollama"),
            ),
            Message(
                "user",
                [
                    ToolResult("call_1", [Text("contents")], is_error=True),
                    Text("and look"),
                    Image("image/png", data=b"xx"),
                ],
            ),
        ],
        system="sys",
        tools=[READ_TOOL],
        params=SamplingParams(
            temperature=0.2,
            max_output_tokens=100,
            top_p=0.9,
            stop_sequences=["STOP"],
        ),
    )
    body = build_native_request_body(request, model="qwen3:32b")

    assert body["model"] == "qwen3:32b"
    assert body["stream"] is True
    assert body["messages"][0] == {"role": "system", "content": "sys"}

    assistant = body["messages"][2]
    assert assistant["role"] == "assistant"
    # Thinking is dropped; the tool call is atomic and an id the server issued
    # is echoed (a synthetic minted id would stay off the wire).
    assert assistant["content"] == ""
    assert assistant["tool_calls"] == [
        {"id": "call_1", "function": {"name": "Read", "arguments": {"path": "a"}}}
    ]

    tool_message = body["messages"][3]
    assert tool_message == {
        "role": "tool",
        "content": "contents",
        "tool_name": "Read",
    }
    follow_up = body["messages"][4]
    assert follow_up["content"] == "and look"
    assert follow_up["images"] == [base64.b64encode(b"xx").decode("ascii")]

    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "Read",
                "description": "reads files",
                "parameters": READ_TOOL.input_schema,
            },
        }
    ]
    assert body["options"] == {
        "temperature": 0.2,
        "num_predict": 100,
        "top_p": 0.9,
        "stop": ["STOP"],
    }


def test_native_request_omits_tools_and_degrades_history_when_disabled():
    request = ModelRequest(
        messages=[
            Message("assistant", [ToolUse("toolu_1", "Read", {"path": "a"})]),
            Message("user", [ToolResult("toolu_1", [Text("contents")])]),
        ],
        tools=[READ_TOOL],
    )
    body = build_native_request_body(request, model="m", tools_enabled=False)
    assert "tools" not in body
    rendered = json.dumps(body)
    assert "tool_calls" not in rendered
    assert "[tool call Read" in rendered
    assert "[tool result: contents]" in rendered
    assert all(message["role"] != "tool" for message in body["messages"])


def test_native_request_recovers_tool_name_from_synthetic_id():
    synthetic = f"{SYNTHETIC_CALL_PREFIX}Read_3f2a1c9e-1111-7000-8000-000000000000"
    request = ModelRequest(
        messages=[
            Message("assistant", [ToolUse(synthetic, "Read", {"path": "a"})]),
            Message("user", [ToolResult(synthetic, [Text("contents")])]),
        ]
    )
    body = build_native_request_body(request, model="m")
    call = body["messages"][0]["tool_calls"][0]
    assert "id" not in call
    assert body["messages"][1]["tool_name"] == "Read"


def test_openai_request_translates_parts_and_tools():
    request = ModelRequest(
        messages=[
            Message("user", [Text("look"), Image("image/png", data=b"xx")]),
            Message(
                "assistant",
                [ToolUse("call_1", "Read", {"path": "a"})],
                meta=MessageMeta(provider="ollama"),
            ),
            Message("user", [ToolResult("call_1", [Text("contents")])]),
        ],
        system="sys",
        tools=[READ_TOOL],
        params=SamplingParams(temperature=0.2, max_output_tokens=100),
    )
    body = build_openai_request_body(request, model="llama-3.2-3b")

    assert body["model"] == "llama-3.2-3b"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    parts = body["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")

    assistant = body["messages"][2]
    call = assistant["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["function"]["name"] == "Read"
    assert call["function"]["arguments"] == '{"path":"a"}'

    assert body["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "contents",
    }
    assert body["tools"][0]["function"]["name"] == "Read"
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 100


def test_openai_request_omits_tools_when_disabled():
    request = ModelRequest(
        messages=[Message("assistant", [ToolUse("toolu_1", "Read", {"path": "a"})])],
        tools=[READ_TOOL],
    )
    body = build_openai_request_body(request, model="m", tools_enabled=False)
    assert "tools" not in body
    assert "tool_calls" not in json.dumps(body)
    assert "[tool call Read" in json.dumps(body)


def test_build_request_body_dispatches_by_api():
    request = ModelRequest(messages=[Message("user", [Text("hi")])])
    native = build_request_body(request, model="m", api=MODE_OLLAMA)
    assert "options" not in native
    assert "stream_options" not in native
    compat = build_request_body(request, model="m", api=MODE_OPENAI)
    assert compat["stream_options"] == {"include_usage": True}


def test_normalize_reason_mappers():
    assert normalize_done_reason("stop") == "end_turn"
    assert normalize_done_reason("length") == "max_tokens"
    assert normalize_done_reason("tool_calls") == "tool_use"
    assert normalize_done_reason("refusal") == "refusal"
    assert normalize_done_reason("brand_new") == "end_turn"
    assert normalize_done_reason(None) is None
    assert normalize_finish_reason("tool_calls") == "tool_use"
    assert normalize_finish_reason("content_filter") == "refusal"
    assert normalize_finish_reason(None) is None


# --------------------------------------------------------------------------
# Native streaming
# --------------------------------------------------------------------------


async def test_native_text_stream_usage_and_finish():
    server = _Server(httpx.Response(200, content=_fixture("text_stream.ndjson")))
    provider, _ = _make(handler=server)
    events = await _collect(provider, ModelRequest(messages=[Message("user", [Text("hi")])]))

    assert isinstance(events[0], MessageStart)
    assert events[0].provider == "ollama"
    assert events[0].model == "qwen3:32b"
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello, world."
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.input == 12
    assert usage.output == 7
    assert usage.cache_read == 0
    assert isinstance(events[-1], MessageStop)
    assert events[-1].stop_reason == "end_turn"

    sent = json.loads(server.requests[0].content)
    assert sent["model"] == "qwen3:32b"
    assert "authorization" not in server.requests[0].headers
    assert server.requests[0].url.path == "/api/chat"


async def test_native_tool_call_mints_id_and_promotes_stop_reason():
    provider, _ = _make(content=_fixture("tool_call.ndjson"))
    events = await _collect(provider, ModelRequest(messages=[], tools=[READ_TOOL]))

    start = next(e for e in events if isinstance(e, ToolCallStart))
    assert start.name == "Read"
    assert start.id.startswith(f"{SYNTHETIC_CALL_PREFIX}Read_")
    token = start.id[len(f"{SYNTHETIC_CALL_PREFIX}Read_") :]
    assert len(token) == 36 and token.count("-") == 4
    end = next(e for e in events if isinstance(e, ToolCallEnd))
    assert end.id == start.id
    assert end.input == {"path": "a.txt"}
    assert events[-1].stop_reason == "tool_use"


async def test_native_tool_call_echoes_provided_id():
    content = (
        b'{"model":"qwen3:32b","message":{"role":"assistant","content":"",'
        b'"tool_calls":[{"id":"call_abc","function":{"name":"Read",'
        b'"arguments":{"path":"a.txt"}}}]},"done":true,"done_reason":"tool_calls"}\n'
    )
    provider, _ = _make(content=content)
    events = await _collect(provider, ModelRequest(messages=[]))
    start = next(e for e in events if isinstance(e, ToolCallStart))
    assert start.id == "call_abc"
    assert events[-1].stop_reason == "tool_use"


async def test_native_thinking_stream():
    provider, _ = _make(content=_fixture("thinking.ndjson"))
    events = await _collect(provider, ModelRequest(messages=[]))
    assert [e.text for e in events if isinstance(e, ThinkingDelta)] == [
        "Let me think."
    ]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Answer"
    assert events[-1].stop_reason == "end_turn"


async def test_native_error_frame_raises_and_redacts():
    secret = "sk-LEAKME0123456789abcdefghij"
    content = json.dumps({"error": f"bad key {secret}"}).encode() + b"\n"
    provider, _ = _make(content=content)
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert secret not in str(excinfo.value)


async def test_native_malformed_json_payload_raises():
    provider, _ = _make(content=b"not json at all\n")
    with pytest.raises(ProviderError):
        await _collect(provider, ModelRequest(messages=[]))


async def test_native_non_object_arguments_raise_after_start():
    content = (
        b'{"message":{"role":"assistant","tool_calls":[{"function":{"name":"Read",'
        b'"arguments":"not-an-object"}}]},"done":false}\n'
    )
    provider, _ = _make(content=content)
    seen = []
    with pytest.raises(MalformedToolCall) as excinfo:
        async for event in provider.stream(ModelRequest(messages=[])):
            seen.append(event)
    assert any(isinstance(e, ToolCallStart) for e in seen)
    assert excinfo.value.tool_call_id.startswith(f"{SYNTHETIC_CALL_PREFIX}Read_")


async def test_native_midstream_disconnect_raises_without_replay():
    prefix = b'{"model":"qwen3:32b","message":{"role":"assistant","content":"Hi"},"done":false}\n'

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
    assert any(isinstance(e, TextDelta) for e in seen)
    assert len(server.requests) == 1


async def test_native_retry_429_honours_retry_after():
    server = _Server(
        httpx.Response(429, headers={"retry-after": "3"}, text="slow down"),
        httpx.Response(200, content=_fixture("text_stream.ndjson")),
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


async def test_native_empty_stream_emits_start_and_stop():
    provider, _ = _make(content=b"")
    events = await _collect(provider, ModelRequest(messages=[], model="m"))
    assert isinstance(events[0], MessageStart)
    assert isinstance(events[-1], MessageStop)
    assert events[-1].stop_reason == "end_turn"


# --------------------------------------------------------------------------
# OpenAI-compatible streaming
# --------------------------------------------------------------------------


async def test_openai_text_stream_and_usage():
    server = _Server(httpx.Response(200, content=_fixture("openai_text_stream.sse")))
    provider, _ = _make(handler=server, api=MODE_OPENAI, model="llama-3.2-3b")
    events = await _collect(provider, ModelRequest(messages=[Message("user", [Text("hi")])]))

    assert isinstance(events[0], MessageStart)
    assert events[0].model == "llama-3.2-3b"
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello there"
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.input == 10
    assert usage.output == 2
    assert events[-1].stop_reason == "end_turn"

    sent = json.loads(server.requests[0].content)
    assert sent["stream_options"] == {"include_usage": True}
    assert server.requests[0].url.path == "/v1/chat/completions"


async def test_openai_tool_call_partial_json_accumulates():
    provider, _ = _make(
        content=_fixture("openai_tool_stream.sse"),
        api=MODE_OPENAI,
        model="llama-3.2-3b",
    )
    events = await _collect(provider, ModelRequest(messages=[], tools=[READ_TOOL]))

    start = next(e for e in events if isinstance(e, ToolCallStart))
    assert start.id == "call_1"
    assert start.name == "Read"
    deltas = [e.partial_json for e in events if isinstance(e, ToolCallDelta)]
    assert deltas == ['{"path":', '"a.txt"}']
    end = next(e for e in events if isinstance(e, ToolCallEnd))
    assert end.input == {"path": "a.txt"}
    assert events[-1].stop_reason == "tool_use"


async def test_openai_tool_call_without_id_mints_one():
    content = (
        b'data: {"model":"m","choices":[{"index":0,"delta":{"tool_calls":'
        b'[{"index":0,"function":{"name":"Read","arguments":"{\\"path\\":\\"a\\"}"}}]},'
        b'"finish_reason":null}]}\n\n'
        b'data: {"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    )
    provider, _ = _make(content=content, api=MODE_OPENAI)
    events = await _collect(provider, ModelRequest(messages=[]))
    start = next(e for e in events if isinstance(e, ToolCallStart))
    assert start.id.startswith(f"{SYNTHETIC_CALL_PREFIX}Read_")
    end = next(e for e in events if isinstance(e, ToolCallEnd))
    assert end.input == {"path": "a"}


async def test_openai_length_maps_to_max_tokens():
    content = (
        b'data: {"model":"m","choices":[{"index":0,"delta":{"content":"trunc"},'
        b'"finish_reason":"length"}]}\n\n'
    )
    provider, _ = _make(content=content, api=MODE_OPENAI)
    events = await _collect(provider, ModelRequest(messages=[]))
    assert events[-1].stop_reason == "max_tokens"


async def test_openai_timings_block_is_usage_fallback():
    content = (
        b'data: {"model":"m","choices":[{"index":0,"delta":{"content":"hi"},'
        b'"finish_reason":"stop"}],"timings":{"prompt_n":5,"predicted_n":3}}\n\n'
    )
    provider, _ = _make(content=content, api=MODE_OPENAI)
    events = await _collect(provider, ModelRequest(messages=[]))
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.input == 5
    assert usage.output == 3


def test_openai_skips_user_message_with_no_representable_parts():
    # An image with neither bytes nor a URL degrades to nothing; the adapter
    # must not send an empty content array.
    request = ModelRequest(messages=[Message("user", [Image("image/png")])])
    body = build_openai_request_body(request, model="m")
    assert body["messages"] == []


async def test_openai_error_object_raises():
    content = (
        b'data: {"error":{"message":"context length exceeded","code":400}}\n\n'
    )
    provider, _ = _make(content=content, api=MODE_OPENAI)
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert "context length exceeded" in str(excinfo.value)


async def test_openai_url_includes_v1_only_once():
    server = _Server(httpx.Response(200, content=_fixture("openai_text_stream.sse")))
    provider, _ = _make(
        handler=server,
        api=MODE_OPENAI,
        base_url="http://localhost:8080/v1",
    )
    await _collect(provider, ModelRequest(messages=[]))
    assert server.requests[0].url.path == "/v1/chat/completions"


# --------------------------------------------------------------------------
# Credentials, base URL, capabilities, lifecycle
# --------------------------------------------------------------------------


def test_default_base_url_is_localhost():
    assert OllamaProvider.DEFAULT_BASE_URL == "http://localhost:11434"
    provider = OllamaProvider(model="m", http_transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert provider.transport.client.base_url.host == "localhost"
    assert provider.api == MODE_OLLAMA


async def test_no_key_sends_no_authorization():
    server = _Server(httpx.Response(200, content=_fixture("text_stream.ndjson")))
    provider, _ = _make(handler=server)
    await _collect(provider, ModelRequest(messages=[]))
    assert "authorization" not in server.requests[0].headers
    await provider.aclose()


async def test_configured_key_sends_bearer_and_never_leaks(caplog):
    secret = "local-proxy-secret-0123456789"
    server = _Server(httpx.Response(200, content=_fixture("text_stream.ndjson")))
    provider, _ = _make(handler=server, api_key=secret)
    assert secret not in repr(provider)
    with caplog.at_level(logging.DEBUG, logger="nexus.model.http"):
        await _collect(provider, ModelRequest(messages=[]))
    assert secret not in caplog.text
    assert server.requests[0].headers["authorization"] == f"Bearer {secret}"


async def test_env_reference_api_key_is_resolved_at_use_time():
    server = _Server(httpx.Response(200, content=_fixture("text_stream.ndjson")))
    provider, _ = _make(
        handler=server, api_key="${env:OLLAMA_KEY}", environ={"OLLAMA_KEY": "env-key"}
    )
    await _collect(provider, ModelRequest(messages=[]))
    assert server.requests[0].headers["authorization"] == "Bearer env-key"


async def test_unresolved_env_reference_names_variable_without_secret():
    provider = OllamaProvider(
        api_key="${env:NOPE_MISSING}",
        model="m",
        environ={},
        transport=_dummy_transport(),
    )
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert "NOPE_MISSING" in str(excinfo.value)


async def test_unsupported_key_reference_is_rejected():
    provider = OllamaProvider(
        api_key="${keychain:ollama}", model="m", transport=_dummy_transport()
    )
    with pytest.raises(ProviderError):
        await _collect(provider, ModelRequest(messages=[]))


def test_unknown_api_mode_is_rejected():
    with pytest.raises(ConfigError):
        OllamaProvider(model="m", api="grpc")


async def test_count_tokens_returns_none_for_heuristic_fallback():
    provider = OllamaProvider(model="m", transport=_dummy_transport())
    assert await provider.count_tokens(ModelRequest(messages=[])) is None


def test_capabilities_fallback_is_conservative():
    provider = OllamaProvider(model="m", transport=_dummy_transport())
    caps = provider.capabilities("some-7b-model")
    assert caps.streaming is True
    assert caps.tools is False
    assert caps.vision is False
    assert caps.documents is False
    assert caps.max_context_tokens == 0
    assert set(caps.degradation) <= set(CAPABILITY_FEATURES)
    assert all(policy in {"drop", "to_text", "error"} for policy in caps.degradation.values())


def test_capabilities_come_from_injected_registry_source():
    injected = Capabilities(tools=True, max_context_tokens=131072, max_output_tokens=8192)
    seen = []

    def source(model):
        seen.append(model)
        return injected

    provider = OllamaProvider(
        model="m", capability_source=source, transport=_dummy_transport()
    )
    caps = provider.capabilities("qwen3:32b")
    # Registry-owned fields are adopted ...
    assert caps.tools is True
    assert caps.max_context_tokens == 131072
    assert caps.max_output_tokens == 8192
    # ... while the adapter's transport-only fields and degradation survive.
    assert caps.streaming is True
    assert caps.parallel_tool_calls is False
    assert caps.degradation == {
        "thinking": "drop",
        "vision": "to_text",
        "documents": "to_text",
    }
    assert seen == ["qwen3:32b"]


def test_capability_source_failure_falls_back():
    def boom(model):
        raise RuntimeError("registry unavailable")

    provider = OllamaProvider(
        model="m", capability_source=boom, transport=_dummy_transport()
    )
    assert provider.capabilities("m").tools is False


def test_static_capability_descriptor_is_used():
    caps = Capabilities(tools=True, max_output_tokens=123)
    provider = OllamaProvider(model="m", capabilities=caps, transport=_dummy_transport())
    assert provider.capabilities("whatever") is caps


async def test_tools_false_omits_schemas_from_stream_request():
    server = _Server(httpx.Response(200, content=_fixture("text_stream.ndjson")))
    provider, _ = _make(handler=server)
    await _collect(provider, ModelRequest(messages=[], tools=[READ_TOOL]))
    sent = json.loads(server.requests[0].content)
    assert "tools" not in sent


async def test_tools_capability_sends_schemas_when_injected():
    server = _Server(httpx.Response(200, content=_fixture("text_stream.ndjson")))
    provider, _ = _make(handler=server, capabilities=Capabilities(tools=True))
    await _collect(provider, ModelRequest(messages=[], tools=[READ_TOOL]))
    sent = json.loads(server.requests[0].content)
    assert sent["tools"][0]["function"]["name"] == "Read"


async def test_close_is_idempotent_and_blocks_streaming():
    provider = OllamaProvider(model="m", transport=_dummy_transport())
    await provider.aclose()
    await provider.aclose()
    with pytest.raises(ProviderError):
        await _collect(provider, ModelRequest(messages=[]))


async def test_provider_leaves_injected_transport_open():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    transport = HTTPTransport(client=client)
    provider = OllamaProvider(model="m", transport=transport)
    await provider.aclose()
    assert transport.closed is False
    assert client.is_closed is False
    await client.aclose()


async def test_from_config_reads_ollama_section():
    class _Section:
        kind = None
        api_key = "${env:OLLAMA_KEY}"
        base_url = "http://localhost:9999"
        api = None

    class _V2:
        providers: ClassVar[dict] = {"ollama": _Section()}

    class _Config:
        v2 = _V2()
        model = "ollama/qwen3:32b"

    provider = OllamaProvider.from_config(
        _Config(),
        environ={"OLLAMA_KEY": "cfg-key"},
        transport=_dummy_transport(),
    )
    assert provider._model == "qwen3:32b"
    assert provider.api == MODE_OLLAMA
    assert provider._base_url == "http://localhost:9999"
    assert provider._resolve_api_key() == "cfg-key"


async def test_from_config_kind_openai_compatible_selects_openai_mode():
    class _Section:
        kind = "openai_compatible"
        api_key = None
        base_url = "http://localhost:8080"
        api = None

    class _V2:
        providers: ClassVar[dict] = {"ollama": _Section()}

    class _Config:
        v2 = _V2()
        model = None

    provider = OllamaProvider.from_config(_Config(), transport=_dummy_transport())
    assert provider.api == MODE_OPENAI
