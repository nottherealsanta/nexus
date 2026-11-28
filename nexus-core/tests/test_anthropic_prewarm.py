import json

import httpx
import pytest

from nexus.llmhub.anthropic import AnthropicProvider, DirectRequest
from nexus.llmhub.types import Done, Message, ProviderError, Text, Usage, WireEvent, WireRequest, WireResponse


class Auth:
    async def headers(self):
        return {"authorization": "Bearer test"}


def request(**kwargs):
    return DirectRequest("claude-haiku-4-5", "frozen system", (Message("user", (Text("hello"),)),), **kwargs)


def response(**kwargs):
    return {"type": "message", "id": "msg_warm", "content": [], "stop_reason": "max_tokens",
            "usage": {"input_tokens": 2, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4,
                      "output_tokens": 0}, **kwargs}


def test_prewarm_preserves_body_except_output_and_stream_flags():
    provider = AnthropicProvider(Auth())
    normal = provider.body(request())
    warm = provider.body(request(prewarm=True))
    assert warm == {**normal, "max_tokens": 0, "stream": False}


@pytest.mark.parametrize("kwargs,match", [({"reasoning_effort": "low"}, "budget thinking"),
    ({"cache_retention": "none"}, "requires a cache breakpoint")])
def test_prewarm_incompatible_options_fail_before_send(kwargs, match):
    with pytest.raises(ProviderError, match=match):
        AnthropicProvider(Auth()).body(request(prewarm=True, **kwargs))


@pytest.mark.asyncio
async def test_prewarm_is_one_visible_request_with_accounted_usage():
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=response())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = [event async for event in AnthropicProvider(Auth(), client=client).stream(request(prewarm=True))]
    assert len(calls) == 1 and calls[0].headers["accept"] == "application/json"
    assert json.loads(calls[0].content)["max_tokens"] == 0
    assert [type(e) for e in events] == [WireRequest, WireResponse, WireEvent, Usage, Done]
    assert events[-2:] == [Usage(9, 3, 0), Done("max_tokens", "msg_warm")]
    assert events[2].data == response()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [response(content=[{"type": "text", "text": "unexpected"}]),
    response(stop_reason="end_turn"), response(usage={"output_tokens": 1}),
    response(usage={"output_tokens": 0, "input_tokens": -1}), response(usage={"output_tokens": False}),
    response(usage=None), [], {"type": "message"}])
async def test_prewarm_never_silently_discards_content_or_bad_usage(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))) as client:
        with pytest.raises(ProviderError, match="prewarm"):
            _ = [e async for e in AnthropicProvider(Auth(), client=client).stream(request(prewarm=True))]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["invalid", "x" * 65537])
async def test_prewarm_response_is_bounded_and_validated(text):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=text))) as client:
        with pytest.raises(ProviderError, match="prewarm"):
            _ = [e async for e in AnthropicProvider(Auth(), client=client).stream(request(prewarm=True))]
