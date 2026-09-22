"""Phase 3 Anthropic token counting and prompt-cache boundary translation.

No network: every test drives the adapter through ``httpx.MockTransport``.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from nexus.errors import ProviderError
from nexus.model.message import Message, Text, ToolResult
from nexus.model.providers.anthropic import (
    AnthropicProvider,
    build_count_tokens_body,
    build_request_body,
)
from nexus.model.request import ModelRequest, SamplingParams, ToolSchema
from nexus.model.stream import Usage

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"


def _request(*, tools=(), cache=None, system="SYS", messages=None):
    metadata = {"cache": cache} if cache is not None else {}
    return ModelRequest(
        messages=messages
        or [Message(role="user", content=[Text(text="hello")])],
        system=system,
        tools=list(tools),
        model="claude-test",
        provider="anthropic",
        metadata=metadata,
    )


def _cache_metadata(*boundaries):
    return {
        "enabled": True,
        "boundaries": [{"position": p, "scope": s} for p, s in boundaries],
    }


def _provider(handler, **kwargs):
    kwargs.setdefault("api_key", "test-key")
    return AnthropicProvider(
        model="claude-test",
        http_transport=httpx.MockTransport(handler),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------


async def test_count_tokens_posts_semantic_body_without_stream_or_cache():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"input_tokens": 321})

    tool = ToolSchema(name="Read", description="d", input_schema={"type": "object"})
    provider = _provider(handler)
    req = _request(tools=[tool], cache=_cache_metadata((0, "system_tools")))
    value = await provider.count_tokens(req)
    await provider.aclose()

    assert value == 321
    assert seen["path"] == "/v1/messages/count_tokens"
    assert seen["key"] == "test-key"
    assert "stream" not in seen["body"]
    assert "cache_control" not in json.dumps(seen["body"])
    assert seen["body"]["tools"][0]["name"] == "Read"


async def test_count_tokens_missing_input_tokens_is_none():
    provider = _provider(lambda r: httpx.Response(200, json={}))
    assert await provider.count_tokens(_request()) is None
    await provider.aclose()


async def test_count_tokens_non_integer_is_none():
    provider = _provider(lambda r: httpx.Response(200, json={"input_tokens": "x"}))
    assert await provider.count_tokens(_request()) is None
    await provider.aclose()


async def test_count_tokens_malformed_json_raises_provider_error_without_secret():
    provider = _provider(lambda r: httpx.Response(200, content=b"{not json"))
    with pytest.raises(ProviderError):
        await provider.count_tokens(_request())
    await provider.aclose()


async def test_count_tokens_http_error_is_typed_and_secret_free():
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "bad"}})

    provider = _provider(handler, api_key="sk-ant-SUPER-SECRET")
    with pytest.raises(ProviderError) as excinfo:
        await provider.count_tokens(_request())
    assert "SUPER-SECRET" not in str(excinfo.value)
    await provider.aclose()


def test_count_body_has_only_count_endpoint_fields():
    tool = ToolSchema(name="Read", description="d", input_schema={"type": "object"})
    req = _request(
        tools=[tool],
        cache=_cache_metadata((0, "system_tools")),
    )
    req = ModelRequest(
        messages=req.messages,
        system=req.system,
        tools=req.tools,
        model=req.model,
        provider=req.provider,
        params=SamplingParams(
            temperature=0.7,
            max_output_tokens=4096,
            top_p=0.9,
            stop_sequences=["STOP"],
            thinking_budget=None,
        ),
        metadata=req.metadata,
    )
    counted = build_count_tokens_body(req, model="m")
    assert set(counted) == {"model", "messages", "system", "tools"}
    for generation_only in (
        "max_tokens",
        "temperature",
        "top_p",
        "stop_sequences",
        "stream",
    ):
        assert generation_only not in counted
    assert "cache_control" not in json.dumps(counted)


def test_count_body_includes_thinking_when_enabled():
    req = ModelRequest(
        messages=[Message(role="user", content=[Text(text="hi")])],
        model="m",
        params=SamplingParams(thinking_budget=2048),
    )
    counted = build_count_tokens_body(req, model="m")
    assert counted["thinking"] == {"type": "enabled", "budget_tokens": 2048}


# ---------------------------------------------------------------------------
# cache_control placement
# ---------------------------------------------------------------------------


def test_cache_control_on_last_tool_after_stable_prefix():
    tool_a = ToolSchema(name="A", description="a", input_schema={"type": "object"})
    tool_b = ToolSchema(name="B", description="b", input_schema={"type": "object"})
    req = _request(tools=[tool_a, tool_b], cache=_cache_metadata((0, "system_tools")))
    body = build_request_body(req, model="m")
    assert "cache_control" not in body["system"]
    assert body["tools"][0].get("cache_control") is None
    assert body["tools"][1]["cache_control"] == {"type": "ephemeral"}


def test_cache_control_on_system_when_no_tools():
    req = _request(cache=_cache_metadata((0, "system_tools")))
    body = build_request_body(req, model="m")
    assert body["system"] == [
        {"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}
    ]


def test_cache_control_at_history_boundary_after_coalescing():
    messages = [
        Message(role="user", content=[Text(text="m0")]),
        Message(role="assistant", content=[Text(text="m1")]),
        Message(role="user", content=[Text(text="m2")]),
        Message(role="assistant", content=[Text(text="m3")]),
        Message(role="user", content=[Text(text="current")]),
    ]
    req = _request(
        messages=messages,
        cache=_cache_metadata((0, "system_tools"), (4, "history")),
    )
    body = build_request_body(req, model="m")
    # The 4th original message (index 3) is the boundary; its wire block carries
    # the marker, and nothing after it does.
    assert body["messages"][3]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in json.dumps(body["messages"][4])


def test_no_cache_markers_for_unsupported_capabilities():
    req = _request(tools=[ToolSchema(name="A", description="a", input_schema={})])
    body = build_request_body(req, model="m")
    assert "cache_control" not in json.dumps(body)
    assert body["system"] == "SYS"


def test_identical_requests_produce_identical_bodies():
    tool = ToolSchema(name="Read", description="d", input_schema={"type": "object"})
    cache = _cache_metadata((0, "system_tools"), (1, "history"))
    messages = [
        Message(role="user", content=[Text(text="a")]),
        Message(
            role="user",
            content=[
                ToolResult(tool_use_id="c", content=[Text(text="r")], is_error=False)
            ],
        ),
    ]
    first = json.dumps(build_request_body(_request(tools=[tool], cache=cache, messages=messages), model="m"), sort_keys=True)
    second = json.dumps(build_request_body(_request(tools=[tool], cache=cache, messages=messages), model="m"), sort_keys=True)
    assert first == second


# ---------------------------------------------------------------------------
# Cache usage mapping: first request writes, second reads (controlled fixtures)
# ---------------------------------------------------------------------------
#
# Recorded before/after numbers from the fixtures below (no live costs claimed):
#   first request  -> cache_write=5, cache_read=0
#   second request -> cache_read=5,  cache_write=0
# Both requests are byte-identical and carry the same ``cache_control`` markers,
# which is what makes the second a cache read.


async def _usage_for(provider, request):
    usage = None
    async for event in provider.stream(request):
        if isinstance(event, Usage):
            usage = event
    return usage


async def test_prompt_cache_fixture_shows_write_then_read():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        name = "cache_write.sse" if len(seen) == 1 else "cache_read.sse"
        return httpx.Response(200, content=(FIXTURES / name).read_bytes())

    provider = _provider(handler)
    req = _request(
        tools=[ToolSchema(name="Read", description="d", input_schema={})],
        cache=_cache_metadata((0, "system_tools")),
    )

    first = await _usage_for(provider, req)
    second = await _usage_for(provider, req)
    await provider.aclose()

    assert first.cache_write == 5 and first.cache_read == 0
    assert second.cache_read == 5 and second.cache_write == 0
    # The cache breakpoint marker is present on both identical requests.
    assert "cache_control" in json.dumps(seen[0])
    assert seen[0] == seen[1]

