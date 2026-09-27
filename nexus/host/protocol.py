"""Transport-neutral wire contract for the host facade (PLAN sections 14.4, 14.9).

Every surface speaks the same commands and receives the same results. The
structs are frozen ``msgspec`` records, so one protocol serves the local Unix
socket, the HTTP/SSE surface, and an in-process test with identical semantics:
a command is JSON-encoded, decoded by kind, dispatched through
:class:`~nexus.host.facade.HostFacade`, and the result is encoded the same way.

The protocol deliberately carries no credential, environment, or configuration
value: the verb list is exactly the PLAN §14.4 surface plus host queries and
owned-worktree review/mutation commands. Streaming is the one verb a
request/response pair cannot model, so ``SessionSubscribe`` names the stream and
the transport attaches through :meth:`HostFacade.subscribe`.
"""
from __future__ import annotations

from typing import Any, Literal

import msgspec

from ..session.manager import SessionSummary

#: Bumped when a command/result shape changes incompatibly, so a transport can
#: refuse a peer built from a different protocol revision instead of misreading.
PROTOCOL_VERSION = 3


# ---------------------------------------------------------------------------
# Commands (tagged by struct name; ``type`` is the discriminator)
# ---------------------------------------------------------------------------


class SessionList(msgspec.Struct, tag=True, frozen=True):
    """List every session as a transport-neutral summary."""


class SessionOpen(msgspec.Struct, tag=True, frozen=True):
    session: str
    create: bool = True
    recover: bool = True


class SessionStart(msgspec.Struct, tag=True, frozen=True):
    session: str
    content: str = ""
    blocks: list[dict[str, Any]] = msgspec.field(default_factory=list)


class SessionEnqueue(msgspec.Struct, tag=True, frozen=True):
    session: str
    content: str = ""
    blocks: list[dict[str, Any]] = msgspec.field(default_factory=list)


class SessionCancel(msgspec.Struct, tag=True, frozen=True):
    session: str
    reason: str = ""
    drop_queue: bool = True


class SessionSubscribe(msgspec.Struct, tag=True, frozen=True):
    session: str
    from_seq: int = 0
    follow: bool = True


class SessionState(msgspec.Struct, tag=True, frozen=True):
    session: str
    from_seq: int = 0


class LogsRead(msgspec.Struct, tag=True, frozen=True):
    """Read bounded, redacted daemon and session lifecycle diagnostics."""

    session: str | None = None
    daemon_cursor: str | None = None
    session_cursor: int | None = None
    limit: int = 50


class AgentTranscript(msgspec.Struct, tag=True, frozen=True):
    """Request the reducer-backed transcript for one child agent."""

    session: str
    agent_id: str


class SessionFork(msgspec.Struct, tag=True, frozen=True):
    session: str
    at_seq: int | None = None
    new_id: str | None = None


class SessionDelete(msgspec.Struct, tag=True, frozen=True):
    session: str
    force: bool = False
    reason: str = ""


class SessionRestore(msgspec.Struct, tag=True, frozen=True):
    trash_id: str


class SessionExport(msgspec.Struct, tag=True, frozen=True):
    session: str
    format: str = "json"


class PermissionResolve(msgspec.Struct, tag=True, frozen=True):
    session: str
    request_id: str
    decision: str
    client_id: str | None = None


class ExtensionsReload(msgspec.Struct, tag=True, frozen=True):
    trigger: str = "api"


class ExtensionsList(msgspec.Struct, tag=True, frozen=True):
    """A sanitized view of the live external modules."""


class ExtensionsValidate(msgspec.Struct, tag=True, frozen=True):
    """Validate candidate extension files without swapping the manifest.

    ``target`` names one workspace-relative file (or a path) to check; ``None``
    validates every candidate in the configured hot directories.
    """

    target: str | None = None


