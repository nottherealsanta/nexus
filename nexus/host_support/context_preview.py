"""Privacy projection for a read-only, next-turn context preview."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..ext.quarantine import sanitize_text
from ..util import redact_secrets

MAX_REQUEST_DETAILS_CHARS = 4_000_000


def safe_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return redact_secrets(sanitize_text(value, limit=limit)).strip()


def redact_value(value: Any) -> Any:
    """Redact string leaves without changing structured tool-schema shapes."""
    if isinstance(value, str):
        return redact_secrets(value[:16_384])
    if isinstance(value, Mapping):
        return {
            str(key)[:256]: redact_value(item)
            for key, item in list(value.items())[:2_000]
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value[:2_000]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(type(value).__name__)


def _bounded_value(value: Any, remaining: list[int]) -> Any:
    """Redact structured request details under one aggregate display cap."""
    if isinstance(value, str):
        kept = value[:max(0, min(len(value), remaining[0]))]
        remaining[0] -= len(kept)
        return redact_secrets(kept + ("…" if len(kept) < len(value) else ""))
    if isinstance(value, Mapping):
        result = {}
        for key, item in list(value.items())[:2_000]:
            if remaining[0] <= 0:
                result["_truncated"] = "additional context omitted"
                break
            safe_key = str(key)[:128]
            remaining[0] -= len(safe_key)
            result[safe_key] = _bounded_value(item, remaining)
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value[:2_000]:
            if remaining[0] <= 0:
                result.append("[additional context omitted]")
                break
            result.append(_bounded_value(item, remaining))
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


def project_context_preview(result: Mapping[str, Any]) -> dict[str, Any]:
    """Bound and redact the runtime preview before it crosses the facade."""
    data = dict(result)
    raw_system_text = data.get("system_text")
    data["system_text"] = (
        redact_secrets(raw_system_text[:1_000_000]) or None
        if isinstance(raw_system_text, str)
        else None
    )
    raw_parts = data.get("included_parts", ())
    data["included_parts"] = [
        {
            "name": safe_text(row.get("name"), 80),
            "text": redact_secrets(str(row.get("text") or "")[:1_000_000]),
        }
        for row in raw_parts[:32]
        if isinstance(row, Mapping)
    ]
    included_names = {
        row["name"] for row in data["included_parts"] if row["name"]
    }
    data["skills_index"] = [
        {
            "name": redact_secrets(str(row.get("name", ""))[:256]),
            "description": redact_secrets(str(row.get("description", ""))[:2_048]),
            "included": bool(row.get("included", False)),
        }
        for row in data.get("skills_index", ())[:512]
        if isinstance(row, Mapping)
    ]
    raw_mcp_index = data.get("mcp_index")
    data["mcp_index"] = (
        redact_secrets(raw_mcp_index[:4_000])
        if isinstance(raw_mcp_index, str)
        else ""
    )
    raw_system_files = data.get("system_files", {})
    data["system_files"] = {
        str(name): {
            "configured": bool(value.get("configured", False)),
            "loaded": bool(value.get("loaded", False)),
            "included": str(name) in included_names,
            "included_nonempty": (
                str(name) in included_names and bool(value.get("included_nonempty", False))
            ),
            "truncated": bool(value.get("truncated", False)),
            "source": safe_text(value.get("source"), 120) or None,
        }
        for name, value in raw_system_files.items()
        if isinstance(value, Mapping) and name in {"soul", "memory"}
    }
    remaining = [MAX_REQUEST_DETAILS_CHARS]
    data["tools"] = [
        _bounded_value(row, remaining)
        for row in data.get("tools", ())[:512]
    ] if isinstance(data.get("tools"), (tuple, list)) else []
    raw_messages = data.get("messages", ())
    data["messages"] = [
        {
            "role": safe_text(row.get("role"), 40) or "unknown",
            "blocks": [
                _bounded_value(block, remaining)
                for block in row.get("blocks", ())[:256]
                if isinstance(block, Mapping)
            ],
        }
        for row in raw_messages[:256]
        if isinstance(row, Mapping)
    ] if isinstance(raw_messages, (tuple, list)) else []
    if len(raw_messages) > 256 if isinstance(raw_messages, (tuple, list)) else False:
        data["omitted"] = [
            *[item for item in data.get("omitted", ()) if isinstance(item, str)],
            f"{len(raw_messages) - 256} earlier request messages omitted from display",
        ]
    request_context = data.get("request_context")
    if isinstance(request_context, Mapping):
        data["request_context"] = _bounded_value(request_context, remaining)
    else:
        data["request_context"] = {}
    params = data.get("params")
    data["params"] = _bounded_value(params, remaining) if isinstance(params, Mapping) else {}
    agent = data.get("agent")
    if isinstance(agent, Mapping):
        data["agent"] = redact_value(agent)
    budget = data.get("budget")
    if isinstance(budget, Mapping):
        data["budget"] = redact_value(budget)
    return data


__all__ = ["project_context_preview"]
