"""A hook returning an unsupported object; the run must isolate it."""

from __future__ import annotations

from typing import Any

from nexus.hooks.model import HookInvocation


def _bad(invocation: HookInvocation, ctx: Any) -> object:
    return object()


HOOKS = [{"event": "PreToolUse", "name": "bad_return", "run": _bad}]
