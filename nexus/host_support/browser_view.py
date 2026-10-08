"""Browser-safe reducer projection and compact structural JSON patches."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any

from ..view import BlockView, ConversationView, jsonable
from ..view.model import MAX_TEXT, message_text


def web_view(
    value: Any,
    old_value: Any = None,
    old_wire: Any = None,
) -> Any:
    """Encode reducer values, reusing serialized branches with structural sharing.

    Unchanged turn/message/agent objects reuse their prior wire objects, which
    avoids recursively serializing unchanged transcript branches on every token.
    The compatibility ``messages`` array is still flattened across history on
    each projection; eliminating that linear traversal needs a versioned shape
    change. Changed messages and their blocks are serialized again.
    """
    if value is old_value and old_wire is not None:
        return old_wire
    if is_dataclass(value) and not isinstance(value, type):
        can_reuse = (
            is_dataclass(old_value)
            and type(value) is type(old_value)
            and isinstance(old_wire, dict)
        )
        result = {}
        if isinstance(value, BlockView):
            text = message_text(value)
            return {
                item.name: (
                    getattr(value, item.name) if item.name == "image_url"
                    else text if item.name == "text" and text is not None
                    else web_view(getattr(value, item.name))
                )
                for item in fields(value)
            }
        for item in fields(value):
            if isinstance(value, ConversationView) and item.name in {
                "messages", "usage", "pending_permissions", "agents"
            }:
                continue
            if item.name == "targets" and getattr(value, item.name) is None:
                continue
            previous = getattr(old_value, item.name) if can_reuse else None
            result[item.name] = web_view(
                getattr(value, item.name),
                previous,
                old_wire.get(item.name) if can_reuse else None,
            )
        if isinstance(value, ConversationView):
            result["messages"] = []
            for serialized_turn in result.get("turns", ()):
                result["messages"].extend(serialized_turn.get("messages", ()))
            result["usage"] = web_view(value.usage)
            result["pending_permissions"] = [item.id for item in value.pending_permissions]
            old_agents_by_id = {
                item.get("id"): item
                for item in (old_wire.get("agents", ()) if can_reuse else ())
                if can_reuse and isinstance(item, dict) and item.get("id")
            }
            result["agents"] = []
            for agent_id in value.agent_order:
                agent = value.agents.get(agent_id)
                if agent is None:
                    continue
                previous_agent = old_value.agents.get(agent_id) if can_reuse else None
                result["agents"].append(
                    web_view(agent, previous_agent, old_agents_by_id.get(agent_id))
                )
        return result
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        can_reuse = isinstance(old_value, Mapping) and isinstance(old_wire, dict)
        if value is old_value and old_wire is not None:
            return old_wire
        return {
            str(key): web_view(
                item,
                old_value.get(key) if can_reuse else None,
                old_wire.get(str(key)) if can_reuse else None,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        if value is old_value and old_wire is not None:
            return old_wire
        can_reuse = isinstance(old_value, (list, tuple)) and isinstance(old_wire, list)
        return [
            web_view(
                item,
                old_value[index] if can_reuse and index < len(old_value) else None,
                old_wire[index] if can_reuse and index < len(old_wire) else None,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "\u2026"
    return jsonable(value)


def _pointer(path: str, part: str | int) -> str:
    escaped = str(part).replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"


def json_patch(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """Build ordered JSON Patch ops; growing strings use a tiny append op."""
    if before is after:
        return []
    if type(before) is not type(after):
        return [{"op": "replace", "path": path, "value": after}]
    if isinstance(before, dict):
        ops: list[dict[str, Any]] = []
        for key in sorted(before.keys() - after.keys()):
            ops.append({"op": "remove", "path": _pointer(path, key)})
        for key in sorted(after.keys() - before.keys()):
            ops.append({"op": "add", "path": _pointer(path, key), "value": after[key]})
        for key in sorted(before.keys() & after.keys()):
            ops.extend(json_patch(before[key], after[key], _pointer(path, key)))
        return ops
    if isinstance(before, list):
        ops = []
        for index in range(min(len(before), len(after))):
            ops.extend(json_patch(before[index], after[index], _pointer(path, index)))
        for index in range(len(before) - 1, len(after) - 1, -1):
            ops.append({"op": "remove", "path": _pointer(path, index)})
        for index in range(len(before), len(after)):
            ops.append({"op": "add", "path": _pointer(path, "-"), "value": after[index]})
        return ops
    if isinstance(before, str) and isinstance(after, str) and after.startswith(before):
        suffix = after[len(before):]
        return [] if not suffix else [{"op": "append", "path": path, "value": suffix}]
    if before == after:
        return []
    return [{"op": "replace", "path": path, "value": after}]


__all__ = ["json_patch", "web_view"]
