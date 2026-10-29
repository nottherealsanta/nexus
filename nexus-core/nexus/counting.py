"""Context budget adapted from old Nexus nexus/context/counting.py and budget.py.

Provider usage is the exact size of the newest measured request, not a claim
that subsequently appended context has already been tokenized by the provider.
"""
from __future__ import annotations

import json
import math
from typing import Any


def estimate_tokens(value: Any) -> int:
    """The character-based estimate `budget` falls back to, for one part of a request."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return math.ceil(len(text) / 4)


def budget(body: dict[str, Any], window: int | None, input_tokens: int | None = None) -> dict[str, Any]:
    """Prefer measured request input; label the character-based fallback."""
    used = input_tokens if input_tokens is not None else math.ceil(
        len(json.dumps(body, ensure_ascii=False, separators=(",", ":"))) / 4
    )
    return {"used": used, "window": window, "exact": input_tokens is not None}
