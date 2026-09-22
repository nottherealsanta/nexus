import base64
import json
import logging
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from nexus.errors import MalformedToolCall, ProviderError
from nexus.model.capabilities import CAPABILITY_FEATURES, Capabilities
from nexus.model.http import HTTPTransport, RetryPolicy
from nexus.model.message import (
    Document,
    Image,
    Message,
    MessageMeta,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)
from nexus.model.providers.gemini import (
    GeminiProvider,
    build_count_tokens_body,
    build_request_body,
    normalize_finish_reason,
)
from nexus.model.request import ModelRequest, SamplingParams, ToolSchema
from nexus.model.stream import (
    MessageStart,
    MessageStop,
    TextDelta,
    ThinkingDelta,
    ThinkingEnd,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gemini"


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
    model="gemini-3-flash",
    environ=None,
    retry=None,
    sleep=None,
):
    if handler is None:

        def handler(request):
            return httpx.Response(200, content=content)

    transport = HTTPTransport(
        base_url="https://generativelanguage.googleapis.com",
        http_transport=httpx.MockTransport(handler),
        retry=retry,
        sleep=sleep,
        jitter=lambda: 0.0,
    )
    provider = GeminiProvider(
        api_key=api_key, model=model, transport=transport, environ=environ
    )
    return provider, transport


