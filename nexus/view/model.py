"""Renderable view-model types for the pure event reducer (PLAN section 14.7).

Dataclasses only: no ``msgspec``, no I/O, and no import from any other Nexus
package. Every view object serializes with :meth:`_View.to_dict` to JSON-native
values, so a view snapshots, ships over the wire, and compares against a golden
file without a custom encoder. The reducer copies the branch it changes, so a
previous state is never mutated.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

__all__ = [
    "AgentView",
    "BlockView",
    "ContextView",
    "ConversationView",
    "DiagnosticView",
    "ErrorView",
    "ExtensionView",
    "HookView",
    "McpView",
    "MessageView",
    "PermissionTargetView",
    "PermissionView",
    "PresenceView",
    "QueuedInputView",
    "RegistryView",
    "RetryView",
    "SkillView",
    "ToolCallView",
    "TurnView",
    "UsageTotals",
    "jsonable",
]

#: Longest string kept verbatim in a snapshot; a hostile payload is clipped.
MAX_TEXT = 8192

def _clip(value: object) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\u2026"

def _dump(value: object) -> Any:
    """Deep-convert any view value to JSON-native types, never raising."""
    if isinstance(value, str):
        return _clip(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if is_dataclass(value) and not isinstance(value, type):
        result = {}
        for item in fields(value):
            field_value = getattr(value, item.name)
            if isinstance(value, BlockView) and item.name == "image_url":
                if field_value is not None:
                    result[item.name] = field_value[:12 * 1024 * 1024]
                continue
            if isinstance(value, BlockView) and item.name == "text" and field_value.startswith("\n\nAttachment:"):
                result[item.name] = field_value[:8 * 1024 * 1024]
                continue
            # UI reconciliation identifiers are reducer-local implementation
            # detail, preserving the established daemon projection wire shape.
            if item.name in {"id", "event_seq"} and isinstance(value, (MessageView, ToolCallView)):
                continue
            # Additive Phase 2 transcript/correlation fields stay absent when
            # reducing older events, preserving the established golden wire
            # snapshots while real values serialize normally.
            if item.name == "targets" and isinstance(value, PermissionView) and field_value is None:
                continue
            if item.name in {
                "input", "result", "display", "context_note", "metrics",
                "child_agent_ids",
                "parent_session", "parent_agent_id", "parent_call_id", "root_turn_id",
            } and field_value in (None, "", 0, {}, []):
                continue
            if item.name in {
                "user_ts", "assistant_ts", "elapsed_ms", "agent", "reasoning_effort",
            } and field_value is None:
                continue
            if item.name == "ts" and isinstance(value, MessageView):
                continue
            if (
                item.name in {"assistant_ts", "elapsed_ms"}
                and isinstance(value, TurnView)
                and value.user_ts is None
            ):
                continue
            if (
                item.name == "diff"
                and isinstance(value, ToolCallView)
                and field_value is None
            ):
                continue
            result[item.name] = _dump(field_value)
        return result
    if isinstance(value, Mapping):
        return {str(key): _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_dump(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _dump(to_dict())
        except Exception:  # noqa: BLE001 - a hostile payload never fails a view
            return _clip(value)
    return _clip(value)

def jsonable(value: object) -> Any:
    """Make an event payload JSON-safe (the reducer's diagnostic converter)."""
    return _dump(value)

class _View:
    """Mixin giving every view dataclass a JSON-safe snapshot."""

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)

