import json

import httpx
import pytest

from nexus.llmhub.anthropic import AnthropicProvider
from nexus.llmhub.anthropic_policy import image_limit
from nexus.llmhub.types import Image, Media, Message, ProviderError, Request, Text, ToolResult


class Auth:
    async def headers(self):
        raise AssertionError("invalid requests must fail before resolving credentials")


@pytest.mark.parametrize("model,limit", [("claude-haiku-4-5", 100), ("claude-opus-4-5-20251101", 100),
    ("claude-sonnet-4-6", 600), ("claude-opus-4.7", 600), ("claude-fable-5-1", 600), ("unknown", 100)])
def test_model_image_limits(model, limit):
    assert image_limit(model) == limit


@pytest.mark.asyncio
async def test_user_and_tool_images_share_limit_and_are_not_dropped():
    image = Image("data:image/png;base64,AA==")
    media = Media("image", "image/png", data_url=image.data_url)
    messages = (Message("user", (image,) * 100), Message("tool", (ToolResult("call", "", media=(media,)),)))
    provider = AnthropicProvider(Auth())
    with pytest.raises(ProviderError, match="exceeds 100 images; no context was removed"):
        _ = [event async for event in provider.stream(Request("claude-haiku-4-5", "", messages))]
    assert len(messages[0].parts) == 100 and messages[1].parts[0].media == (media,)
    body = provider.body(Request("claude-sonnet-4-6", "", messages))
    assert len(body["messages"][0]["content"]) == 101


@pytest.mark.asyncio
async def test_request_size_uses_exact_utf8_wire_encoding(monkeypatch):
    provider = AnthropicProvider(Auth(), cache=False)
    req = Request("claude-haiku-4-5", "", (Message("user", (Text("こんにちは"),)),))
    size = len(json.dumps(provider.body(req), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
    monkeypatch.setattr("nexus.llmhub.anthropic.REQUEST_BYTE_LIMIT", size)
    assert provider.body(req)["messages"][0]["content"][0]["text"] == "こんにちは"
    monkeypatch.setattr("nexus.llmhub.anthropic.REQUEST_BYTE_LIMIT", size - 1)
    with pytest.raises(ProviderError, match="exceeds 128 MB; no context was removed"):
        _ = [event async for event in provider.stream(req)]


@pytest.mark.parametrize("version", ["2023-06-01", "2026-10-21"])
@pytest.mark.asyncio
async def test_explicit_api_version_is_sent_without_sampling_or_long_context_beta(version):
    class Headers:
        async def headers(self):
            return {}

    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(400)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(Headers(), client=client, api_version=version)
        with pytest.raises(ProviderError, match="HTTP 400"):
            _ = [event async for event in provider.stream(Request("claude-sonnet-5", "", ()))]
    assert calls[0].headers["anthropic-version"] == version
    assert not {"temperature", "top_p", "top_k"}.intersection(json.loads(calls[0].content))
    assert "context-1m-2025-08-07" not in calls[0].headers["anthropic-beta"]


def test_unknown_api_version_fails_closed():
    with pytest.raises(ProviderError, match="unsupported API version"):
        AnthropicProvider(Auth(), api_version="future")
