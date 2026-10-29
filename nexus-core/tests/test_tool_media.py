"""Images in tool results reach the model, per dialect (PLAN K8; docs/llm-providers.md "Tool-result media")."""
from __future__ import annotations

import base64
import json

from test_server import TOKEN, Scripted  # noqa: F401
from test_server import setup as setup  # noqa: PLC0414

from nexus.codec import message_from_json, message_to_json
from nexus.llmhub.chat import chat_messages
from nexus.llmhub.responses import ResponsesQuirks, input_items
from nexus.llmhub.types import Media, Message, Request, ToolCall, ToolResult, has_images

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 40
URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


def _conversation(media):
    return (Message("assistant", (ToolCall("c1", "mcp_call", "{}"), ToolCall("c2", "mcp_call", "{}"))),
            Message("tool", (ToolResult("c1", "seen", media=media), ToolResult("c2", "plain"))))


def test_responses_puts_the_images_in_the_function_call_output():
    items = input_items(_conversation((Media("art_1", "image/png", "shot.image", URL),)), origin="x", quirks=ResponsesQuirks())
    outputs = {i["call_id"]: i["output"] for i in items if i["type"] == "function_call_output"}
    assert outputs["c1"] == [{"type": "input_text", "text": "seen"}, {"type": "input_image", "image_url": URL}]
    assert outputs["c2"] == "plain"  # a text-only result is still a plain string


def test_chat_sends_the_images_as_one_user_message_after_every_tool_message():
    wire = chat_messages(Request("m", "", _conversation((Media("art_1", "image/png", "shot.image", URL),))), origin="x")
    assert [m["role"] for m in wire[-3:]] == ["tool", "tool", "user"]
    user = wire[-1]["content"]
    assert user[0]["text"] == "[Image returned by tool call c1: shot.image]" and user[1] == {"type": "image_url", "image_url": {"url": URL}}


def test_an_image_without_bytes_is_not_sent_and_the_json_never_holds_bytes():
    media = (Media("art_1", "image/png", "s", None),)
    items = input_items(_conversation(media), origin="x", quirks=ResponsesQuirks())
    assert next(i for i in items if i.get("call_id") == "c1" and i["type"] == "function_call_output")["output"] == "seen"
    assert not has_images(_conversation(media))
    assert has_images(_conversation((Media("a", "image/png", "s", URL),)))
    stored = message_to_json(_conversation((Media("art_1", "image/png", "s", URL),))[1])
    assert "base64" not in json.dumps(stored) and stored["parts"][0]["media"] == [{"artifact": "art_1", "mime": "image/png", "name": "s"}]
    assert message_from_json(stored).parts[0].media == (Media("art_1", "image/png", "s"),)
