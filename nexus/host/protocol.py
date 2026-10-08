"""Transport-neutral wire contract for the host facade (PLAN sections 14.4, 14.9).

Every surface speaks the same commands and receives the same results. The
structs are frozen ``msgspec`` records, so one protocol serves the local Unix
socket, the HTTP/SSE surface, and an in-process test with identical semantics:
a command is JSON-encoded, decoded by kind, dispatched through
:class:`~nexus.host.facade.HostFacade`, and the result is encoded the same way.

The protocol deliberately carries no credential, environment, or configuration
value back to a client: the verb list is exactly the PLAN §14.4 surface plus
host queries, owned-worktree review/mutation commands, and provider sign-in.
``ProviderKeySet`` and ``ProviderLoginCode`` are the inward-only exceptions (a
pasted API key goes to the daemon's private credential file, a one-time sign-in code to the
Claude CLI; neither is ever returned). Streaming is the one verb a
request/response pair cannot model, so ``SessionSubscribe`` names the stream and
the transport attaches through :meth:`HostFacade.subscribe`.
"""
from __future__ import annotations

from typing import Any, Literal

import msgspec

from ..host_support.archive_protocol import (
    ArchivedSummary,
    SessionArchive,
    SessionArchiveResult,
    SessionListArchived,
    SessionListArchivedResult,
    SessionListResult,
    SessionPreview,
    SessionPreviewResult,
    SessionSearch,
    SessionSearchResult,
    SessionUnarchive,
    SessionUnarchiveResult,
)
from ..session.manager import SessionSummary

#: Bumped when a command/result shape changes incompatibly, so a transport can
#: refuse a peer built from a different protocol revision instead of misreading.
PROTOCOL_VERSION = 3


# ---------------------------------------------------------------------------
# Commands (tagged by struct name; ``type`` is the discriminator)
# ---------------------------------------------------------------------------


class SessionList(msgspec.Struct, tag=True, frozen=True):
    """List every session as a transport-neutral summary."""


class ProjectSessionsList(msgspec.Struct, tag=True, frozen=True):
    """List saved sessions across projects in this Nexus home."""


class ProjectSessionOpen(msgspec.Struct, tag=True, frozen=True):
    workspace: str
    session: str
    browser: bool = False


class ProjectSession(msgspec.Struct, frozen=True):
    workspace: str
    project_id: str
    session: SessionSummary
    #: The repository's main checkout when ``workspace`` is a linked Git worktree
    #: (sessions there belong with the repository), else "".
    repo: str = ""
    #: The worktree's branch (or its name when detached); "" outside a linked worktree.
    worktree: str = ""


class ProjectSessionsListResult(msgspec.Struct, tag=True, frozen=True):
    workspace: str = ""
    sessions: list[ProjectSession] = msgspec.field(default_factory=list)
    truncated: bool = False


class ProjectSessionOpenResult(msgspec.Struct, tag=True, frozen=True):
    socket_path: str = ""
    url: str = ""


class SettingsInventory(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]