class ExtensionsTrash(msgspec.Struct, tag=True, frozen=True):
    """Move one managed extension file to trash, then rebuild the manifest.

    ``target`` is a workspace-relative or absolute path that must resolve to a
    discoverable candidate under ``[ext].dirs``; the facade refuses anything
    else. ``force`` keeps the trashed file even if the rebuild aborts.
    """

    target: str
    reason: str = ""
    force: bool = False


class ModelsRefresh(msgspec.Struct, tag=True, frozen=True):
    """Force a catalogue acquisition and report the resulting status."""


class ModelsList(msgspec.Struct, tag=True, frozen=True):
    """List model metadata.

    With ``selectable_only=True``, each row additionally carries
    ``supported_efforts: list[str]``. This is the exact set of explicit effort
    values the runtime can apply to that candidate through its resolved provider
    route; an unsupported or unresolvable route reports ``[]``. The field is
    absent on non-selectable listings, preserving their existing row shape.
    The existing catalogue ``reasoning_efforts`` field remains descriptive and
    is not a substitute for this route-aware list.

    These choices do not identify a candidate's default effort. Before model
    selection, treat its default effort as unset: the row reports choices, not a
    default. Selecting a model creates an explicit session model selection, so a
    selected root-agent ``reasoning_effort`` default does not apply to that
    session route. An explicit session effort override does apply when supported;
    an unsupported stored override is dormant rather than replaced by the agent
    default. The active route's effective value is reported by ``AgentCurrent``.
    Listing candidates is read-only and does not select a model or effort.
    """

    provider: str | None = None
    tier: str | None = None
    selectable_only: bool = False
    search: str | None = None


class ModelShow(msgspec.Struct, tag=True, frozen=True):
    """Resolve one model reference to its descriptive registry row."""

    ref: str


class ModelTiers(msgspec.Struct, tag=True, frozen=True):
    """The tier table: ordering, default, curated map, and user overrides."""


class ModelSelect(msgspec.Struct, tag=True, frozen=True):
    """Select and persist one session's model for its subsequent turns.

    ``ref`` is a tier name, ``"provider/model"``, or a bare id; it is validated
    daemon-side (a bad reference is an ``ErrorResult``). The choice is durable in
    the session log and never disturbs a turn already running.
    """

    session: str
    ref: str


class ReasoningEffortSelect(msgspec.Struct, tag=True, frozen=True):
    """Select a root-session reasoning effort for subsequent turns."""

    session: str
    effort: str | None


class AgentsList(msgspec.Struct, tag=True, frozen=True):
    """The discovered subagent definitions (sanitized index rows)."""


class AgentCurrent(msgspec.Struct, tag=True, frozen=True):
    session: str


class AgentSelect(msgspec.Struct, tag=True, frozen=True):
    session: str
    name: str


class AgentReset(msgspec.Struct, tag=True, frozen=True):
    session: str


class ToolsList(msgspec.Struct, tag=True, frozen=True):
    """The model-facing tool catalog for the current config and manifest."""


class ContextInspect(msgspec.Struct, tag=True, frozen=True):
    """Preview one session's next-turn standing context without starting a turn."""

    session: str


class FileSearch(msgspec.Struct, tag=True, frozen=True):
    """Search workspace-relative file paths for composer completion."""

    query: str
    limit: int = 30


class WorktreeList(msgspec.Struct, tag=True, frozen=True):
    """List daemon-owned agent worktrees using sanitized metadata only."""


class WorktreeInspect(msgspec.Struct, tag=True, frozen=True):
    """Inspect one daemon-owned child worktree by its authenticated child id."""

    child_id: str


class WorktreeReview(msgspec.Struct, tag=True, frozen=True):
    """Read a bounded page from one finalized child worktree review."""

    child_id: str
    review_id: str | None = None
    cursor: int = 0
    limit: int = 1


class WorktreeAcknowledge(msgspec.Struct, tag=True, frozen=True):
    """Acknowledge the exact current finalized child review."""

    child_id: str
    review_id: str
    digest: str