def _dummy_transport() -> HTTPTransport:
    return HTTPTransport(
        base_url="https://generativelanguage.googleapis.com",
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
                    Thinking("why", signature="sigA"),
                    ToolUse("call_1", "Read", {"path": "a"}),
                ],
                meta=MessageMeta(provider="gemini"),
            ),
            Message(
                "user",
                [
                    ToolResult("call_1", [Text("contents")], is_error=True),
                    Image("image/png", data=b"xx"),
                    Document("application/pdf", b"%PDF", title="doc"),
                ],
            ),
        ],
        system="sys",
        tools=[
            ToolSchema(
                "Read",
                "reads files",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            )
        ],
        params=SamplingParams(
            temperature=0.2,
            max_output_tokens=100,
            top_p=0.9,
            stop_sequences=["STOP"],
            thinking_budget=1024,
        ),
        model="gemini-3-pro",
    )
    body = build_request_body(request, model="gemini-3-pro")

    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["systemInstruction"] == {"parts": [{"text": "sys"}]}

    model_parts = body["contents"][1]["parts"]
    assert model_parts[0] == {"text": "why", "thought": True}
    assert model_parts[1] == {
        "functionCall": {
            "name": "Read",
            "args": {"path": "a"},
            "id": "call_1",
        },
        "thoughtSignature": "sigA",
    }

    user_parts = body["contents"][2]["parts"]
    assert user_parts[0] == {
        "functionResponse": {
            "name": "Read",
            "response": {"content": "contents", "isError": True},
            "id": "call_1",
        }
    }
    assert user_parts[1] == {
        "inlineData": {
            "mimeType": "image/png",
            "data": base64.b64encode(b"xx").decode("ascii"),
        }
    }
    assert user_parts[2]["inlineData"]["mimeType"] == "application/pdf"

    declaration = body["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "Read"
    # ``additionalProperties`` is a JSON Schema keyword Gemini rejects.
    assert "additionalProperties" not in declaration["parameters"]

    generation = body["generationConfig"]
    assert generation["temperature"] == 0.2
    assert generation["maxOutputTokens"] == 100
    assert generation["topP"] == 0.9
    assert generation["stopSequences"] == ["STOP"]
    assert generation["thinkingConfig"] == {
        "thinkingBudget": 1024,
        "includeThoughts": True,
    }


def test_build_request_body_drops_foreign_and_unsigned_thinking():
    request = ModelRequest(
        messages=[
            Message(
                "assistant",
                [Thinking("foreign", signature="anthropic-sig"), Text("a")],
                meta=MessageMeta(provider="anthropic"),
            ),
            Message("assistant", [Thinking("unsigned"), Text("b")]),
        ]
    )
    body = build_request_body(request, model="m")
    rendered = json.dumps(body)
    assert "foreign" not in rendered
    assert "anthropic-sig" not in rendered
    assert "unsigned" not in rendered
    # Both messages coalesce to a single model turn of plain text.
    assert body["contents"] == [
        {"role": "model", "parts": [{"text": "a"}, {"text": "b"}]}
    ]


def test_build_request_body_coalesces_adjacent_same_role():
    request = ModelRequest(
        messages=[
            Message(
                "assistant",
                [ToolUse("call_1", "Read", {"path": "a"})],
            ),
            Message("user", [ToolResult("call_1", [Text("contents")])]),
            Message("user", [Text("next")]),
        ]
    )
    body = build_request_body(request, model="m")
    assert [c["role"] for c in body["contents"]] == ["model", "user"]
    parts = body["contents"][-1]["parts"]
    assert parts[0]["functionResponse"]["name"] == "Read"
    assert parts[-1] == {"text": "next"}


def test_build_request_body_recovers_name_from_synthetic_id():
    # A tool result whose originating call had no provider id: the name is
    # recovered from the synthetic ``nexus_<name>_<uuid>`` id, and Gemini is not
    # sent an ``id`` it never issued.
    synthetic = "nexus_Read_3f2a1c9e-1111-7000-8000-000000000000"
    request = ModelRequest(
        messages=[
            Message("assistant", [ToolUse(synthetic, "Read", {"path": "a"})]),
            Message("user", [ToolResult(synthetic, [Text("contents")])]),
        ]
    )
    body = build_request_body(request, model="m")
    call = body["contents"][0]["parts"][0]["functionCall"]
    assert "id" not in call
    function_response = body["contents"][1]["parts"][0]["functionResponse"]
    assert function_response["name"] == "Read"
    assert "id" not in function_response


def test_build_request_body_foreign_tool_id_is_not_echoed():
    # An Anthropic tool id is not a Gemini id; the call still goes out by name
    # and the response omits an id.
    request = ModelRequest(
        messages=[
            Message(
                "assistant",
                [ToolUse("toolu_01", "Read", {"path": "a"})],
                meta=MessageMeta(provider="anthropic"),
            ),
            Message("user", [ToolResult("toolu_01", [Text("contents")])]),
        ]
    )
    body = build_request_body(request, model="m")
    call = body["contents"][0]["parts"][0]["functionCall"]
    assert "id" not in call
    assert body["contents"][1]["parts"][0]["functionResponse"]["name"] == "Read"


def test_build_request_body_signature_attaches_to_thought_text():
    # No function call: the thought signature rides on the thought text part.
    request = ModelRequest(
        messages=[
            Message(
                "assistant",
                [Thinking("weighing options", signature="sig-text")],
                meta=MessageMeta(provider="gemini"),
            )
        ]
    )
    body = build_request_body(request, model="m")
    assert body["contents"][0]["parts"] == [
        {
            "text": "weighing options",
            "thought": True,
            "thoughtSignature": "sig-text",
        }
    ]


def test_build_request_body_signature_only_thinking_emits_carrier():
    # Gemini documents an empty thought part carrying only a signature.
    request = ModelRequest(
        messages=[
            Message(
                "assistant",
                [Thinking("", signature="sig-only")],
                meta=MessageMeta(provider="gemini"),
            )
        ]
    )
    body = build_request_body(request, model="m")
    assert body["contents"][0]["parts"] == [
        {"text": "", "thought": True, "thoughtSignature": "sig-only"}
    ]


def test_build_request_body_signature_attaches_only_to_first_tool_call():
    # Gemini validates the signature on the first functionCall of a turn; the
    # parallel calls that follow carry no signature.
    request = ModelRequest(
        messages=[
            Message(
                "assistant",
                [
                    Thinking("", signature="sig-fc"),
                    ToolUse("a", "Read", {"path": "a"}),
                    ToolUse("b", "Read", {"path": "b"}),
                ],
                meta=MessageMeta(provider="gemini"),
            )
        ]
    )
    body = build_request_body(request, model="m")
    first, second = body["contents"][0]["parts"]
    assert first == {
        "functionCall": {"name": "Read", "args": {"path": "a"}, "id": "a"},
        "thoughtSignature": "sig-fc",
    }
    assert second == {
        "functionCall": {"name": "Read", "args": {"path": "b"}, "id": "b"}
    }


def test_build_request_body_omits_parameters_for_no_arg_tool():
    request = ModelRequest(
        messages=[Message("user", [Text("hi")])],
        tools=[ToolSchema("Ping", "no args", {"type": "object"})],
    )
    body = build_request_body(request, model="m")
    declaration = body["tools"][0]["functionDeclarations"][0]
    assert declaration == {"name": "Ping", "description": "no args"}


def test_build_request_body_url_image_uses_file_data():
    request = ModelRequest(
        messages=[Message("user", [Image("image/png", url="https://x/i.png")])]
    )
    body = build_request_body(request, model="m")
    assert body["contents"][0]["parts"][0] == {
        "fileData": {"mimeType": "image/png", "fileUri": "https://x/i.png"}
    }


def test_thinking_budget_zero_disables_thoughts():
    request = ModelRequest(
        messages=[Message("user", [Text("hi")])],
        params=SamplingParams(thinking_budget=0),
    )
    body = build_request_body(request, model="m")
    assert body["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 0,
        "includeThoughts": False,
    }


def test_build_count_tokens_body_wraps_generate_content_request():
    request = ModelRequest(
        messages=[Message("user", [Text("hi")])],
        system="sys",
        tools=[ToolSchema("Read", "r", {"type": "object"})],
        params=SamplingParams(temperature=0.5, max_output_tokens=10),
    )
    body = build_count_tokens_body(request, model="m")
    inner = body["generateContentRequest"]
    assert inner["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert inner["tools"][0]["functionDeclarations"][0]["name"] == "Read"
    # Generation-only fields are omitted; they cannot change the count.
    assert "generationConfig" not in inner


def test_normalize_finish_reason_maps_known_and_unknown():
    assert normalize_finish_reason("STOP") == "end_turn"
    assert normalize_finish_reason("MAX_TOKENS") == "max_tokens"
    assert normalize_finish_reason("SAFETY") == "refusal"
    assert normalize_finish_reason("MALFORMED_FUNCTION_CALL") == "error"
    assert normalize_finish_reason("BRAND_NEW") == "end_turn"
    assert normalize_finish_reason(None) is None


# --------------------------------------------------------------------------
# Streaming translation
# --------------------------------------------------------------------------


async def test_text_stream_and_wire_request():
    server = _Server(httpx.Response(200, content=_fixture("text_stream.sse")))
    provider, _ = _make(handler=server)
    request = ModelRequest(
        messages=[Message("user", [Text("hi")])], system="sys", model="gemini-3-flash"
    )
    events = await _collect(provider, request)

    assert isinstance(events[0], MessageStart)
    assert events[0].provider == "gemini"
    assert events[0].model == "gemini-3-flash"
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello"
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.input == 12
    assert usage.output == 7
    assert usage.cache_read == 3
    assert usage.reasoning == 0
    assert isinstance(events[-1], MessageStop)
    assert events[-1].stop_reason == "end_turn"

    sent = json.loads(server.requests[0].content)
    assert "model" not in sent
    assert sent["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert server.requests[0].headers["x-goog-api-key"] == "test-key"
    url = server.requests[0].url
    assert url.path.endswith("/v1beta/models/gemini-3-flash:streamGenerateContent")
    assert url.params["alt"] == "sse"


async def test_thinking_and_tool_stream():
    server = _Server(
        httpx.Response(200, content=_fixture("thinking_tool_stream.sse"))
    )
    provider, _ = _make(handler=server)
    events = await _collect(provider, ModelRequest(messages=[]))

    thinking = [e for e in events if isinstance(e, ThinkingDelta)]
    assert [e.text for e in thinking] == ["Let me check."]
    signature = next(e for e in events if isinstance(e, ThinkingEnd))
    assert signature.signature == "c2lnbmF0dXJl"

    start = next(e for e in events if isinstance(e, ToolCallStart))
    assert start.id == "call_abc"
    assert start.name == "Read"
    end = next(e for e in events if isinstance(e, ToolCallEnd))
    assert end.input == {"path": "a.txt"}

    assert events[-1].stop_reason == "tool_use"
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.reasoning == 5
    # Thinking is finalized before the tool call begins.
    assert events.index(signature) < events.index(start)


async def test_synthetic_tool_id_when_gemini_omits_one():
    content = (
        b'data: {"candidates":[{"content":{"role":"model","parts":'
        b'[{"functionCall":{"name":"Read","args":{"path":"a.txt"}}}]},'
        b'"finishReason":"STOP","index":0}],"modelVersion":"gemini-3-pro"}\n\n'
    )
    provider, _ = _make(content=content)
    events = await _collect(provider, ModelRequest(messages=[]))
    start = next(e for e in events if isinstance(e, ToolCallStart))
    assert start.id.startswith("nexus_Read_")
    # The id carries a UUID, so it is unique across turns (not an index).
    token = start.id[len("nexus_Read_") :]
    assert len(token) == 36 and token.count("-") == 4
    end = next(e for e in events if isinstance(e, ToolCallEnd))
    assert end.input == {"path": "a.txt"}
    assert events[-1].stop_reason == "tool_use"


async def test_synthetic_tool_ids_are_unique_across_turns_and_replay():
    # Two turns each call Read with no provider id. The synthetic ids must
    # differ across turns, and a replayed history must recover the name from the
    # id without inventing a Gemini ``id``.
    content = (
        b'data: {"candidates":[{"content":{"role":"model","parts":'
        b'[{"functionCall":{"name":"Read","args":{"path":"a.txt"}}}]},'
        b'"finishReason":"STOP","index":0}],"modelVersion":"gemini-3-pro"}\n\n'
    )
    provider, _ = _make(content=content + content)
    first = await _collect(provider, ModelRequest(messages=[]))
    second = await _collect(provider, ModelRequest(messages=[]))
    id_one = next(e for e in first if isinstance(e, ToolCallStart)).id
    id_two = next(e for e in second if isinstance(e, ToolCallStart)).id
    assert id_one != id_two

    # Replay both calls plus their results in one request.
    request = ModelRequest(
        messages=[
            Message(
                "assistant",
                [ToolUse(id_one, "Read", {"path": "a.txt"})],
                meta=MessageMeta(provider="gemini"),
            ),
            Message("user", [ToolResult(id_one, [Text("one")])]),
            Message(
                "assistant",
                [ToolUse(id_two, "Read", {"path": "b.txt"})],
                meta=MessageMeta(provider="gemini"),
            ),
            Message("user", [ToolResult(id_two, [Text("two")])]),
        ]
    )
    body = build_request_body(request, model="m")
    calls = [
        part["functionCall"]
        for content in body["contents"]
        for part in content["parts"]
        if "functionCall" in part
    ]
    assert [call["name"] for call in calls] == ["Read", "Read"]
    assert all("id" not in call for call in calls)
    responses = [
        part["functionResponse"]
        for content in body["contents"]
        for part in content["parts"]
        if "functionResponse" in part
    ]
    assert [response["name"] for response in responses] == ["Read", "Read"]
    assert all("id" not in response for response in responses)


async def test_non_object_function_args_raise_malformed_after_start():
    content = (
        b'data: {"candidates":[{"content":{"role":"model","parts":'
        b'[{"functionCall":{"name":"Read","args":"not-an-object"}}]}}]}\n\n'
    )
    provider, _ = _make(content=content)
    seen = []
    with pytest.raises(MalformedToolCall) as excinfo:
        async for event in provider.stream(ModelRequest(messages=[])):
            seen.append(event)
    # The start is surfaced first so the loop's collector knows the call and can
    # turn the malformed frame into a model-visible error result.
    assert any(isinstance(e, ToolCallStart) for e in seen)
    assert excinfo.value.tool_call_id.startswith("nexus_Read_")


async def test_thinking_signature_split_across_chunks_emits_one_end():
    content = (
        b'data: {"candidates":[{"content":{"role":"model","parts":'
        b'[{"text":"one","thought":true}]}}]}\n\n'
        b'data: {"candidates":[{"content":{"role":"model","parts":'
        b'[{"text":"two","thought":true}]}}]}\n\n'
        b'data: {"candidates":[{"content":{"role":"model","parts":'
        b'[{"text":"","thought":true,"thoughtSignature":"sig-late"}]}}]}\n\n'
        b'data: {"candidates":[{"content":{"role":"model","parts":'
        b'[{"text":"answer"}]},"finishReason":"STOP","index":0}]}\n\n'
    )
    provider, _ = _make(content=content)
    events = await _collect(provider, ModelRequest(messages=[]))
    assert [e.text for e in events if isinstance(e, ThinkingDelta)] == [
        "one",
        "two",
    ]
    signatures = [e.signature for e in events if isinstance(e, ThinkingEnd)]
    assert signatures == ["sig-late"]
    assert "answer" in "".join(
        e.text for e in events if isinstance(e, TextDelta)
    )


async def test_refusal_stream():
    provider, _ = _make(content=_fixture("refusal.sse"))
    events = await _collect(provider, ModelRequest(messages=[]))
    assert isinstance(events[0], MessageStart)
    assert events[-1].stop_reason == "refusal"


async def test_prompt_feedback_block_reason_is_refusal():
    content = (
        b'data: {"promptFeedback":{"blockReason":"SAFETY"},'
        b'"candidates":[],"modelVersion":"gemini-3-flash"}\n\n'
    )
    provider, _ = _make(content=content)
    events = await _collect(provider, ModelRequest(messages=[]))
    assert events[-1].stop_reason == "refusal"


async def test_empty_stream_still_emits_start_and_stop():
    provider, _ = _make(content=b"")
    events = await _collect(provider, ModelRequest(messages=[], model="m"))
    assert isinstance(events[0], MessageStart)
    assert isinstance(events[-1], MessageStop)
    assert events[-1].stop_reason == "end_turn"


async def test_malformed_midstream_payload_raises():
    content = b'data: {"candidates":[]}\n\ndata: {not json}\n\n'
    provider, _ = _make(content=content)
    with pytest.raises(ProviderError):
        await _collect(provider, ModelRequest(messages=[]))


async def test_stream_error_object_raises():
    content = (
        b'data: {"error":{"code":429,"status":"RESOURCE_EXHAUSTED",'
        b'"message":"quota"}}\n\n'
    )
    provider, _ = _make(content=content)
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert "RESOURCE_EXHAUSTED" in str(excinfo.value)


async def test_stream_error_detail_redacts_url_userinfo():
    content = (
        b'data: {"error":{"status":"INVALID_ARGUMENT",'
        b'"message":"bad key at https://user:pass@host/v1"}}\n\n'
    )
    provider, _ = _make(content=content)
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert "user:pass" not in str(excinfo.value)
    assert "<redacted>@" in str(excinfo.value)


async def test_stream_error_detail_redacts_api_key_pattern():
    secret = "AIzaSyLEAKME0123456789abcdefghijkl"
    content = (
        b'data: {"error":{"status":"INVALID_ARGUMENT",'
        b'"message":"bad key ' + secret.encode() + b'"}}\n\n'
    )
    provider, _ = _make(content=content)
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert secret not in str(excinfo.value)


async def test_midstream_disconnect_raises_without_replay():
    prefix = b'data: {"candidates":[{"content":{"parts":[{"text":"Hi"}]}}]}\n\n'

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


async def test_http_error_is_mapped_with_status_code():
    server = _Server(
        httpx.Response(
            401,
            json={"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "bad"}},
        )
    )
    provider, _ = _make(handler=server)
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert excinfo.value.status_code == 401


async def test_http_error_detail_does_not_echo_api_key():
    secret = "AIzaSyLEAKME0123456789abcdefghijkl"

    def handler(request):
        return httpx.Response(
            401,
            json={"error": {"message": f"bad key {secret}"}},
        )

    provider, _ = _make(handler=handler)
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert secret not in str(excinfo.value)
    assert secret not in repr(excinfo.value)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


async def test_env_reference_api_key_is_resolved_at_use_time():
    server = _Server(httpx.Response(200, content=_fixture("text_stream.sse")))
    provider, _ = _make(
        handler=server, api_key="${env:MY_KEY}", environ={"MY_KEY": "env-key"}
    )
    await _collect(provider, ModelRequest(messages=[]))
    assert server.requests[0].headers["x-goog-api-key"] == "env-key"


async def test_missing_api_key_raises_before_request():
    provider = GeminiProvider(api_key=None, model="m", transport=_dummy_transport())
    with pytest.raises(ProviderError):
        await _collect(provider, ModelRequest(messages=[]))


async def test_unresolved_env_reference_names_variable_without_secret():
    provider = GeminiProvider(
        api_key="${env:NOPE_MISSING}",
        model="m",
        environ={},
        transport=_dummy_transport(),
    )
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider, ModelRequest(messages=[]))
    assert "NOPE_MISSING" in str(excinfo.value)


async def test_unsupported_key_reference_is_rejected():
    provider = GeminiProvider(
        api_key="${keychain:gemini}", model="m", transport=_dummy_transport()
    )
    with pytest.raises(ProviderError):
        await _collect(provider, ModelRequest(messages=[]))


async def test_secret_never_appears_in_repr_logs_or_errors(caplog):
    server = _Server(httpx.Response(200, content=_fixture("text_stream.sse")))
    provider, _ = _make(handler=server, api_key="AIza-secret")
    assert "AIza-secret" not in repr(provider)
    with caplog.at_level(logging.DEBUG, logger="nexus.model.http"):
        await _collect(provider, ModelRequest(messages=[]))
    assert "AIza-secret" not in caplog.text

    error_server = _Server(httpx.Response(401, json={"error": {"message": "no"}}))
    provider2, _ = _make(handler=error_server, api_key="AIza-secret")
    with pytest.raises(ProviderError) as excinfo:
        await _collect(provider2, ModelRequest(messages=[]))
    assert "AIza-secret" not in str(excinfo.value)


# --------------------------------------------------------------------------
# Capabilities and lifecycle
# --------------------------------------------------------------------------


async def test_capabilities_fallback_when_nothing_injected():
    provider = GeminiProvider(
        api_key="k",
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    caps = provider.capabilities("gemini-3-pro")
    assert caps.tools is True
    assert caps.streaming is True
    assert caps.thinking is True
    assert caps.vision is True
    assert caps.documents is True
    assert caps.max_context_tokens == 1_000_000
    assert caps.max_output_tokens == 65536
    assert caps.degradation == {
        "thinking": "drop",
        "vision": "to_text",
        "documents": "to_text",
    }
    assert provider.capabilities("gemini-1.5-flash").max_output_tokens == 8192
    await provider.aclose()


async def test_capabilities_come_from_injected_registry_source():
    injected = Capabilities(
        tools=True,
        thinking=False,
        max_context_tokens=500_000,
        max_output_tokens=4096,
    )
    seen = []

    def source(model):
        seen.append(model)
        return injected

    provider = GeminiProvider(
        api_key="k",
        capability_source=source,
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    caps = provider.capabilities("gemini-3-flash")
    # Registry-owned fields are adopted ...
    assert caps.tools is True
    assert caps.thinking is False
    assert caps.max_context_tokens == 500_000
    assert caps.max_output_tokens == 4096
    # ... while the transport-only fields and degradation policy survive.
    assert caps.streaming is True
    assert caps.degradation["thinking"] in {"drop", "to_text", "error"}
    assert seen == ["gemini-3-flash"]
    await provider.aclose()


async def test_capabilities_static_descriptor_is_used():
    caps = Capabilities(tools=True, max_output_tokens=123)
    provider = GeminiProvider(
        api_key="k",
        capabilities=caps,
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    assert provider.capabilities("whatever") is caps
    await provider.aclose()


async def test_capability_source_failure_falls_back():
    def boom(model):
        raise RuntimeError("registry unavailable")

    provider = GeminiProvider(
        api_key="k",
        capability_source=boom,
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    assert provider.capabilities("gemini-3-pro").tools is True
    await provider.aclose()


def _fallback_caps() -> Capabilities:
    return GeminiProvider(
        api_key="k",
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    ).capabilities("gemini-3-pro")


def test_fallback_degradation_policies_are_loop_recognized():
    caps = _fallback_caps()
    assert set(caps.degradation) <= set(CAPABILITY_FEATURES)
    assert all(
        policy in {"drop", "to_text", "error"}
        for policy in caps.degradation.values()
    )
    # The blocks the policies describe are the ones Gemini actually gets wrong.
    assert caps.degradation["vision"] == "to_text"
    assert caps.degradation["documents"] == "to_text"


def test_vision_degradation_retry_removes_images():
    from nexus.core.loop import _request_without_feature

    original = ModelRequest(
        messages=[
            Message(
                "user",
                [Text("look"), Image("image/png", url="https://x/i.png")],
            )
        ]
    )
    adapted = _request_without_feature(original, "vision", _fallback_caps())
    blocks = adapted.messages[0].content
    assert all(not isinstance(block, Image) for block in blocks)
    assert any(
        isinstance(block, Text) and "image omitted" in block.text
        for block in blocks
    )


def test_documents_degradation_retry_replaces_documents():
    from nexus.core.loop import _request_without_feature

    original = ModelRequest(
        messages=[
            Message(
                "user",
                [
                    Document(
                        media_type="application/pdf",
                        data=b"%PDF",
                        title="spec",
                    )
                ],
            )
        ]
    )
    adapted = _request_without_feature(original, "documents", _fallback_caps())
    (block,) = adapted.messages[0].content
    assert isinstance(block, Text)
    assert "spec" in block.text


async def test_capabilities_from_model_info_registry():
    from nexus.model.registry import Cost, ModelInfo

    info = ModelInfo(
        provider="google",
        id="gemini-3-pro",
        name="Gemini 3 Pro",
        context=1_000_000,
        max_output=65536,
        tool_call=True,
        reasoning=True,
        structured_output=True,
        input_modalities=("text", "image", "pdf"),
        cost=Cost(input=1.25, output=10.0),
    )
    provider = GeminiProvider(
        api_key="k",
        capability_source=lambda model: info.capabilities(),
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    caps = provider.capabilities("gemini-3-pro")
    assert caps.tools is True
    assert caps.thinking is True
    assert caps.vision is True
    assert caps.documents is True
    assert caps.max_context_tokens == 1_000_000
    await provider.aclose()


async def test_count_tokens_returns_value():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"totalTokens": 42})

    provider = GeminiProvider(
        api_key="k",
        model="gemini-3-flash",
        http_transport=httpx.MockTransport(handler),
    )
    value = await provider.count_tokens(ModelRequest(messages=[]))
    assert value == 42
    assert captured["path"].endswith("/v1beta/models/gemini-3-flash:countTokens")
    assert "generateContentRequest" in captured["body"]
    await provider.aclose()


async def test_count_tokens_missing_or_invalid_is_none():
    provider = GeminiProvider(
        api_key="k",
        model="m",
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    assert await provider.count_tokens(ModelRequest(messages=[])) is None
    await provider.aclose()


async def test_count_tokens_http_error_is_typed_and_secret_free():
    secret = "AIzaSyLEAKME0123456789abcdefghijkl"
    provider = GeminiProvider(
        api_key="k",
        model="m",
        http_transport=httpx.MockTransport(
            lambda r: httpx.Response(403, json={"error": {"message": secret}})
        ),
    )
    with pytest.raises(ProviderError) as excinfo:
        await provider.count_tokens(ModelRequest(messages=[]))
    assert secret not in str(excinfo.value)
    await provider.aclose()


async def test_provider_closes_owned_client_idempotently():
    provider = GeminiProvider(
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
    provider = GeminiProvider(api_key="k", model="m", transport=transport)
    await provider.aclose()
    assert transport.closed is False
    assert client.is_closed is False
    await client.aclose()


async def test_closed_provider_rejects_stream():
    provider = GeminiProvider(
        api_key="k",
        model="m",
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    await provider.aclose()
    with pytest.raises(ProviderError):
        await _collect(provider, ModelRequest(messages=[]))


async def test_from_config_reads_google_and_gemini_sections():
    class _Section:
        api_key = "${env:FROM_CFG}"
        base_url = None

    class _V2:
        providers: ClassVar[dict] = {"google": _Section()}

    class _Config:
        v2 = _V2()
        model = "google/gemini-3-pro"

    server = _Server(httpx.Response(200, content=_fixture("text_stream.sse")))
    provider = GeminiProvider.from_config(
        _Config(),
        environ={"FROM_CFG": "cfg-key"},
        http_transport=httpx.MockTransport(server),
    )
    await _collect(provider, ModelRequest(messages=[]))
    assert server.requests[0].headers["x-goog-api-key"] == "cfg-key"
    sent = json.loads(server.requests[0].content)
    assert "model" not in sent
    assert server.requests[0].url.path.endswith(
        "/v1beta/models/gemini-3-pro:streamGenerateContent"
    )
    await provider.aclose()
