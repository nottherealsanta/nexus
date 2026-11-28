import gzip
import json

import httpx
import pytest

from nexus.llmhub.anthropic import AnthropicProvider, DirectRequest
from nexus.llmhub.types import Message, ProviderError, Request, Text, WireRequest


class Auth:
    async def headers(self):
        return {}


@pytest.mark.parametrize("encoding", [None, "gzip"])
@pytest.mark.parametrize("prewarm", [False, True])
@pytest.mark.asyncio
async def test_compressed_transport_preserves_visible_body_and_does_not_retry(encoding, prewarm):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(400, text="request_body_encoding_invalid")
    request = DirectRequest("claude-haiku-4-5", "frozen system", (Message("user", (Text("こんにちは"),)),), prewarm=prewarm)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(Auth(), client=client, request_encoding=encoding)
        stream = provider.stream(request)
        wire = await anext(stream)
        assert isinstance(wire, WireRequest)
        with pytest.raises(ProviderError, match="HTTP 400"):
            _ = [e async for e in stream]
    assert len(calls) == 1
    raw = gzip.decompress(calls[0].content) if encoding else calls[0].content
    assert json.loads(raw) == wire.body
    assert (calls[0].headers.get("content-encoding") == "gzip") == (encoding == "gzip")
    assert (wire.headers.get("content-encoding") == "gzip") == (encoding == "gzip")
    assert "こんにちは" in raw.decode()


@pytest.mark.parametrize("encoding", ["br", "zstd", "deflate"])
def test_other_request_encodings_fail_closed(encoding):
    with pytest.raises(ProviderError, match="request encoding"):
        AnthropicProvider(Auth(), request_encoding=encoding)


@pytest.mark.asyncio
async def test_compression_cannot_bypass_uncompressed_size_limit(monkeypatch):
    monkeypatch.setattr("nexus.llmhub.anthropic.REQUEST_BYTE_LIMIT", 200)
    provider = AnthropicProvider(Auth(), request_encoding="gzip")
    req = Request("claude-haiku-4-5", "repeated" * 100, ())
    with pytest.raises(ProviderError, match="exceeds 128 MB"):
        _ = [e async for e in provider.stream(req)]