class WorktreeIntegrate(msgspec.Struct, tag=True, frozen=True):
    """Preview or confirm integration of the exact acknowledged review."""

    child_id: str
    review_id: str
    digest: str
    confirmation_token: str = ""


class WorktreeDiscard(msgspec.Struct, tag=True, frozen=True):
    """Preview or confirm removal of one authenticated child worktree."""

    child_id: str
    force: bool = False
    review_id: str | None = None
    confirmation_token: str = ""


class Doctor(msgspec.Struct, tag=True, frozen=True):
    """A redacted health report: config, providers, registry, extensions, MCP.

    ``explain_reload`` adds the hot-vs-restart boundary (PLAN section 6.6).
    """

    explain_reload: bool = False


class Health(msgspec.Struct, tag=True, frozen=True):
    """Daemon-level liveness and scheduling counters."""


class Shutdown(msgspec.Struct, tag=True, frozen=True):
    reason: str = ""


class WebLaunch(msgspec.Struct, tag=True, frozen=True):
    """Ask the local daemon to start its browser surface and issue a ticket.

    This command is intercepted by the authenticated Unix-socket transport and
    is intentionally unavailable through the HTTP peer API.
    """


#: The complete command union. ``msgspec`` decodes it by the ``type`` tag.
Command = (
    SessionList
    | SessionOpen
    | SessionStart
    | SessionEnqueue
    | SessionCancel
    | SessionSubscribe
    | SessionState
    | LogsRead
    | AgentTranscript
    | SessionFork
    | SessionDelete
    | SessionRestore
    | SessionExport
    | PermissionResolve
    | ExtensionsReload
    | ExtensionsList
    | ExtensionsValidate
    | ExtensionsTrash
    | ModelsRefresh
    | ModelsList
    | ModelShow
    | ModelTiers
    | ModelSelect
    | ReasoningEffortSelect
    | AgentsList
    | AgentCurrent
    | AgentSelect
    | AgentReset
    | ToolsList
    | ContextInspect
    | FileSearch
    | WorktreeList
    | WorktreeInspect
    | WorktreeReview
    | WorktreeAcknowledge
    | WorktreeIntegrate
    | WorktreeDiscard
    | Doctor
    | Health
    | WebLaunch
    | Shutdown
)

COMMANDS: tuple[type, ...] = (
    SessionList,
    SessionOpen,
    SessionStart,
    SessionEnqueue,
    SessionCancel,
    SessionSubscribe,
    SessionState,
    LogsRead,
    AgentTranscript,
    SessionFork,
    SessionDelete,
    SessionRestore,
    SessionExport,
    PermissionResolve,
    ExtensionsReload,
    ExtensionsList,
    ExtensionsValidate,
    ExtensionsTrash,
    ModelsRefresh,
    ModelsList,
    ModelShow,
    ModelTiers,
    ModelSelect,
    ReasoningEffortSelect,
    AgentsList,
    AgentCurrent,
    AgentSelect,
    AgentReset,
    ToolsList,
    ContextInspect,
    FileSearch,
    WorktreeList,
    WorktreeInspect,
    WorktreeReview,
    WorktreeAcknowledge,
    WorktreeIntegrate,
    WorktreeDiscard,
    Doctor,
    Health,
    WebLaunch,
    Shutdown,
)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class SessionListResult(msgspec.Struct, tag=True, frozen=True):
    sessions: list[SessionSummary] = msgspec.field(default_factory=list)


class SessionOpenResult(msgspec.Struct, tag=True, frozen=True):
    session: SessionSummary


class SessionStartResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    turn_id: str
    scheduled: bool = True


class SessionEnqueueResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    queued_id: str
    depth: int = 0
    turn_id: str = ""


class SessionCancelResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    cancelled: bool = False
    dropped: int = 0


class SessionSubscribeResult(msgspec.Struct, tag=True, frozen=True):
    """Names the stream a transport attaches to; events follow out of band."""

    session: str
    subscribed: bool = True
    from_seq: int = 0


class SessionStateResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    seq: int = 0
    view: dict[str, Any] = msgspec.field(default_factory=dict)


class LogEntry(msgspec.Struct, frozen=True):
    """One allowlisted log projection; never a raw session event."""

    source: Literal["daemon", "session"]
    seq: int
    ts: float
    level: Literal["info", "warning", "error"]
    kind: str
    summary: str


class DaemonLogPage(msgspec.Struct, frozen=True):
    entries: list[LogEntry] = msgspec.field(default_factory=list)
    next_cursor: str | None = None
    truncated: bool = False
    has_more: bool = False


class SessionLogPage(msgspec.Struct, frozen=True):
    entries: list[LogEntry] = msgspec.field(default_factory=list)
    next_cursor: int = 0
    truncated: bool = False
    has_more: bool = False


class LogsReadResult(msgspec.Struct, tag=True, frozen=True):
    daemon: DaemonLogPage
    session: SessionLogPage


class AgentTranscriptResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    agent_id: str
    found: bool = True
    status: str = "running"
    view: dict[str, Any] = msgspec.field(default_factory=dict)


class SessionForkResult(msgspec.Struct, tag=True, frozen=True):
    session: SessionSummary


class SessionDeleteResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    trash_id: str
    delete_after: float = 0.0


class SessionRestoreResult(msgspec.Struct, tag=True, frozen=True):
    session: str


class SessionExportResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    format: str
    content: str


class PermissionResolveResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    request_id: str
    resolved: bool = False
    client_id: str | None = None


class ExtensionsReloadResult(msgspec.Struct, tag=True, frozen=True):
    generation: int = 0
    previous_generation: int = 0
    changed: bool = False
    loaded: list[str] = msgspec.field(default_factory=list)
    unloaded: list[str] = msgspec.field(default_factory=list)
    failed: list[dict[str, Any]] = msgspec.field(default_factory=list)


class ExtensionsListResult(msgspec.Struct, tag=True, frozen=True):
    generation: int = 0
    extensions: list[dict[str, Any]] = msgspec.field(default_factory=list)


class ExtensionsValidateResult(msgspec.Struct, tag=True, frozen=True):
    generation: int = 0
    valid: bool = False
    checked: int = 0
    results: list[dict[str, Any]] = msgspec.field(default_factory=list)


class ExtensionsTrashResult(msgspec.Struct, tag=True, frozen=True):
    """The durable trash record plus the rebuild that retired the extension."""

    target: str = ""
    trash_id: str = ""
    source_path: str = ""
    relative_path: str = ""
    origin: str = ""
    names: list[str] = msgspec.field(default_factory=list)
    tools: list[str] = msgspec.field(default_factory=list)
    sha256: str = ""
    module_generation: int = 0
    trashed_at: float = 0.0
    delete_after: float = 0.0
    reason: str = ""
    changed: bool = False
    generation: int = 0
    previous_generation: int = 0


class ModelsRefreshResult(msgspec.Struct, tag=True, frozen=True):
    status: dict[str, Any] | None = None


class ModelsListResult(msgspec.Struct, tag=True, frozen=True):
    """Model rows; selectable-only rows may include runtime ``supported_efforts``."""

    count: int = 0
    models: list[dict[str, Any]] = msgspec.field(default_factory=list)


class ModelShowResult(msgspec.Struct, tag=True, frozen=True):
    ref: str = ""
    found: bool = False
    model: dict[str, Any] | None = None
    tier_source: str = ""


class ModelTiersResult(msgspec.Struct, tag=True, frozen=True):
    order: list[str] = msgspec.field(default_factory=list)
    default: str = ""
    builtin: dict[str, str] = msgspec.field(default_factory=dict)
    overrides: dict[str, str] = msgspec.field(default_factory=dict)


