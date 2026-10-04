"""Focused unit tests for the OpenAI adapter's Responses edge cases.

The conformance suite covers the happy paths; these pin two behaviours that a
recorded fixture does not exercise:

* a Responses gateway that omits ``call_id`` on the ``output_item.done`` item
  must still finalize the *same* accumulator entry the added item started, not
  mint a second dangling call;
* reasoning is not replayed on the next request (the documented degradation).
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from nexus.model.message import (
    Message,
    MessageMeta,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)
from nexus.model.providers.openai import OpenAIProvider, build_request_body
from nexus.model.request import ModelRequest
from nexus.model.stream import MessageStop, ThinkingDelta, ThinkingEnd, ToolCallEnd


def _sse(*frames: tuple[str, dict[str, Any]]) -> bytes:
    parts: list[str] = []
    for event_type, payload in frames:
        parts.append(f"event: {event_type}\n")
        parts.append(f"data: {json.dumps(payload)}\n\n")
    return "".join(parts).encode("utf-8")


def _provider(handler: Any) -> OpenAIProvider:
    return OpenAIProvider(
        api_key="k",
        model="gpt-5",
        api="responses",
        base_url="https://api.openai.com/v1",
        environ={},
        http_transport=httpx.MockTransport(handler),
    )


async def test_responses_done_without_call_id_finalizes_one_call():
    # ``added`` carries the real call id; ``done`` omits it and only has the
    # item id. The adapter must map item id -> call id and finish exactly once.
    body = _sse(
        (
            "response.created",
            {
                "type": "response.created",
                "response": {"id": "r1", "model": "gpt-5", "status": "in_progress"},
            },
        ),
        (
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "Read",
                    "arguments": "",
                },
            },
        ),
        (
            "response.function_call_arguments.delta",
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_1",
                "delta": '{"path": "a.txt"}',
            },
        ),
        (
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "name": "Read",
                    "arguments": '{"path": "a.txt"}',
                },
            },
        ),
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {"id": "r1", "status": "completed", "usage": {}},
            },
        ),
    )

    provider = _provider(lambda request: httpx.Response(200, content=body))
    try:
        events = [
            event
            async for event in provider.stream(
                ModelRequest(messages=[Message("user", [Text("hi")])])
            )
        ]
    finally:
        await provider.aclose()

    ends = [event for event in events if isinstance(event, ToolCallEnd)]
    assert len(ends) == 1, ends
    assert ends[0].id == "call_1"
    assert ends[0].input == {"path": "a.txt"}
    assert isinstance(events[-1], MessageStop)


async def test_responses_reasoning_is_not_replayed():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=_sse(
                (
                    "response.created",
                    {
                        "type": "response.created",
                        "response": {
                            "id": "r1",
                            "model": "gpt-5",
                            "status": "in_progress",
                        },
                    },
                ),
                (
                    "response.output_text.delta",
                    {"type": "response.output_text.delta", "delta": "ok"},
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "r1",
                            "status": "completed",
                            "usage": {},
                        },
                    },
                ),
            ),
        )

    provider = _provider(handler)
    request = ModelRequest(
        messages=[
            Message(
                "assistant",
                [Thinking("secret reasoning", signature="sig-native"), Text("visible")],
                meta=MessageMeta(provider="openai"),
            ),
            Message("user", [Text("next")]),
        ]
    )
    try:
        [event async for event in provider.stream(request)]
    finally:
        await provider.aclose()

    dumped = json.dumps(captured["body"])
    # The limitation is explicit: a Responses reasoning signature is captured on
    # decode but never emitted as an input item, so it is dropped on replay.
    assert "secret reasoning" not in dumped
    assert "sig-native" not in dumped
    assert "visible" in dumped


def test_responses_input_preserves_mixed_block_order():
    # A single assistant message interleaves text, a tool call, then more text.
    # The Responses input is an ordered item list, so the replayed order must
    # match: text run, function_call, text run, function_call_output.
    request = ModelRequest(
        messages=[
            Message(
                "assistant",
                [
                    Text("before"),
                    ToolUse("c1", "Read", {"path": "a"}),
                    Text("after"),
                ],
            ),
            Message("user", [ToolResult("c1", [Text("out")])]),
        ]
    )
    body = build_request_body(request, model="gpt-5", api="responses")
    assert body["input"] == [
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "before"}],
        },
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "Read",
            "arguments": '{"path":"a"}',
        },
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "after"}],
        },
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": "out",
        },
    ]


async def test_endpoint_fallback_retries_once_on_the_other_dialect_and_remembers_it():
    from nexus.model.providers.openai import EndpointFallback

    urls: list[str] = []
    done = _sse(
        ("response.created", {"type": "response.created", "response": {"id": "r", "model": "m", "status": "in_progress"}}),
        ("response.completed", {"type": "response.completed", "response": {"id": "r", "status": "completed"}}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(400, json={"error": {"message": 'model "m" is not accessible via the /chat/completions endpoint'}})
        return httpx.Response(200, content=done, headers={"content-type": "text/event-stream"})

    provider = OpenAIProvider(
        api_key="k", model="m", api="chat", base_url="https://copilot.example/v1", environ={},
        http_transport=httpx.MockTransport(handler), api_selector=EndpointFallback("chat"),
    )
    req = ModelRequest(model="m", messages=[Message(role="user", content=[Text(text="hi")])])
    async for _ in provider.stream(req):
        pass
    assert urls == ["/v1/chat/completions", "/v1/responses"]
    urls.clear()
    async for _ in provider.stream(req):
        pass
    assert urls == ["/v1/responses"]  # learned; no second rejected call
    await provider.aclose()


async def test_opencode_go_unknown_protocol_retries_on_the_other_dialect():
    """OpenCode Go rejects a model with a generic message; retry the other endpoint."""
    from nexus.model.providers.openai import EndpointFallback

    urls: list[str] = []
    responses_body = _sse(
        ("response.created", {"type": "response.created", "response": {"id": "r", "model": "gpt-6-luna", "status": "in_progress"}}),
        ("response.completed", {"type": "response.completed", "response": {"id": "r", "status": "completed"}}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(400, json={"error": {"message": "Model does not support this protocol."}})
        return httpx.Response(200, content=responses_body, headers={"content-type": "text/event-stream"})

    provider = OpenAIProvider(
        api_key="k", model="gpt-6-luna", api="chat", base_url="https://opencode.ai/zen/go/v1", environ={},
        http_transport=httpx.MockTransport(handler), api_selector=EndpointFallback("chat"),
    )
    req = ModelRequest(model="gpt-6-luna", messages=[Message(role="user", content=[Text(text="hi")])])
    async for _ in provider.stream(req):
        pass
    assert urls == ["/zen/go/v1/chat/completions", "/zen/go/v1/responses"]
    urls.clear()
    async for _ in provider.stream(req):
        pass
    assert urls == ["/zen/go/v1/responses"]  # learned; no rejected call again
    await provider.aclose()


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra", "gpt-5.6-luna", "gpt-5.6-sol"])
async def test_copilot_responses_only_models_request_thinking_summaries(model):
    from nexus.model.providers.openai import EndpointFallback
    from nexus.model.capabilities import Capabilities

    requests = []
    def handle(request):
        requests.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/chat/completions":
            return httpx.Response(400, json={"error": {"message": f'model "{model}" is not accessible via the /chat/completions endpoint'}})
        return httpx.Response(200, content=
            b'data: {"type":"response.created","response":{"id":"r"}}\n\n'
            b'data: {"type":"response.reasoning_summary_text.delta","delta":"**Checking candidates**"}\n\n'
            b'data: {"type":"response.output_item.done","item":{"type":"reasoning"}}\n\n'
            b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n')
    provider = OpenAIProvider(
        api_key="test", base_url="https://api.githubcopilot.com", api="chat",
        api_selector=EndpointFallback(), model=model, capabilities=Capabilities(thinking=True),
        http_transport=httpx.MockTransport(handle),
    )
    try:
        events = [event async for event in provider.stream(ModelRequest(messages=[Message("user", [Text("check")])]))]
        assert [path for path, _ in requests] == ["/chat/completions", "/responses"]
        assert requests[-1][1]["reasoning"] == {"summary": "auto"}
        assert any(isinstance(event, ThinkingDelta) for event in events)
        assert any(isinstance(event, ThinkingEnd) for event in events)
    finally:
        await provider.aclose()


async def test_mixed_chat_reasoning_and_answer_keeps_thought_before_text():
    body = b'data: {"choices":[{"delta":{"content":"answer","reasoning_content":"check"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    provider = OpenAIProvider(api_key="test", model="m", api="chat",
                              http_transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    try:
        events = [event async for event in provider.stream(ModelRequest(messages=[]))]
        from nexus.model.stream import TextDelta
        assert [type(event) for event in events if isinstance(event, (ThinkingDelta, TextDelta))] == [ThinkingDelta, TextDelta]
    finally:
        await provider.aclose()


@pytest.mark.parametrize("error_type,code", [("service_unavailable_error", "server_is_overloaded"), ("server_error", "overloaded")])
async def test_explicit_stream_overload_is_retryable(error_type, code):
    from nexus.errors import ProviderOverloaded
    body = _sse(("error", {"type": "error", "error": {"type": error_type, "code": code, "message": "Try again later"}}))
    provider = _provider(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body))
    with pytest.raises(ProviderOverloaded):
        async for _ in provider.stream(ModelRequest(messages=[Message(role="user", content=[Text(text="hi")])])):
            pass


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [("https://opencode.ai/zen/go/v1", "sess-1"), ("https://api.example.com/v1", None)],
)
async def test_opencode_session_header_only_for_opencode_hosts(base_url, expected):
    seen: list[str | None] = []
    done = _sse(
        ("response.created", {"type": "response.created", "response": {"id": "r", "model": "m", "status": "in_progress"}}),
        ("response.completed", {"type": "response.completed", "response": {"id": "r", "status": "completed"}}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-opencode-session"))
        return httpx.Response(200, content=done, headers={"content-type": "text/event-stream"})

    provider = OpenAIProvider(
        api_key="k", model="m", api="responses", base_url=base_url, environ={},
        http_transport=httpx.MockTransport(handler),
    )
    req = ModelRequest(
        model="m", messages=[Message(role="user", content=[Text(text="hi")])],
        metadata={"session_id": "sess-1"},
    )
    async for _ in provider.stream(req):
        pass
    assert seen == [expected]
    await provider.aclose()
