"""Provider-neutral message IR (plan section 3.1).

Everything above the model layer speaks these types. There is deliberately no
``system`` role: system instructions travel on ``ModelRequest.system`` so each
adapter can place them where its wire protocol expects.
"""
from __future__ import annotations

from typing import Any, Literal

import msgspec


class Text(msgspec.Struct, tag="text"):
    text: str


class Thinking(msgspec.Struct, tag="thinking"):
    text: str
    signature: str | None = None  # opaque provider blob, replayed verbatim


class Image(msgspec.Struct, tag="image"):
    media_type: str
    data: bytes | None = None
    url: str | None = None


class Document(msgspec.Struct, tag="document"):
    media_type: str
    data: bytes
    title: str | None = None


#: Harness-internal marker injected into the input of a **later** duplicate of a
#: tool-call id after the loop has re-identified it. The tools layer turns a
#: marked entry into a model-visible duplicate-id error, so rejecting the
#: duplicate never depends solely on manager-level id detection. It rides along
#: in persisted history and is harmless on the wire (it is not authoritative
#: input); it is not part of any tool's declared schema.
DUPLICATE_TOOL_CALL_KEY = "__nexus_duplicate_tool_call_id__"


class ToolUse(msgspec.Struct, tag="tool_use"):
    id: str
    name: str
    input: dict[str, Any]


class ToolResult(msgspec.Struct, tag="tool_result"):
    tool_use_id: str
    content: list[Text | Image]
    is_error: bool = False


ContentBlock = Text | Thinking | ToolUse | ToolResult | Image | Document


class MessageMeta(msgspec.Struct):
    """Harness-only metadata; never sent to a provider."""

    provider: str | None = None
    model: str | None = None
    usage: dict[str, Any] | None = None
    ts: float | None = None
    turn_id: str | None = None
    cache_hit: bool = False
    redacted: bool = False


class Message(msgspec.Struct):
    role: Literal["user", "assistant"]
    content: list[ContentBlock]
    meta: MessageMeta = msgspec.field(default_factory=MessageMeta)


__all__ = [
    "DUPLICATE_TOOL_CALL_KEY",
    "ContentBlock",
    "Document",
    "Image",
    "Message",
    "MessageMeta",
    "Text",
    "Thinking",
    "ToolResult",
    "ToolUse",
]