class ModelSelectResult(msgspec.Struct, tag=True, frozen=True):
    """The accepted, validated selection plus how it resolved.

    ``fallback`` is the configured fallback chain that still applies if the
    selected provider fails at call time. ``apply_next_turn`` is always true: a
    running turn's model was frozen at its start, so the selection takes effect
    from the session's next turn onward.
    """

    session: str
    accepted: bool = True
    reference: str = ""
    provider: str = ""
    model: str = ""
    tier: str = ""
    tier_source: str = ""
    requested_tier: str = ""
    clamped: bool = False
    fallback: list[str] = msgspec.field(default_factory=list)
    apply_next_turn: bool = True


class ReasoningEffortSelectResult(msgspec.Struct, tag=True, frozen=True):
    """The durable root-session override and its current effective value."""

    session: str
    accepted: bool = True
    stored_override: str | None = None
    effective_effort: str | None = None
    source: str | None = None
    supported_levels: list[str] = msgspec.field(default_factory=list)
    apply_next_turn: bool = True


class AgentsListResult(msgspec.Struct, tag=True, frozen=True):
    generation: int = 0
    agents: list[dict[str, Any]] = msgspec.field(default_factory=list)


class AgentCurrentResult(msgspec.Struct, tag=True, frozen=True):
    """Current root-agent metadata for the session's next turn.

    ``reasoning_effort`` is the effort actually applied to the root turn;
    supported levels, the stored session override, and its source describe the
    Ctrl+T selection state.
    """

    session: str
    name: str = "general"
    source: str = "default"
    color: str | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    supported_levels: list[str] = msgspec.field(default_factory=list)
    stored_override: str | None = None
    reasoning_effort_source: str | None = None
    thinking_budget: int | None = None


class AgentSelectResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    name: str = "general"
    source: str = "session"
    apply_next_turn: bool = True


class ToolsListResult(msgspec.Struct, tag=True, frozen=True):
    count: int = 0
    tools: list[dict[str, Any]] = msgspec.field(default_factory=list)


class ContextInspectResult(msgspec.Struct, tag=True, frozen=True):
    """A redacted, read-only inspection of the assembled next-turn request."""

    session: str
    mode: Literal["next_turn_preview"] = "next_turn_preview"
    actually_sent: bool = False
    draft_provided: bool = False
    manifest_generation: int | None = None
    agent: dict[str, Any] = msgspec.field(default_factory=dict)
    system_files: dict[str, Any] = msgspec.field(default_factory=dict)
    system_text: str | None = None
    redacted_for_display: bool = False
    skills_index: list[dict[str, Any]] = msgspec.field(default_factory=list)
    mcp_index: str = ""
    included_parts: list[dict[str, str]] = msgspec.field(default_factory=list)
    tools: list[dict[str, Any]] = msgspec.field(default_factory=list)
    tools_supported: bool | None = None
    messages: list[dict[str, Any]] = msgspec.field(default_factory=list)
    history_included: bool = False
    request_context: dict[str, Any] = msgspec.field(default_factory=dict)
    params: dict[str, Any] = msgspec.field(default_factory=dict)
    model: str | None = None
    provider: str | None = None
    budget: dict[str, Any] = msgspec.field(default_factory=dict)
    omitted: list[str] = msgspec.field(default_factory=list)


class FileSearchResult(msgspec.Struct, tag=True, frozen=True):
    paths: list[str] = msgspec.field(default_factory=list)


class WorktreeListResult(msgspec.Struct, tag=True, frozen=True):
    status: str = "ok"
    worktrees: list[dict[str, Any]] = msgspec.field(default_factory=list)
    has_more: bool = False


class WorktreeInspectResult(msgspec.Struct, tag=True, frozen=True):
    child_id: str
    status: str
    record: dict[str, Any] = msgspec.field(default_factory=dict)


