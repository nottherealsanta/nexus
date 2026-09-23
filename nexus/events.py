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
    "model.selected",
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
EXT_EVENTS = (
    "ext.loaded",
    "ext.unloaded",
    "ext.failed",
    "ext.tool_shadowed",
    "ext.manifest_changed",
)
MCP_EVENTS = ("mcp.connected", "mcp.disconnected", "mcp.failed", "mcp.tools_changed")
SKILL_EVENTS = ("skill.invoked", "skill.completed")
AGENT_EVENTS = ("agent.spawned", "agent.completed", "agent.clamped")
HOOK_EVENTS = ("hook.fired", "hook.blocked")

# Plan section 15.10: the model-registry lifecycle. ``registry.refreshed``/
# ``registry.stale``/``registry.failed`` describe acquisition; ``registry.mismatch``
# records a provider rejection that contradicts a registry capability claim
# (section 15.5). They are folded into ``misc`` so the earlier catalogue groups
# stay exact and mutually disjoint (the surface-amendment tests pin that list).
REGISTRY_EVENTS = (
    "registry.refreshed",
    "registry.stale",
    "registry.failed",
    "registry.mismatch",
)
MISC_EVENTS = ("provider.raw", "error", *REGISTRY_EVENTS)

# Phase 3.5 session surface, as amended by PLAN section 14.10. The input queue
# persists a submission, the loop consumes it at the next turn boundary, and a
# submission that can never run is dropped; each transition is a drawable event
# so no UI polls the queue.
INPUT_EVENTS = ("input.queued", "input.consumed", "input.dropped")

# Presence is a subscriber count, not identity (single user, many views).
# Section 14.10 adds only the join/leave transitions; the derived
# ``presence.changed`` event is retained as a compatibility alias below so
# existing emitters and status bars keep working.
PRESENCE_EVENTS = ("presence.joined", "presence.left")

# Phase 8's daemon lifecycle and handshake, per PLAN section 14.10. Emitted by
# ``host/daemon.py``/``host/protocol.py`` once they exist; listed here so the
# wire catalogue is fixed before any surface depends on the names. The earlier
# ``daemon.stopped``/``daemon.client_*`` names are transitional compat aliases
# below, not part of this group.
DAEMON_EVENTS = (
    "daemon.started",
    "daemon.stopping",
    "daemon.session_scheduled",
    "daemon.session_queued",
)

# Transitional names still emitted by the pre-Phase-1 path: the agent emits
# ``started``/``completed``, the Codex adapter emits ``message`` and
# ``provider``, and ``error`` is emitted by the CLI's JSON error handler. They
# are documented here so the exported catalogue does not reject live events.
LEGACY_EVENTS = ("started", "message", "provider", "completed", "error")

# Surface-amendment compatibility aliases. ``presence.changed`` is still emitted
# alongside join/leave, and the pre-§14.10 daemon names remain documented
# transitional aliases so a subscriber built against the earlier catalogue keeps
# classifying them. They live outside ``EVENT_GROUPS`` so the planned groups stay
# exact, internally unique, and mutually disjoint.
COMPAT_EVENTS = (
    "presence.changed",
    "daemon.stopped",
    "daemon.client_connected",
    "daemon.client_disconnected",
)

#: Catalogue groups by name, in the order the plan lists them. This is the
#: single source of truth for ``EVENT_TYPES`` and lets a view reducer classify an
#: event without re-listing names. Membership is disjoint; legacy and compat
#: names are not groups because ``error`` already lives in ``misc``.
EVENT_GROUPS: dict[str, tuple[str, ...]] = {
    "session": SESSION_EVENTS,
    "turn": TURN_EVENTS,
    "context": CONTEXT_EVENTS,
    "model": MODEL_EVENTS,
    "tool": TOOL_EVENTS,
    "permission": PERMISSION_EVENTS,
    "ext": EXT_EVENTS,
    "mcp": MCP_EVENTS,
    "skill": SKILL_EVENTS,
    "agent": AGENT_EVENTS,
    "hook": HOOK_EVENTS,
    "input": INPUT_EVENTS,
    "presence": PRESENCE_EVENTS,
    "daemon": DAEMON_EVENTS,
    "misc": MISC_EVENTS,
}

EVENT_TYPES = frozenset(
    name for group in EVENT_GROUPS.values() for name in group
).union(LEGACY_EVENTS, COMPAT_EVENTS)

__all__ = [
    "AGENT_EVENTS",
    "COMPAT_EVENTS",
    "CONTEXT_EVENTS",
    "DAEMON_EVENTS",
    "EVENT_GROUPS",
    "EVENT_TYPES",
    "EXT_EVENTS",
    "HOOK_EVENTS",
    "INPUT_EVENTS",
    "LEGACY_EVENTS",
    "MCP_EVENTS",
    "MISC_EVENTS",
    "MODEL_EVENTS",
    "PERMISSION_EVENTS",
    "PRESENCE_EVENTS",
    "REGISTRY_EVENTS",
    "SESSION_EVENTS",
    "SKILL_EVENTS",
    "TOOL_EVENTS",
    "TURN_EVENTS",
    "Event",
]
