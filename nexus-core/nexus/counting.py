"""Context budget adapted from old Nexus nexus/context/counting.py and budget.py.

Provider usage is the exact size of the newest measured request, not a claim
that subsequently appended context has already been tokenized by the provider.
"""
from __future__ import annotations

import math
from typing import Any

from .jsonenc import dumps

IMAGE_TOKENS = 1_000  # a flat cost per inline image; its base64 characters are not tokens


def _without_images(value: Any, *, native_images: bool = False) -> Any:
    """`value` with each `data:` URL replaced by a short stand-in, so the estimate prices an image, not its bytes."""
    if isinstance(value, str):
        return "x" * (IMAGE_TOKENS * 4) if value.startswith("data:") else value
    if isinstance(value, dict):
        if native_images and value.get("type") == "image" and isinstance(value.get("source"), dict) and value["source"].get("type") == "base64":
            return "x" * (IMAGE_TOKENS * 4)
        return {k: _without_images(v, native_images=native_images) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_without_images(v, native_images=native_images) for v in value]
    return value


def estimate_tokens(value: Any) -> int:
    """The character-based estimate `budget` falls back to, for one part of a request."""
    native = isinstance(value, dict) and _direct_body(value)
    text = value if isinstance(value, str) else dumps(_without_images(value, native_images=native)).decode()
    return math.ceil(len(text) / 4)


def _direct_body(body: dict[str, Any]) -> bool:
    return (isinstance(body.get("model"), str) and body["model"].startswith("claude-")
            and isinstance(body.get("messages"), list) and "max_tokens" in body and "stream" in body
            and (isinstance(body.get("system"), list)
                 or any("input_schema" in t for t in body.get("tools", []) if isinstance(t, dict))
                 or any(isinstance(m.get("content"), list) and any(isinstance(b, dict) and b.get("type") in {
                     "image", "tool_use", "tool_result", "thinking", "redacted_thinking"} for b in m["content"])
                        for m in body["messages"] if isinstance(m, dict))))


def _context_body(body: dict[str, Any]) -> dict[str, Any]:
    """The request with its reasoning-effort options removed.

    Effort steers the model; it is not part of the context the model reads, so
    switching it must not move the estimate.
    """
    body = dict(body)
    body.pop("reasoning_effort", None)
    body.pop("effort", None)
    if _direct_body(body):
        body.pop("thinking", None)
        body.pop("output_config", None)
        body["max_tokens"] = 0  # keep dialect detection while excluding the generation cap
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        reasoning = {key: value for key, value in reasoning.items() if key != "effort"}
        if reasoning:
            body["reasoning"] = reasoning
        else:
            body.pop("reasoning")
    return body


def budget(body: dict[str, Any], window: int | None, input_tokens: int | None = None) -> dict[str, Any]:
    """Prefer measured request input; label the character-based fallback."""
    used = input_tokens if input_tokens is not None else estimate_tokens(_context_body(body))
    return {"used": used, "window": window, "exact": input_tokens is not None}
