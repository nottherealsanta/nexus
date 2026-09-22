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
from nexus.model.stream import MessageStop, ToolCallEnd


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