@dataclass
class UsageTotals(_View):
    """Additive token accounting, shared by turns, agents, and the session."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def merge(self, other: UsageTotals) -> UsageTotals:
        return UsageTotals(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    @classmethod
    def _from(cls, data: object, keys: tuple[str, ...]) -> UsageTotals:
        if not isinstance(data, Mapping):
            return cls()
        values = []
        for key in keys:
            value = data.get(key, 0)
            values.append(value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0)
        return cls(*values)

    @classmethod
    def from_dict(cls, data: object) -> UsageTotals:
        return cls._from(data, ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"))

    @classmethod
    def from_stream(cls, data: object) -> UsageTotals:
        return cls._from(data, ("input", "output", "cache_read", "cache_write", "reasoning"))

@dataclass
class BlockView(_View):
    """One text/thinking block; ``streamed`` guards against a duplicate final."""

    image_url: str | None = None
    kind: str = "text"
    text: str = ""
    signature: str | None = None
    streamed: bool = False
    finalized: bool = False
    #: Thinking runs only: first-delta event time and the closed run's length,
    #: both derived from event timestamps so replay reproduces them.
    started_ts: float | None = None
    elapsed_ms: int | None = None

@dataclass
class MessageView(_View):
    """One message reconstructed from the event stream."""

    id: str = ""
    event_seq: int = 0
    role: str = "assistant"
    blocks: list[BlockView] = field(default_factory=list)
    iteration: int = 0
    provider: str | None = None
    model: str | None = None
    attempt: int = 0
    stop_reason: str | None = None
    done: bool = False
    #: Event timestamp used as the completed assistant message's durable bound.
    ts: float | None = None

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks if b.kind == "text")

    @property
    def thinking(self) -> str:
        return "".join(b.text for b in self.blocks if b.kind == "thinking")

    @property
    def thinking_ms(self) -> int | None:
        """Total closed thinking time, or None when no run recorded one."""
        runs = [b.elapsed_ms for b in self.blocks if b.kind == "thinking" and b.elapsed_ms is not None]
        return sum(runs) if runs else None

@dataclass
class ToolCallView(_View):
    """A tool invocation with live status, result metadata, and progress."""

    call_id: str = ""
    event_seq: int = 0
    #: Model iteration that requested the call; calls sharing it are one batch.
    iteration: int = 0
    name: str = ""
    target: str | None = None
    status: str = "requested"  # requested|running|completed|failed
    bundle: str | None = None
    is_error: bool = False
    executed: bool = False
    code: str | None = None
    error: str | None = None
    duration_ms: int | None = None
    progress: list[str] = field(default_factory=list)
    requested_ts: float | None = None
    started_ts: float | None = None
    finished_ts: float | None = None
    input: dict[str, Any] = field(default_factory=dict)
    result: list[dict[str, Any]] = field(default_factory=list)
    display: str | None = None
    context_note: str | None = None
    metrics: dict[str, Any] | None = None
    #: Bounded transcript-only preview emitted for a successful Edit.
    diff: dict[str, Any] | None = None
    #: Direct Task children, linked from each agent's durable parent_call_id.
    child_agent_ids: list[str] = field(default_factory=list)

@dataclass
class PermissionTargetView(_View):
    """One bounded, display-safe path detail for a permission request."""

    role: str
    path: str
    reason: str

@dataclass
class PermissionView(_View):
    """One approval request and, once resolved, the winning decision."""

    id: str = ""
    call_id: str | None = None
    tool: str | None = None
    key: str | None = None
    bundle: str | None = None
    preview: str | None = None
    suggestions: list[str] = field(default_factory=list)
    default_rule: str | None = None
    persistence_available: bool = True
    status: str = "pending"  # pending|resolved|cancelled
    decision: str | None = None
    scope: str | None = None
    grant: dict[str, Any] | None = None
    ts: float | None = None
    targets: list[PermissionTargetView] | None = None

@dataclass
class ContextView(_View):
    """One context lifecycle event (assembled / compacted / degraded)."""

    kind: str = "assembled"
    iteration: int = 0
    data: dict[str, Any] = field(default_factory=dict)

@dataclass
class SkillView(_View):
    """One skill invocation and its completion."""

    skill: str = ""
    resource: str | None = None
    status: str = "invoked"  # invoked|completed
    ok: bool | None = None
    error: str | None = None
    scoped_tools: list[str] = field(default_factory=list)
    narrowed: list[str] = field(default_factory=list)
    unknown_bundles: list[str] = field(default_factory=list)

@dataclass
class HookView(_View):
    """One fired or blocked hook decision."""

    event: str = ""
    hook: str | None = None
    kind: str | None = None
    action: str | None = None
    reason: str | None = None
    status: str = "fired"  # fired|blocked

@dataclass
class RetryView(_View):
    """A provider-fallback retry (``model.retrying``)."""

    iteration: int = 0
    attempt: int = 0
    from_provider: str | None = None
    from_model: str | None = None
    provider: str | None = None
    model: str | None = None
    reason: str | None = None
    event_seq: int = 0
    delay_seconds: int = 0

@dataclass
class TurnView(_View):
    """One turn: messages, tools, approvals, context, retries, and usage."""

    id: str = ""
    index: int = 0
    phase: str = "active"  # active|completed|failed|cancelled
    stop_reason: str | None = None
    error: str | None = None
    iteration: int = 0
    limits: dict[str, Any] = field(default_factory=dict)
    messages: list[MessageView] = field(default_factory=list)
    tools: list[ToolCallView] = field(default_factory=list)
    permission_ids: list[str] = field(default_factory=list)
    context: list[ContextView] = field(default_factory=list)
    skills: list[SkillView] = field(default_factory=list)
    hooks: list[HookView] = field(default_factory=list)
    retries: list[RetryView] = field(default_factory=list)
    usage: UsageTotals = field(default_factory=UsageTotals)
    started_ts: float | None = None
    updated_ts: float | None = None
    #: Frozen root-agent metadata carried by this turn's ``turn.started``.
    agent: dict[str, Any] | None = None
    #: Effort on the actual model request, captured from ``model.started``.
    reasoning_effort: str | None = None
    #: Durable input/assistant event timestamps used for the summary duration.
    user_ts: float | None = None
    assistant_ts: float | None = None
    elapsed_ms: int | None = None
    #: ``agent`` for a model turn; ``shell`` for a composer ``!`` run (one
    #: user message and one ``bash`` row, no model call, no usage).
    kind: str = "agent"

    @property
    def terminal(self) -> bool:
        return self.phase in ("completed", "failed", "cancelled")

@dataclass
class PresenceView(_View):
    viewers: int = 0
    attended: bool = False

@dataclass
class QueuedInputView(_View):
    queued_id: str = ""
    mode: str = "queue"
    content: list[Any] = field(default_factory=list)
    depth: int = 0

@dataclass
class ExtensionView(_View):
    name: str = ""
    status: str = "loaded"  # loaded|unloaded|failed|shadowed
    source: str | None = None
    sha256: str | None = None
    generation: int | None = None
    origin: str | None = None
    kind: str | None = None
    error: str | None = None
    error_type: str | None = None
    trigger: str | None = None
    summary: dict[str, Any] | None = None
    diff: dict[str, Any] | None = None
    previous_generation: int | None = None

@dataclass
class McpView(_View):
    server: str = ""
    status: str = "connected"  # connected|disconnected|failed
    version: str | None = None
    tool_count: int | None = None
    tools: list[str] = field(default_factory=list)
    cached: bool | None = None
    generation: int | None = None
    reason: str | None = None
    error: str | None = None
    error_type: str | None = None
    attempts: int | None = None
    health: str | None = None
    retry_in_s: float | None = None

@dataclass
class RegistryView(_View):
    status: str = "refreshed"  # refreshed|stale|failed|mismatch
    source: str | None = None
    stale: bool | None = None
    models: int | None = None
    generation: int | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

@dataclass
class ErrorView(_View):
    type: str = ""
    message: str = ""
    seq: int = 0
    ts: float | None = None
    data: dict[str, Any] = field(default_factory=dict)

@dataclass
class DiagnosticView(_View):
    """A retained event the reducer does not specifically interpret."""

    type: str = ""
    seq: int = 0
    ts: float | None = None
    data: dict[str, Any] = field(default_factory=dict)
    event_id: str | None = None

@dataclass
class AgentView(_View):
    """One subagent and the nested conversation its relayed events build."""

    id: str = ""
    parent: str | None = None
    type: str | None = None
    task: str | None = None
    session: str | None = None
    depth: int = 0
    tier: str | None = None
    requested_tier: str | None = None
    index: int | None = None
    description: str = ""
    model: str | None = None
    tools: list[str] = field(default_factory=list)
    dropped_tools: list[str] = field(default_factory=list)
    clamped: bool = False
    status: str = "spawned"  # spawned|completed|clamped
    ok: bool | None = None
    is_error: bool | None = None
    error: str | None = None
    iterations: int = 0
    stop_reason: str | None = None
    usage: UsageTotals = field(default_factory=UsageTotals)
    max_tier: str | None = None
    diagnostics: list[str] = field(default_factory=list)
    body: ConversationView = field(default_factory=lambda: ConversationView())
    spawned_ts: float | None = None
    completed_ts: float | None = None
    parent_session: str | None = None
    parent_agent_id: str | None = None
    parent_call_id: str | None = None
    root_turn_id: str | None = None

@dataclass
class ConversationView(_View):
    """The full renderable state of one session, folded from its event log."""

    session_id: str | None = None
    last_seq: int = 0
    phase: str = "idle"  # idle|running|awaiting_input|awaiting_permission|closed
    opened: bool = False
    closed: bool = False
    turns: list[TurnView] = field(default_factory=list)
    permissions: list[PermissionView] = field(default_factory=list)
    presence: PresenceView = field(default_factory=PresenceView)
    input_queue: list[QueuedInputView] = field(default_factory=list)
    agents: dict[str, AgentView] = field(default_factory=dict)
    agent_order: list[str] = field(default_factory=list)
    extensions: dict[str, ExtensionView] = field(default_factory=dict)
    mcp: dict[str, McpView] = field(default_factory=dict)
    context: dict[str, Any] | None = None
    model: dict[str, Any] | None = None
    registry: RegistryView | None = None
    errors: list[ErrorView] = field(default_factory=list)
    hooks: list[HookView] = field(default_factory=list)
    diagnostics: list[DiagnosticView] = field(default_factory=list)

    @property
    def messages(self) -> list[MessageView]:
        return [message for turn in self.turns for message in turn.messages]

    @property
    def tools(self) -> list[ToolCallView]:
        return [tool for turn in self.turns for tool in turn.tools]

    @property
    def usage(self) -> UsageTotals:
        total = UsageTotals()
        for turn in self.turns:
            total = total.merge(turn.usage)
        return total

    @property
    def pending_permissions(self) -> list[PermissionView]:
        return [p for p in self.permissions if p.status == "pending"]

    @property
    def active_turn(self) -> TurnView | None:
        for turn in reversed(self.turns):
            if not turn.terminal:
                return turn
        return None

    @property
    def root_agents(self) -> list[AgentView]:
        """Agents spawned directly by the root session (parent is the session)."""
        return [
            self.agents[aid]
            for aid in self.agent_order
            if aid in self.agents
            and (self.agents[aid].parent is None or self.agents[aid].parent == self.session_id)
        ]

    def children_of(self, agent_id: str) -> list[AgentView]:
        direct = [
            self.agents[aid]
            for aid in self.agent_order
            if aid in self.agents and self.agents[aid].parent == agent_id
        ]
        if direct:
            return direct
        parent = self.agents.get(agent_id)
        if parent is not None:
            return [parent.body.agents[aid] for aid in parent.body.agent_order if aid in parent.body.agents]
        for candidate in self.agents.values():
            nested_parent = candidate.body.agents.get(agent_id)
            if nested_parent is not None:
                return [
                    nested_parent.body.agents[aid]
                    for aid in nested_parent.body.agent_order
                    if aid in nested_parent.body.agents
                ]
            for nested in candidate.body.agents.values():
                if nested.id == agent_id:
                    return [
                        nested.body.agents[aid]
                        for aid in nested.body.agent_order
                        if aid in nested.body.agents
                    ]
        return [
            self.agents[aid]
            for aid in self.agent_order
            if aid in self.agents and self.agents[aid].parent == agent_id
        ]

    def to_dict(self) -> dict[str, Any]:
        payload = _dump(self)
        payload["messages"] = [_dump(message) for message in self.messages]
        payload["usage"] = _dump(self.usage)
        payload["pending_permissions"] = [p.id for p in self.pending_permissions]
        # Derived, UI-facing fields overlaid on the dataclass snapshot.
        serialized_agents = []
        for aid in self.agent_order:
            agent = self.agents.get(aid)
            if agent is None:
                continue
            serialized = _dump(agent)
            serialized["body"] = _dump(agent.body)
            serialized_agents.append(serialized)
        payload["agents"] = serialized_agents
        return payload