class WorktreeReviewResult(msgspec.Struct, tag=True, frozen=True):
    child_id: str
    status: str
    record: dict[str, Any] = msgspec.field(default_factory=dict)
    entries: list[dict[str, Any]] = msgspec.field(default_factory=list)
    diff: list[dict[str, Any]] = msgspec.field(default_factory=list)
    cursor: int = 0
    has_more: bool = False
    review_id: str = ""
    digest: str = ""


class WorktreeAcknowledgeResult(msgspec.Struct, tag=True, frozen=True):
    child_id: str
    status: str = "acknowledged"
    review_id: str = ""
    digest: str = ""


class WorktreeMutationResult(msgspec.Struct, tag=True, frozen=True):
    """A confirmation preview or exact outcome of a worktree mutation."""

    child_id: str
    status: Literal[
        "requires_confirmation",
        "committed",
        "rolled_back",
        "recovery_required",
        "cleanup_pending",
    ]
    operation: str = ""
    review_id: str | None = None
    digest: str | None = None
    confirmation_token: str = ""
    impact: dict[str, Any] = msgspec.field(default_factory=dict)
    transaction_id: str | None = None
    changed_paths: list[str] = msgspec.field(default_factory=list)
    error: str | None = None


class DoctorResult(msgspec.Struct, tag=True, frozen=True):
    ok: bool = True
    report: dict[str, Any] = msgspec.field(default_factory=dict)


class HealthResult(msgspec.Struct, tag=True, frozen=True):
    ok: bool = True
    version: int = PROTOCOL_VERSION
    sessions: int = 0
    running: int = 0
    queued: int = 0
    max_concurrent: int = 0
    viewers: int = 0
    uptime: float = 0.0


class ShutdownResult(msgspec.Struct, tag=True, frozen=True):
    stopping: bool = True
    reason: str = ""


class WebLaunchResult(msgspec.Struct, tag=True, frozen=True):
    """Browser URL carrying its one-time launch ticket in the fragment."""

    url: str = ""


class ErrorResult(msgspec.Struct, tag=True, frozen=True):
    """A failed command, with a redacted message (never a credential)."""

    kind: str = "error"
    message: str = ""


#: The complete result union. ``msgspec`` decodes it by the ``type`` tag.
Result = (
    SessionListResult
    | SessionOpenResult
    | SessionStartResult
    | SessionEnqueueResult
    | SessionCancelResult
    | SessionSubscribeResult
    | SessionStateResult
    | LogsReadResult
    | AgentTranscriptResult
    | SessionForkResult
    | SessionDeleteResult
    | SessionRestoreResult
    | SessionExportResult
    | PermissionResolveResult
    | ExtensionsReloadResult
    | ExtensionsListResult
    | ExtensionsValidateResult
    | ExtensionsTrashResult
    | ModelsRefreshResult
    | ModelsListResult
    | ModelShowResult
    | ModelTiersResult
    | ModelSelectResult
    | ReasoningEffortSelectResult
    | AgentsListResult
    | AgentCurrentResult
    | AgentSelectResult
    | ToolsListResult
    | ContextInspectResult
    | FileSearchResult
    | WorktreeListResult
    | WorktreeInspectResult
    | WorktreeReviewResult
    | WorktreeAcknowledgeResult
    | WorktreeMutationResult
    | DoctorResult
    | HealthResult
    | WebLaunchResult
    | ShutdownResult
    | ErrorResult
)

RESULTS: tuple[type, ...] = (
    SessionListResult,
    SessionOpenResult,
    SessionStartResult,
    SessionEnqueueResult,
    SessionCancelResult,
    SessionSubscribeResult,
    SessionStateResult,
    LogsReadResult,
    AgentTranscriptResult,
    SessionForkResult,
    SessionDeleteResult,
    SessionRestoreResult,
    SessionExportResult,
    PermissionResolveResult,
    ExtensionsReloadResult,
    ExtensionsListResult,
    ExtensionsValidateResult,
    ExtensionsTrashResult,
    ModelsRefreshResult,
    ModelsListResult,
    ModelShowResult,
    ModelTiersResult,
    ModelSelectResult,
    ReasoningEffortSelectResult,
    AgentsListResult,
    AgentCurrentResult,
    AgentSelectResult,
    ToolsListResult,
    ContextInspectResult,
    FileSearchResult,
    WorktreeListResult,
    WorktreeInspectResult,
    WorktreeReviewResult,
    WorktreeAcknowledgeResult,
    WorktreeMutationResult,
    DoctorResult,
    HealthResult,
    WebLaunchResult,
    ShutdownResult,
    ErrorResult,
)


