"""Stable logical-to-stored child ids (never infer ownership from a slug)."""
from __future__ import annotations

import hashlib
import re


def stored_child_id(logical: str) -> str:
    """Return the legacy-compatible, valid 80-character stored id."""
    digest = hashlib.sha1(logical.encode("utf-8")).hexdigest()[:10]
    base = re.sub(r"[^A-Za-z0-9_-]", "_", logical) or "subagent"
    return f"{base[:80 - len(digest) - 1]}_{digest}"
