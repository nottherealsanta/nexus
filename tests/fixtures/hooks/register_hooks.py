"""A hook module declared through ``register()`` instead of ``HOOKS``."""

from __future__ import annotations

from typing import Any

from nexus.hooks.model import HookDecision, HookInvocation


async def _block(invocation: HookInvocation, ctx: Any) -> HookDecision:
    return HookDecision.block("blocked by the registered hook")


def register() -> list[dict[str, Any]]:
    return [
        {
            "event": "PreToolUse",
            "name": "registered_block",
            "matcher": "Bash(**)",
            "run": _block,
        }
    ]
