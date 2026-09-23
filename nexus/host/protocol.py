"""Transport-neutral wire contract for the host facade (PLAN sections 14.4, 14.9).

Every surface speaks the same commands and receives the same results. The
structs are frozen ``msgspec`` records, so one protocol serves the local Unix
socket, the HTTP/SSE surface, and an in-process test with identical semantics:
a command is JSON-encoded, decoded by kind, dispatched through
:class:`~nexus.host.facade.HostFacade`, and the result is encoded the same way.

The protocol deliberately carries no credential, environment, or configuration
value: the verb list is exactly the PLAN §14.4 surface plus the Phase 8 queries
(extensions, models, agents, health, shutdown). Streaming is the one verb a
request/response pair cannot model, so ``SessionSubscribe`` names the stream and
the transport attaches through :meth:`HostFacade.subscribe`.
"""
from __future__ import annotations

from typing import Any

import msgspec

from ..session.manager import SessionSummary

#: Bumped when a command/result shape changes incompatibly, so a transport can
#: refuse a peer built from a different protocol revision instead of misreading.
PROTOCOL_VERSION = 1


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


class AgentsList(msgspec.Struct, tag=True, frozen=True):
    """The discovered subagent definitions (sanitized index rows)."""


class ToolsList(msgspec.Struct, tag=True, frozen=True):
    """The model-facing tool catalog for the current config and manifest."""


class Doctor(msgspec.Struct, tag=True, frozen=True):
    """A redacted health report: config, providers, registry, extensions, MCP.

    ``explain_reload`` adds the hot-vs-restart boundary (PLAN section 6.6).
    """

    explain_reload: bool = False


class Health(msgspec.Struct, tag=True, frozen=True):
    """Daemon-level liveness and scheduling counters."""


class Shutdown(msgspec.Struct, tag=True, frozen=True):
    reason: str = ""


#: The complete command union. ``msgspec`` decodes it by the ``type`` tag.
Command = (
    SessionList
    | SessionOpen
    | SessionStart
    | SessionEnqueue
    | SessionCancel
    | SessionSubscribe
    | SessionState
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
    | AgentsList
    | ToolsList
    | Doctor
    | Health
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
    AgentsList,
    ToolsList,
    Doctor,
    Health,
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


class AgentsListResult(msgspec.Struct, tag=True, frozen=True):
    generation: int = 0
    agents: list[dict[str, Any]] = msgspec.field(default_factory=list)


class ToolsListResult(msgspec.Struct, tag=True, frozen=True):
    count: int = 0
    tools: list[dict[str, Any]] = msgspec.field(default_factory=list)


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
    | AgentsListResult
    | ToolsListResult
    | DoctorResult
    | HealthResult
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
    AgentsListResult,
    ToolsListResult,
    DoctorResult,
    HealthResult,
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
    "AgentsList",
    "AgentsListResult",
    "Command",
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
    "Health",
    "HealthResult",
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
    "decode_command",
    "decode_result",
    "encode_command",
    "encode_result",
]
