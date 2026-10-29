"""JSON encoding for messages and events: storage and the HTTP API use it.

Parts and messages round-trip exactly (`message_from_json(message_to_json(m)) == m`),
including each part's `raw` wire item. Events encode one way, as
`{"type": "<snake_name>", ...fields}`.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any

from .llmhub.types import Image, Message, Part, Reasoning, Text, ToolCall, ToolResult

_PARTS: dict[str, type] = {"image": Image, "text": Text, "reasoning": Reasoning, "tool_call": ToolCall, "tool_result": ToolResult}
_NAMES = {cls: name for name, cls in _PARTS.items()}


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def to_json(value: Any) -> Any:
    """Dataclasses become dicts with a `type` tag; everything else passes through."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        name = _NAMES.get(type(value)) or snake(type(value).__name__)
        return {"type": name, **{f.name: to_json(getattr(value, f.name)) for f in dataclasses.fields(value)}}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    return value


def part_from_json(data: dict[str, Any]) -> Part:
    fields = {k: v for k, v in data.items() if k != "type"}
    return _PARTS[data["type"]](**fields)


def message_to_json(message: Message) -> dict[str, Any]:
    return {"role": message.role, "parts": [to_json(p) for p in message.parts]}


def message_from_json(data: dict[str, Any]) -> Message:
    return Message(data["role"], tuple(part_from_json(p) for p in data["parts"]))
