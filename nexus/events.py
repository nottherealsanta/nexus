"""The public UI boundary: JSON-serializable events, no rendering or input.

Phase 0 widens the original ``Event(type, data)`` dataclass into the plan's
envelope. All added fields carry safe defaults, so existing call sites such as
``Event("message", {"text": ...})`` keep working unchanged.

Rule kept from the original design: every state change a UI could want to draw
is an event; no UI polls a manager.
"""
from __future__ import annotations

import time
from typing import Any

import msgspec

from .util import new_id


class Event(msgspec.Struct, frozen=True):
    """One item in the session event stream (plan section 3.5)."""

    type: str
    data: dict[str, Any] = msgspec.field(default_factory=dict)
    seq: int = 0  # monotonic per session; 0 until SessionManager exists
    ts: float = msgspec.field(default_factory=time.time)
    session: str | None = None
    turn: str | None = None
    id: str = msgspec.field(default_factory=new_id)

    def to_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)


# Grouped catalogue from the plan. UIs must tolerate unknown types; these
# constants exist so emitters and tests do not scatter string literals.
SESSION_EVENTS = ("session.opened", "session.closed")
TURN_EVENTS = (
    "turn.started",
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
)
CONTEXT_EVENTS = ("context.assembled", "context.compacted", "context.degraded")
MODEL_EVENTS = (
    "model.started",
    "text.delta",
    "text",
    "thinking.delta",
    "thinking",
    "model.usage",
    "model.stopped",
    "model.retrying",
)
TOOL_EVENTS = (
    "tool.requested",
    "tool.started",
    "tool.progress",
    "tool.completed",
    "tool.failed",
)
PERMISSION_EVENTS = ("permission.requested", "permission.resolved")
EXT_EVENTS = ("ext.loaded", "ext.unloaded", "ext.failed", "ext.manifest_changed")
MCP_EVENTS = ("mcp.connected", "mcp.disconnected", "mcp.failed", "mcp.tools_changed")
SKILL_EVENTS = ("skill.invoked", "skill.completed")
AGENT_EVENTS = ("agent.spawned", "agent.completed")
HOOK_EVENTS = ("hook.fired", "hook.blocked")
MISC_EVENTS = ("provider.raw", "error")

# Transitional names still emitted by the pre-Phase-1 path: the agent emits
# ``started``/``completed``, the Codex adapter emits ``message`` and
# ``provider``, and ``error`` is emitted by the CLI's JSON error handler. They
# are documented here so the exported catalogue does not reject live events.
LEGACY_EVENTS = ("started", "message", "provider", "completed", "error")

EVENT_TYPES = frozenset(
    SESSION_EVENTS
    + TURN_EVENTS
    + CONTEXT_EVENTS
    + MODEL_EVENTS
    + TOOL_EVENTS
    + PERMISSION_EVENTS
    + EXT_EVENTS
    + MCP_EVENTS
    + SKILL_EVENTS
    + AGENT_EVENTS
    + HOOK_EVENTS
    + MISC_EVENTS
    + LEGACY_EVENTS
)

__all__ = ["Event", "EVENT_TYPES"]