# ---------------------------------------------------------------------------
# Codec helpers
# ---------------------------------------------------------------------------


def encode_command(command: Command) -> bytes:
    """Encode a command to compact JSON bytes."""
    return msgspec.json.encode(command)


def decode_command(data: bytes | str) -> Command:
    """Decode a command by its ``type`` tag; unknown tags raise."""
    return msgspec.json.decode(data, type=Command)


def encode_result(result: Result) -> bytes:
    """Encode a result to compact JSON bytes."""
    return msgspec.json.encode(result)


def decode_result(data: bytes | str) -> Result:
    """Decode a result by its ``type`` tag; unknown tags raise."""
    return msgspec.json.decode(data, type=Result)


__all__ = [
    "COMMANDS",
    "PROTOCOL_VERSION",
    "RESULTS",
    "AgentCurrent",
    "AgentCurrentResult",
    "AgentReset",
    "AgentSelect",
    "AgentSelectResult",
    "AgentTranscript",
    "AgentTranscriptResult",
    "AgentsList",
    "AgentsListResult",
    "Command",
    "ContextInspect",
    "ContextInspectResult",
    "DaemonLogPage",
    "Doctor",
    "DoctorResult",
    "ErrorResult",
    "ExtensionsList",
    "ExtensionsListResult",
    "ExtensionsReload",
    "ExtensionsReloadResult",
    "ExtensionsTrash",
    "ExtensionsTrashResult",
    "ExtensionsValidate",
    "ExtensionsValidateResult",
    "FileSearch",
    "FileSearchResult",
    "Health",
    "HealthResult",
    "LogEntry",
    "LogsRead",
    "LogsReadResult",
    "ModelSelect",
    "ModelSelectResult",
    "ModelShow",
    "ModelShowResult",
    "ModelTiers",
    "ModelTiersResult",
    "ModelsList",
    "ModelsListResult",
    "ModelsRefresh",
    "ModelsRefreshResult",
    "PermissionResolve",
    "PermissionResolveResult",
    "ReasoningEffortSelect",
    "ReasoningEffortSelectResult",
    "Result",
    "SessionCancel",
    "SessionCancelResult",
    "SessionDelete",
    "SessionDeleteResult",
    "SessionEnqueue",
    "SessionEnqueueResult",
    "SessionExport",
    "SessionExportResult",
    "SessionFork",
    "SessionForkResult",
    "SessionList",
    "SessionListResult",
    "SessionLogPage",
    "SessionOpen",
    "SessionOpenResult",
    "SessionRestore",
    "SessionRestoreResult",
    "SessionStart",
    "SessionStartResult",
    "SessionState",
    "SessionStateResult",
    "SessionSubscribe",
    "SessionSubscribeResult",
    "Shutdown",
    "ShutdownResult",
    "ToolsList",
    "ToolsListResult",
    "WebLaunch",
    "WebLaunchResult",
    "WorktreeAcknowledge",
    "WorktreeAcknowledgeResult",
    "WorktreeDiscard",
    "WorktreeInspect",
    "WorktreeInspectResult",
    "WorktreeIntegrate",
    "WorktreeList",
    "WorktreeListResult",
    "WorktreeMutationResult",
    "WorktreeReview",
    "WorktreeReviewResult",
    "decode_command",
    "decode_result",
    "encode_command",
    "encode_result",
]
