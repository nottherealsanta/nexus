"""Classify durable context notices independently of client dismissal (plan 13).

Explicit metadata wins; the fallback preserves older model-visible records.
"""
from collections.abc import Mapping
from typing import Any

MODEL_VISIBLE_KINDS = frozenset({
    "tool_call_nudge", "subagent_roster", "subagent_reports", "external_inbox", "worktree_move",
})


def model_visible(data: Mapping[str, Any]) -> bool:
    explicit = data.get("model_visible")
    return explicit if isinstance(explicit, bool) else data.get("kind") in MODEL_VISIBLE_KINDS
