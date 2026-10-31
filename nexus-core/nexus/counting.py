"""Context budget adapted from old Nexus nexus/context/counting.py and budget.py.

Provider usage is the exact size of the newest measured request, not a claim
that subsequently appended context has already been tokenized by the provider.
"""
from __future__ import annotations

import json
import math
from typing import Any

IMAGE_TOKENS = 1_000  # a flat cost per inline image; its base64 characters are not tokens


def _without_images(value: Any) -> Any:
    """`value` with each `data:` URL replaced by a short stand-in, so the estimate prices an image, not its bytes."""
    if isinstance(value, str):
        return "x" * (IMAGE_TOKENS * 4) if value.startswith("data:") else value
    if isinstance(value, dict):
        return {k: _without_images(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_without_images(v) for v in value]
    return value


def estimate_tokens(value: Any) -> int:
    """The character-based estimate `budget` falls back to, for one part of a request."""
    text = value if isinstance(value, str) else json.dumps(_without_images(value), ensure_ascii=False, separators=(",", ":"))
    return math.ceil(len(text) / 4)


def budget(body: dict[str, Any], window: int | None, input_tokens: int | None = None) -> dict[str, Any]:
    """Prefer measured request input; label the character-based fallback."""
    used = input_tokens if input_tokens is not None else estimate_tokens(body)
    return {"used": used, "window": window, "exact": input_tokens is not None}