class SettingsRead(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    category: str
    id: str


class SettingsWrite(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    category: str
    id: str
    body: str
    expected_sha256: str | None = None


class SettingsMcpLoadingSet(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    server: str
    mode: Literal["search", "all"]
    expected_sha256: str


class SettingsMcpEnabledSet(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    server: str
    enabled: bool
    expected_sha256: str


class SettingsReset(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    category: str


class SettingsDelete(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    category: str
    id: str


class SetupStatus(msgspec.Struct, tag=True, frozen=True):
    """Read-only first-run provider connection state; no credentials cross the wire."""


class SetupSave(msgspec.Struct, tag=True, frozen=True):
    """Save a connected provider as the user-global default and reload routes.

    A blank ``model`` picks the provider's newest tool-calling model.
    """

    provider: str
    model: str = ""


class ProvidersStatus(msgspec.Struct, tag=True, frozen=True):
    """Settings → Providers: sign-in state per provider; never a credential."""


class ProviderLogin(msgspec.Struct, tag=True, frozen=True):
    """Start a browser or device-code sign-in; poll it with ``ProviderLoginPoll``.

    ``domain`` names a GitHub Enterprise host for ``github-copilot``.
    """

    provider: str
    method: str = ""
    domain: str = ""


class ProviderLoginPoll(msgspec.Struct, tag=True, frozen=True):
    login_id: str


class ProviderLoginCancel(msgspec.Struct, tag=True, frozen=True):
    login_id: str


class ProviderKeySet(msgspec.Struct, tag=True, frozen=True, repr_omit_defaults=True):
    """Store a pasted API key in the daemon's private credential file.

    The one command that carries a credential, and only inward: the key is
    never echoed in a result, an error, a log, or config.
    """

    provider: str
    key: str

    def __repr__(self) -> str:
        return f"ProviderKeySet(provider={self.provider!r}, key='***')"


class ProviderLogout(msgspec.Struct, tag=True, frozen=True):
    """Remove one provider's credential from the private credential file."""

    provider: str


class ProviderLoginCode(msgspec.Struct, tag=True, frozen=True, repr_omit_defaults=True):
    """Paste the one-time code a ``code_entry`` sign-in page shows (Claude).

    Inward only, like ``ProviderKeySet``: never echoed, logged or stored.
    """

    login_id: str
    code: str

    def __repr__(self) -> str:
        return f"ProviderLoginCode(login_id={self.login_id!r}, code='***')"


class ProvidersUsage(msgspec.Struct, tag=True, frozen=True):
    """Plan limits (5-hour, weekly, monthly…) for every connected provider."""


class SessionOpen(msgspec.Struct, tag=True, frozen=True):
    session: str
    create: bool = True
    recover: bool = True


class AttachmentPrepare(msgspec.Struct, tag=True, frozen=True):
    """Prepare a user-selected local file or uploaded bytes for a prompt."""

    name: str = ""
    path: str = ""
    data: bytes = b""


class AttachmentPrepareResult(msgspec.Struct, tag=True, frozen=True):
    attachment_id: str
    name: str
    kind: str
    preview: str


class AttachmentPreview(msgspec.Struct, tag=True, frozen=True):
    """Retrieve bounded daemon-owned image bytes for local terminal rendering."""

    attachment_id: str
    max_bytes: int = 4 * 1024 * 1024


class AttachmentPreviewResult(msgspec.Struct, tag=True, frozen=True):
    attachment_id: str
    media_type: str
    data: bytes


class SessionStart(msgspec.Struct, tag=True, frozen=True):
    session: str
    content: str = ""
    blocks: list[dict[str, Any]] = msgspec.field(default_factory=list)
    attachments: list[str] = msgspec.field(default_factory=list)
    attachment_labels: list[str] = msgspec.field(default_factory=list)


class SessionEnqueue(msgspec.Struct, tag=True, frozen=True):
    session: str
    mode: str = "steer"  # queue | steer | interrupt
    content: str = ""
    blocks: list[dict[str, Any]] = msgspec.field(default_factory=list)
    attachments: list[str] = msgspec.field(default_factory=list)
    attachment_labels: list[str] = msgspec.field(default_factory=list)


class SessionShell(msgspec.Struct, tag=True, frozen=True):
    """Run a user-typed ``!`` command with bash in the workspace.

    The output (bounded like the ``bash`` tool) is added to the model context;
    it never starts a turn. A running turn sees it at its next safe boundary.
    """

    session: str
    command: str


class SessionCancel(msgspec.Struct, tag=True, frozen=True):
    session: str
    reason: str = ""
    drop_queue: bool = True
    return_queue: bool = False  # recover removed queued text for the composer


class SessionQueueMove(msgspec.Struct, tag=True, frozen=True):
    """Swap one pending queued/steering message with its neighbour (durable ``input.moved``)."""

    session: str
    queued_id: str
    offset: int = -1  # -1 earlier, 1 later


class SessionQueueRemove(msgspec.Struct, tag=True, frozen=True):
    """Drop one pending queued/steering message (durable ``input.dropped``)."""

    session: str
    queued_id: str


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


class QuestionAnswer(msgspec.Struct, tag=True, frozen=True):
    """Answer one agent question: a choice id, or free text when it has none.

    Clients see a pending question as the running ``question`` tool call, so
    they address it by ``call_id``; ``question_id`` is accepted when known.
    """

    session: str
    answer: str
    call_id: str = ""
    question_id: str = ""


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


class ModelTierSet(msgspec.Struct, tag=True, frozen=True):
    """Settings -> Models: save the ordered models of one tier (global config).

    The first model whose provider can run is used. Returns the refreshed rows.
    """

    tier: str
    refs: list[str]


class ModelTierReset(msgspec.Struct, tag=True, frozen=True):
    """Remove the user's list for ``tier`` (back to the built-in or price rule)."""

    tier: str


class AgentMaxTierSet(msgspec.Struct, tag=True, frozen=True):
    """Set ``[agents] max_tier``: subagents never run above this tier."""

    tier: str


class DefaultModelSettings(msgspec.Struct, tag=True, frozen=True):
    """Read the ordered global default model chain."""


class DefaultModelSet(msgspec.Struct, tag=True, frozen=True):
    """Atomically save the first model and its ordered global fallbacks."""

    refs: list[str]


class SessionTitleSettings(msgspec.Struct, tag=True, frozen=True):
    """Read the automatic session-title settings (Settings -> Session titles)."""


class SessionTitleSettingsSet(msgspec.Struct, tag=True, frozen=True):
    """Switch automatic titles on or off and/or choose the title model."""

    enabled: bool | None = None
    model: str | None = None


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


class AgentDefaultSet(msgspec.Struct, tag=True, frozen=True):
    """Persist the root agent new sessions start with (``[agent] name``)."""

    name: str
    scope: Literal["global", "project"] = "global"


class ToolsList(msgspec.Struct, tag=True, frozen=True):
    """The model-facing tool catalog for the current config and manifest."""


class ContextExtensionSelect(msgspec.Struct, tag=True, frozen=True):
    session: str
    category: Literal["skills", "mcp", "tools"]
    name: str
    enabled: bool


class ContextMcpLoadingSelect(msgspec.Struct, tag=True, frozen=True):
    session: str
    server: str
    mode: Literal["search", "all"] | None


class ContextInspect(msgspec.Struct, tag=True, frozen=True):
    """Preview one session's next-turn standing context without starting a turn."""

    session: str


class McpServerShow(msgspec.Struct, tag=True, frozen=True):
    """Read retained MCP state without connecting or listing remote catalogs."""

    session: str
    name: str
    max_bytes: int = 262_144


class McpServerRestart(msgspec.Struct, tag=True, frozen=True):
    """Close one MCP server's connection and connect it again (the Restart button).

    The session names who asked; the server is shared by the daemon, so every
    session sees the new connection. A failed connect is reported, never raised.
    """

    session: str
    name: str


class SkillInspect(msgspec.Struct, tag=True, frozen=True):
    """Read a pinned skill snapshot without enabling or invoking it."""

    session: str
    name: str
    max_body_bytes: int = 65_536


class FileSearch(msgspec.Struct, tag=True, frozen=True):
    """Search workspace-relative file paths for composer completion."""

    query: str
    limit: int = 30


class GitDiff(msgspec.Struct, tag=True, frozen=True):
    """Bounded diff for the current workspace, optionally staged or against a ref."""

    staged: bool = False
    ref: str = ""


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


class Speak(msgspec.Struct, tag=True, frozen=True):
    """Speak only the latest completed assistant answer on the daemon host."""

    session_id: str
    download: bool = False


class SpeakStop(msgspec.Struct, tag=True, frozen=True):
    """Stop the speech that is playing now (Esc); a no-op when nothing is playing."""


class SpeakResult(msgspec.Struct, tag=True, frozen=True):
    message: str
    backend: str


class SpeechStatus(msgspec.Struct, tag=True, frozen=True):
    """Read the local speech model status (what ``/speak download`` fetches)."""


class SpeechPrepare(msgspec.Struct, tag=True, frozen=True):
    """Start the one-time speech model download (after the user consented)."""


class SpeechStatusResult(msgspec.Struct, tag=True, frozen=True):
    """``state``: unsupported (packages missing), absent, downloading, ready or error."""

    state: str
    progress: float = 0.0
    bytes_done: int = 0
    bytes_total: int = 0
    message: str = ""


class VoiceStatus(msgspec.Struct, tag=True, frozen=True):
    """Read the bounded local voice model status."""


class VoicePrepare(msgspec.Struct, tag=True, frozen=True):
    force: bool = False
    allow_download: bool = True


class VoiceTranscribe(msgspec.Struct, tag=True, frozen=True, repr_omit_defaults=True):
    audio: bytes
    request_id: str
    session: str = ""
    #: A live preview of a recording still in progress. The daemon answers
    #: ``voice_busy`` at once instead of queueing it behind other inference.
    partial: bool = False

    def __repr__(self) -> str:
        return f"VoiceTranscribe(request_id={self.request_id!r}, partial={self.partial!r}, audio=<redacted>)"


class VoiceCancel(msgspec.Struct, tag=True, frozen=True):
    request_id: str


class VoiceRemove(msgspec.Struct, tag=True, frozen=True):
    """Remove the cached local voice model."""


class Doctor(msgspec.Struct, tag=True, frozen=True):
    """A redacted health report: config, providers, registry, extensions, MCP.

    ``explain_reload`` adds the hot-vs-restart boundary (PLAN section 6.6).
    """

    explain_reload: bool = False


class UpdateStatus(msgspec.Struct, tag=True, frozen=True):
    """Is a newer release available? Answered from the daemon's cached check."""

    #: Claim the one-time announcement of the available release (a client toast).
    announce: bool = False


class Health(msgspec.Struct, tag=True, frozen=True):
    """Daemon-level liveness and scheduling counters."""


class MockList(msgspec.Struct, tag=True, frozen=True):
    """List the mock scenarios (dev mode only; MOCK_PLAN §4.1)."""


class MockStart(msgspec.Struct, tag=True, frozen=True):
    """Start a scripted mock scenario in a new session (dev mode only).

    ``speed`` scales streaming/tool pacing (``0`` is instant); ``seed`` fixes
    jitter. ``session`` may name the session; the host generates one otherwise.
    """

    scenario: str
    speed: float = 1.0
    seed: int = 0
    session: str = ""


class MockClean(msgspec.Struct, tag=True, frozen=True):
    """Restore the dev sandbox workspace to its seeded state (dev mode only)."""


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
    | ProjectSessionsList
    | ProjectSessionOpen
    | SettingsInventory
    | SettingsRead
    | SettingsMcpLoadingSet
    | SettingsMcpEnabledSet
    | SettingsWrite
    | SettingsDelete
    | SettingsReset
    | SetupStatus
    | SetupSave
    | ProvidersStatus
    | ProviderLogin
    | ProviderLoginPoll
    | ProviderLoginCancel
    | ProviderKeySet
    | ProviderLogout
    | ProviderLoginCode
    | ProvidersUsage
    | SessionArchive
    | SessionUnarchive
    | SessionListArchived
    | SessionPreview
    | SessionSearch
    | SessionOpen
    | AttachmentPrepare
    | AttachmentPreview
    | SessionStart
    | SessionEnqueue
    | SessionShell
    | SessionCancel
    | SessionQueueMove
    | SessionQueueRemove
    | SessionSubscribe
    | SessionState
    | LogsRead
    | AgentTranscript
    | SessionFork
    | SessionDelete
    | SessionRestore
    | SessionExport
    | PermissionResolve
    | QuestionAnswer
    | ExtensionsReload
    | ExtensionsList
    | ExtensionsValidate
    | ExtensionsTrash
    | ModelsRefresh
    | ModelsList
    | ModelShow
    | ModelTiers
    | ModelTierSet
    | ModelTierReset
    | DefaultModelSettings
    | DefaultModelSet
    | AgentMaxTierSet
    | SessionTitleSettings
    | SessionTitleSettingsSet
    | ModelSelect
    | ReasoningEffortSelect
    | AgentsList
    | AgentCurrent
    | AgentSelect
    | AgentReset
    | AgentDefaultSet
    | ToolsList
    | ContextInspect
    | SkillInspect
    | McpServerShow
    | McpServerRestart
    | ContextMcpLoadingSelect
    | ContextExtensionSelect
    | FileSearch
    | GitDiff
    | WorktreeList
    | WorktreeInspect
    | WorktreeReview
    | WorktreeAcknowledge
    | WorktreeIntegrate
    | WorktreeDiscard
    | Speak
    | SpeakStop
    | SpeechStatus
    | SpeechPrepare
    | VoiceStatus
    | VoicePrepare
    | VoiceTranscribe
    | VoiceCancel
    | VoiceRemove
    | Doctor
    | UpdateStatus
    | Health
    | MockList
    | MockStart
    | MockClean
    | WebLaunch
    | Shutdown
)

COMMANDS: tuple[type, ...] = (
    SessionList,
    SettingsInventory,
    SettingsRead,
    SettingsMcpLoadingSet,
    SettingsMcpEnabledSet,
    SettingsWrite,
    SettingsDelete,
    SettingsReset,
    SetupStatus,
    SetupSave,
    ProvidersStatus,
    ProviderLogin,
    ProviderLoginPoll,
    ProviderLoginCancel,
    ProviderKeySet,
    ProviderLogout,
    ProviderLoginCode,
    ProvidersUsage,
    SessionArchive,
    SessionUnarchive,
    SessionListArchived,
    SessionPreview,
    SessionSearch,
    SessionOpen,
    AttachmentPrepare,
    AttachmentPreview,
    SessionStart,
    SessionEnqueue,
    SessionShell,
    SessionCancel,
    SessionQueueMove,
    SessionQueueRemove,
    SessionSubscribe,
    SessionState,
    LogsRead,
    AgentTranscript,
    SessionFork,
    SessionDelete,
    SessionRestore,
    SessionExport,
    PermissionResolve,
    QuestionAnswer,
    ExtensionsReload,
    ExtensionsList,
    ExtensionsValidate,
    ExtensionsTrash,
    ModelsRefresh,
    ModelsList,
    ModelShow,
    ModelTiers,
    ModelTierSet,
    ModelTierReset,
    DefaultModelSettings,
    DefaultModelSet,
    AgentMaxTierSet,
    SessionTitleSettings,
    SessionTitleSettingsSet,
    ModelSelect,
    ReasoningEffortSelect,
    AgentsList,
    AgentCurrent,
    AgentSelect,
    AgentReset,
    AgentDefaultSet,
    ToolsList,
    ContextInspect,
    SkillInspect,
    McpServerShow,
    McpServerRestart,
    ContextMcpLoadingSelect,
    ContextExtensionSelect,
    FileSearch,
    GitDiff,
    WorktreeList,
    WorktreeInspect,
    WorktreeReview,
    WorktreeAcknowledge,
    WorktreeIntegrate,
    WorktreeDiscard,
    Speak,
    SpeakStop,
    SpeechStatus,
    SpeechPrepare,
    VoiceStatus,
    VoicePrepare,
    VoiceTranscribe,
    VoiceCancel,
    VoiceRemove,
    Doctor,
    UpdateStatus,
    Health,
    MockList,
    MockStart,
    MockClean,
    WebLaunch,
    Shutdown,
)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class SettingsCategory(msgspec.Struct, frozen=True):
    key: str
    label: str
    count: int


class SettingsItem(msgspec.Struct, frozen=True):
    category: str
    id: str
    label: str
    summary: str
    builtin: bool = False
    rel_path: str = ""
    #: A file in this scope that shadows a packaged built-in of the same id.
    overrides_builtin: bool = False


class SettingsInventoryResult(msgspec.Struct, tag=True, frozen=True):
    scope: str
    root_display: str
    categories: list[SettingsCategory] = msgspec.field(default_factory=list)
    items: list[SettingsItem] = msgspec.field(default_factory=list)


class SettingsReadResult(msgspec.Struct, tag=True, frozen=True):
    body: str
    rel_path: str
    builtin: bool
    sha256: str
    overrides_builtin: bool = False


class SettingsWriteResult(msgspec.Struct, tag=True, frozen=True):
    status: str
    sha256: str = ""
    loaded: list[str] = msgspec.field(default_factory=list)
    unloaded: list[str] = msgspec.field(default_factory=list)
    failed: list[str] = msgspec.field(default_factory=list)
    config_reloaded: bool = False


class SettingsResetResult(msgspec.Struct, tag=True, frozen=True):
    status: str
    trash_ids: list[str] = msgspec.field(default_factory=list)


class SettingsDeleteResult(msgspec.Struct, tag=True, frozen=True):
    status: str
    trash_id: str = ""


class SetupStatusResult(msgspec.Struct, tag=True, frozen=True):
    required: bool
    global_model: str = ""
    effective_model: str = ""
    providers: list[dict[str, Any]] = msgspec.field(default_factory=list)


class SetupSaveResult(msgspec.Struct, tag=True, frozen=True):
    global_model: str
    restart_required: bool = True


class ProvidersStatusResult(msgspec.Struct, tag=True, frozen=True):
    """Rows: ``id``, ``label``, ``methods``, ``help``, ``connected``, ``detail``, ``login``."""

    providers: list[dict[str, Any]] = msgspec.field(default_factory=list)


class ProviderLoginResult(msgspec.Struct, tag=True, frozen=True):
    """One sign-in: ``status`` is pending, connected, failed or cancelled."""

    login_id: str
    provider: str
    method: str = ""
    status: str = "pending"
    url: str = ""
    user_code: str = ""
    message: str = ""
    #: The page shows a code the user pastes back with ``ProviderLoginCode``.
    code_entry: bool = False


class ProvidersUsageResult(msgspec.Struct, tag=True, frozen=True):
    """Rows: ``id``, ``label``, ``plan``, ``source``, ``windows``, ``notes``, ``error``.

    A window is ``label``, ``used_percent`` (0-100 or ``None``), ``resets_at``
    (epoch seconds or ``None``), ``reset_text`` and ``detail``.
    ``not_connected`` names the providers that were skipped.
    """

    providers: list[dict[str, Any]] = msgspec.field(default_factory=list)
    not_connected: list[str] = msgspec.field(default_factory=list)
    fetched_at: float = 0.0


class ProviderAuthResult(msgspec.Struct, tag=True, frozen=True):
    provider: str
    connected: bool
    message: str = ""


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


class SessionShellResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    shell_id: str


class SessionCancelResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    cancelled: bool = False
    dropped: int = 0
    returned_messages: list[str] = msgspec.field(default_factory=list)


class SessionQueueEditResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    #: False when the message already ran, was dropped, or sits at the edge.
    changed: bool = False


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
    context: dict[str, Any] = msgspec.field(default_factory=dict)  # sent request, header-shaped


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


class QuestionAnswerResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    call_id: str = ""
    resolved: bool = False
    error: str | None = None


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
    """The tier table. ``tiers`` rows: ``name``, ``refs`` (ordered models in
    effect), ``source`` (``your list`` / ``built-in`` / ``by price``),
    ``resolved`` (the model it runs on now, ``""`` when none can run),
    ``runnable`` and ``editable``. ``max_tier`` is the subagent ceiling."""

    order: list[str] = msgspec.field(default_factory=list)
    default: str = ""
    builtin: dict[str, str] = msgspec.field(default_factory=dict)
    overrides: dict[str, str] = msgspec.field(default_factory=dict)
    tiers: list[dict[str, Any]] = msgspec.field(default_factory=list)
    max_tier: str = ""
    restart_required: bool = False


class SessionTitleSettingsResult(msgspec.Struct, tag=True, frozen=True):
    """``model`` is the setting (a tier or reference); ``resolved`` the concrete
    model it runs on now, ``""`` with ``message`` saying why when none can."""

    enabled: bool = True
    model: str = "low"
    resolved: str = ""
    message: str = ""


class DefaultModelSettingsResult(msgspec.Struct, tag=True, frozen=True):
    refs: list[str] = msgspec.field(default_factory=list)
    resolved: str = ""
    message: str = ""
    candidates: list[dict[str, Any]] = msgspec.field(default_factory=list)
    restart_required: bool = False


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
    #: The root agent a new session starts with (``[agent] name``).
    default: str = "build"


class AgentCurrentResult(msgspec.Struct, tag=True, frozen=True):
    """Current root-agent metadata for the session's next turn.

    ``reasoning_effort`` is the effort actually applied to the root turn;
    supported levels, the stored session override, and its source describe the
    Ctrl+T selection state.
    """

    session: str
    name: str = "build"
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
    name: str = "build"
    source: str = "session"
    apply_next_turn: bool = True


class AgentDefaultSetResult(msgspec.Struct, tag=True, frozen=True):
    #: The name written, and the default now in effect (a project config can
    #: still override a global write).
    name: str
    effective: str
    scope: str
    rel_path: str = ""


class ToolsListResult(msgspec.Struct, tag=True, frozen=True):
    count: int = 0
    tools: list[dict[str, Any]] = msgspec.field(default_factory=list)


class ContextInspectResult(msgspec.Struct, tag=True, frozen=True):
    """A redacted, read-only inspection of the assembled next-turn request."""

    session: str
    mode: Literal["next_turn_preview", "sent_request"] = "next_turn_preview"
    actually_sent: bool = False
    draft_provided: bool = False
    manifest_generation: int | None = None
    agent: dict[str, Any] = msgspec.field(default_factory=dict)
    system_files: dict[str, Any] = msgspec.field(default_factory=dict)
    system_text: str | None = None
    redacted_for_display: bool = False
    context_locked: bool = False
    skills_index: list[dict[str, Any]] = msgspec.field(default_factory=list)
    mcp_index: str = ""
    mcp_servers: list[dict[str, Any]] = msgspec.field(default_factory=list)
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


class McpServerShowResult(msgspec.Struct, tag=True, frozen=True):
    name: str
    status: str = "unavailable"
    scope: str = "unavailable"
    enabled: bool = False
    transport: str = ""
    command_label: str = ""
    tool_loading: str = ""
    tool_loading_source: str = ""
    server_info: dict[str, Any] = msgspec.field(default_factory=dict)
    instructions: str = ""
    tools: list[dict[str, Any]] = msgspec.field(default_factory=list)
    resources: list[dict[str, Any]] = msgspec.field(default_factory=list)
    prompts: list[dict[str, Any]] = msgspec.field(default_factory=list)
    detail: dict[str, Any] = msgspec.field(default_factory=dict)
    error: str = ""
    clipped: bool = False
    redacted_for_display: bool = True


class McpServerRestartResult(msgspec.Struct, tag=True, frozen=True):
    name: str
    status: str = "unavailable"
    error: str = ""


class SkillInspectResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    name: str
    status: Literal["ok", "error"] = "ok"
    error: str | None = None
    manifest_generation: int | None = None
    enabled: bool = False
    scope: str = ""
    origin: str = ""
    metadata: dict[str, Any] = msgspec.field(default_factory=dict)
    frontmatter_text: str = ""
    body: str = ""
    body_bytes: int = 0
    truncated: bool = False
    redacted_for_display: bool = True


class FileSearchResult(msgspec.Struct, tag=True, frozen=True):
    paths: list[str] = msgspec.field(default_factory=list)


class GitDiffResult(msgspec.Struct, tag=True, frozen=True):
    patch: str = ""
    truncated: bool = False


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


class VoiceStatusResult(msgspec.Struct, tag=True, frozen=True):
    state: str = "absent"
    progress: float = 0.0
    cached: bool = False
    bytes_done: int = 0
    bytes_total: int = 0
    device: str = ""
    revision: str = ""
    message: str = ""
    max_seconds: int = 120
    enabled: bool = False
    auto_send: bool = False
    configured_device: str = "auto"


class VoiceTranscribeResult(msgspec.Struct, tag=True, frozen=True):
    request_id: str
    text: str
    duration_s: float
    elapsed_s: float
    language: str = ""


class VoiceCancelResult(msgspec.Struct, tag=True, frozen=True):
    cancelled: bool = False


class UpdateStatusResult(msgspec.Struct, tag=True, frozen=True):
    enabled: bool = False
    current: str = ""
    latest: str | None = None
    #: The newer release, when there is one.
    available: str | None = None
    command: str = "nexus update"
    #: True only for the first ``announce`` request that sees this release.
    announce: bool = False


class HealthResult(msgspec.Struct, tag=True, frozen=True):
    ok: bool = True
    version: int = PROTOCOL_VERSION
    sessions: int = 0
    running: int = 0
    queued: int = 0
    max_concurrent: int = 0
    viewers: int = 0
    uptime: float = 0.0
    #: Dev mode (isolated home, sandbox workspace, ``/mock``); MOCK_PLAN §3.1.
    dev: bool = False


class MockScenarioInfo(msgspec.Struct, frozen=True):
    name: str
    summary: str
    tags: list[str] = msgspec.field(default_factory=list)
    est_seconds: int = 0
    interactive: bool = False
    slow: bool = False


class MockListResult(msgspec.Struct, tag=True, frozen=True):
    scenarios: list[MockScenarioInfo] = msgspec.field(default_factory=list)


class MockStartResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    scenario: str
    turn_id: str = ""
    interactive: bool = False


class MockCleanResult(msgspec.Struct, tag=True, frozen=True):
    restored: bool = True


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
    ProjectSessionsListResult
    | ProjectSessionOpenResult
    | SettingsInventoryResult
    | SettingsReadResult
    | SettingsWriteResult
    | SettingsDeleteResult
    | SettingsResetResult
    | SetupStatusResult
    | SetupSaveResult
    | ProvidersStatusResult
    | ProviderLoginResult
    | ProviderAuthResult
    | ProvidersUsageResult
    | SessionListResult
    | SessionArchiveResult
    | SessionUnarchiveResult
    | SessionListArchivedResult
    | SessionPreviewResult
    | SessionSearchResult
    | SessionOpenResult
    | AttachmentPrepareResult
    | AttachmentPreviewResult
    | SessionStartResult
    | SessionEnqueueResult
    | SessionShellResult
    | SessionCancelResult
    | SessionQueueEditResult
    | SessionSubscribeResult
    | SessionStateResult
    | LogsReadResult
    | AgentTranscriptResult
    | SessionForkResult
    | SessionDeleteResult
    | SessionRestoreResult
    | SessionExportResult
    | PermissionResolveResult
    | QuestionAnswerResult
    | ExtensionsReloadResult
    | ExtensionsListResult
    | ExtensionsValidateResult
    | ExtensionsTrashResult
    | ModelsRefreshResult
    | ModelsListResult
    | ModelShowResult
    | ModelTiersResult
    | DefaultModelSettingsResult
    | SessionTitleSettingsResult
    | ModelSelectResult
    | ReasoningEffortSelectResult
    | AgentsListResult
    | AgentCurrentResult
    | AgentSelectResult
    | AgentDefaultSetResult
    | ToolsListResult
    | ContextInspectResult
    | SkillInspectResult
    | McpServerShowResult
    | McpServerRestartResult
    | FileSearchResult
    | GitDiffResult
    | WorktreeListResult
    | WorktreeInspectResult
    | WorktreeReviewResult
    | WorktreeAcknowledgeResult
    | WorktreeMutationResult
    | DoctorResult
    | SpeakResult
    | SpeechStatusResult
    | VoiceStatusResult
    | VoiceTranscribeResult
    | VoiceCancelResult
    | UpdateStatusResult
    | HealthResult
    | MockListResult
    | MockStartResult
    | MockCleanResult
    | WebLaunchResult
    | ShutdownResult
    | ErrorResult
)

RESULTS: tuple[type, ...] = (
    SettingsInventoryResult,
    SettingsReadResult,
    SettingsWriteResult,
    SettingsDeleteResult,
    SettingsResetResult,
    SetupStatusResult,
    SetupSaveResult,
    ProvidersStatusResult,
    ProviderLoginResult,
    ProviderAuthResult,
    ProvidersUsageResult,
    SessionListResult,
    SessionArchiveResult,
    SessionUnarchiveResult,
    SessionListArchivedResult,
    SessionPreviewResult,
    SessionSearchResult,
    SessionArchiveResult,
    SessionUnarchiveResult,
    SessionListArchivedResult,
    SessionPreviewResult,
    SessionSearchResult,
    SessionOpenResult,
    AttachmentPrepareResult,
    AttachmentPreviewResult,
    SessionStartResult,
    SessionEnqueueResult,
    SessionShellResult,
    SessionCancelResult,
    SessionQueueEditResult,
    SessionSubscribeResult,
    SessionStateResult,
    LogsReadResult,
    AgentTranscriptResult,
    SessionForkResult,
    SessionDeleteResult,
    SessionRestoreResult,
    SessionExportResult,
    PermissionResolveResult,
    QuestionAnswerResult,
    ExtensionsReloadResult,
    ExtensionsListResult,
    ExtensionsValidateResult,
    ExtensionsTrashResult,
    ModelsRefreshResult,
    ModelsListResult,
    ModelShowResult,
    ModelTiersResult,
    DefaultModelSettingsResult,
    SessionTitleSettingsResult,
    ModelSelectResult,
    ReasoningEffortSelectResult,
    AgentsListResult,
    AgentCurrentResult,
    AgentSelectResult,
    AgentDefaultSetResult,
    ToolsListResult,
    ContextInspectResult,
    SkillInspectResult,
    McpServerShowResult,
    McpServerRestartResult,
    FileSearchResult,
    GitDiffResult,
    WorktreeListResult,
    WorktreeInspectResult,
    WorktreeReviewResult,
    WorktreeAcknowledgeResult,
    WorktreeMutationResult,
    DoctorResult,
    SpeakResult,
    SpeechStatusResult,
    VoiceStatusResult,
    VoiceTranscribeResult,
    VoiceCancelResult,
    UpdateStatusResult,
    HealthResult,
    MockListResult,
    MockStartResult,
    MockCleanResult,
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
    "AgentDefaultSet",
    "AgentDefaultSetResult",
    "AgentReset",
    "AgentSelect",
    "AgentSelectResult",
    "AgentTranscript",
    "AgentTranscriptResult",
    "AgentsList",
    "AgentsListResult",
    "ArchivedSummary",
    "Command",
    "ContextInspect",
    "ContextMcpLoadingSelect",
    "ContextExtensionSelect",
    "ContextInspectResult",
    "DaemonLogPage",
    "Doctor",
    "DoctorResult",
    "UpdateStatus",
    "UpdateStatusResult",
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
    "GitDiff",
    "GitDiffResult",
    "Health",
    "HealthResult",
    "LogEntry",
    "LogsRead",
    "LogsReadResult",
    "MockClean",
    "MockCleanResult",
    "MockList",
    "MockListResult",
    "MockScenarioInfo",
    "MockStart",
    "MockStartResult",
    "ModelSelect",
    "ModelSelectResult",
    "ModelShow",
    "ModelShowResult",
    "ModelTiers",
    "ModelTiersResult",
    "ModelTierSet",
    "ModelTierReset",
    "DefaultModelSettings",
    "DefaultModelSet",
    "DefaultModelSettingsResult",
    "AgentMaxTierSet",
    "SessionTitleSettings",
    "SessionTitleSettingsSet",
    "SessionTitleSettingsResult",
    "ModelsList",
    "ModelsListResult",
    "ModelsRefresh",
    "ModelsRefreshResult",
    "PermissionResolve",
    "PermissionResolveResult",
    "ProviderAuthResult",
    "ProviderKeySet",
    "ProviderLogin",
    "ProviderLoginCancel",
    "ProviderLoginCode",
    "ProviderLoginPoll",
    "ProviderLoginResult",
    "ProviderLogout",
    "ProvidersStatus",
    "ProvidersStatusResult",
    "ProvidersUsage",
    "ProvidersUsageResult",
    "QuestionAnswer",
    "QuestionAnswerResult",
    "ReasoningEffortSelect",
    "ReasoningEffortSelectResult",
    "Result",
    "SessionArchive",
    "SessionArchiveResult",
    "SessionCancel",
    "SessionCancelResult",
    "SessionQueueEditResult",
    "SessionQueueMove",
    "SessionQueueRemove",
    "SessionDelete",
    "SessionDeleteResult",
    "SessionEnqueue",
    "SessionEnqueueResult",
    "SessionShell",
    "SessionShellResult",
    "SessionExport",
    "SessionExportResult",
    "SessionFork",
    "SessionForkResult",
    "SessionList",
    "SessionListArchived",
    "SessionListArchivedResult",
    "SessionListResult",
    "SessionLogPage",
    "SessionOpen",
    "SessionOpenResult",
    "SessionPreview",
    "SessionPreviewResult",
    "SessionRestore",
    "SessionRestoreResult",
    "SessionSearch",
    "SessionSearchResult",
    "SessionStart",
    "SessionStartResult",
    "SessionState",
    "SessionStateResult",
    "SessionSubscribe",
    "SessionSubscribeResult",
    "SessionUnarchive",
    "SessionUnarchiveResult",
    "SettingsCategory",
    "SettingsReset",
    "SettingsResetResult",
    "SettingsDelete",
    "SettingsDeleteResult",
    "SettingsInventory",
    "SettingsInventoryResult",
    "SettingsItem",
    "SettingsRead",
    "SettingsReadResult",
    "SettingsMcpLoadingSet",
    "SettingsMcpEnabledSet",
    "SettingsWrite",
    "SettingsWriteResult",
    "SetupSave",
    "SetupSaveResult",
    "SetupStatus",
    "SetupStatusResult",
    "Shutdown",
    "ShutdownResult",
    "ToolsList",
    "ToolsListResult",
    "WebLaunch",
    "WebLaunchResult",
    "VoiceCancel",
    "VoiceCancelResult",
    "VoicePrepare",
    "VoiceRemove",
    "Speak",
    "SpeakResult",
    "SpeakStop",
    "SpeechPrepare",
    "SpeechStatus",
    "SpeechStatusResult",
    "VoiceStatus",
    "VoiceStatusResult",
    "VoiceTranscribe",
    "VoiceTranscribeResult",
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
