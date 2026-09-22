"""In-process hook fixtures declared with ``HOOKS``.

The manager discovers this file as ``.nexus/hooks/*.py``: a module defines
``HOOKS`` (or a synchronous ``register()``) and each declaration names an event
and carries a callable ``run(invocation, ctx)``.
"""

from __future__ import annotations

from typing import Any

from nexus.hooks.model import HookDecision, HookInvocation


def _mark_reviewed(invocation: HookInvocation, ctx: Any) -> HookDecision:
    payload = dict(invocation.tool_input)
    payload["reviewed"] = True
    return HookDecision.modify(payload, reason="marked reviewed")


def _warn_on_write(invocation: HookInvocation, ctx: Any) -> HookDecision:
    return HookDecision.warn(f"observed {invocation.tool} on {invocation.key}")


async def _allow_async(invocation: HookInvocation, ctx: Any) -> HookDecision:
    return HookDecision.allow()


HOOKS = [
    {
        "event": "PreToolUse",
        "name": "mark_reviewed",
        "matcher": "Write(**)",
        "run": _mark_reviewed,
    },
    {
        "event": "PostToolUse",
        "name": "warn_observer",
        "run": _warn_on_write,
    },
    {
        "event": "SessionStart",
        "name": "async_allow",
        "run": _allow_async,
    },
]
