"""Allowlisted, bounded projection of owned-worktree records and diffs."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..util import redact_secrets
from .context_preview import safe_text


def review_hex(value: object, length: int) -> str:
    """Pass a lowercase hex review identity through unchanged; redaction would turn a
    64-character digest into ``***`` and make every review unacknowledgeable."""
    if isinstance(value, str) and len(value) == length and all(char in "0123456789abcdef" for char in value):
        return value
    return safe_text(value, length)


def worktree_record(record: Any) -> dict[str, Any]:
    """Project descriptive fields only; filesystem and ref internals stay private."""
    text_fields = (
        "child_id", "lifecycle", "final_status", "review_status", "reviewer",
        "created_at", "finalized_at", "reviewed_at", "acknowledged_at",
    )
    values = {
        field: safe_text(getattr(record, field, None), 256) or None
        for field in text_fields
    }
    values.update({
        "dirty": bool(getattr(record, "dirty", False)),
        "review_id": review_hex(getattr(record, "current_review_id", None), 32) or None,
        "digest": review_hex(getattr(record, "current_review_digest", None), 64) or None,
        "acknowledged": bool(getattr(record, "acknowledged_review_id", None)),
    })
    return values


def worktree_review_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Project manifest rows without blob identifiers or unbounded paths."""
    result: dict[str, Any] = {
        "path": safe_text(entry.get("path"), 1024),
        "change": safe_text(entry.get("change"), 32),
        "binary": bool(entry.get("binary", False)),
    }
    for field in ("old_mode", "new_mode"):
        result[field] = safe_text(entry.get(field), 16) or None
    for field in ("old_sha256", "new_sha256"):
        value = entry.get(field)
        result[field] = (
            value if isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value) else None
        )
    return result


def worktree_diff_row(row: Mapping[str, Any], limit: int) -> dict[str, Any]:
    projected = worktree_review_entry(row)
    patch = row.get("patch")
    projected["patch"] = (
        worktree_patch(patch, min(limit, 128 * 1024))
        if isinstance(patch, str)
        else ""
    )
    return projected


def worktree_patch(value: str, limit: int) -> str:
    """Redact patch lines individually while preserving unified-diff structure."""
    remaining = max(0, limit)
    output: list[str] = []
    for line in value.splitlines(keepends=True):
        ending = "\n" if line.endswith("\n") else ""
        body = line[:-1] if ending else line
        clean = "".join(
            " " if ord(char) < 32 and char != "\t" or ord(char) == 127 else char
            for char in body
        )
        safe = redact_secrets(clean)
        if len(safe) > remaining:
            safe = safe[:remaining]
        output.append(safe + ending)
        remaining -= len(safe) + len(ending)
        if remaining <= 0:
            break
    return "".join(output)


__all__ = ["worktree_diff_row", "worktree_record", "worktree_review_entry"]
