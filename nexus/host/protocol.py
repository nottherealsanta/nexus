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
PROTOCOL_VERSION = 6


# ---------------------------------------------------------------------------
# Commands (tagged by struct name; ``type`` is the discriminator)
# ---------------------------------------------------------------------------


class ProjectRef(msgspec.Struct, frozen=True):
    key: str
    root: str
    name: str
    kind: Literal["git", "plain"]


class ProjectFocus(msgspec.Struct, tag=True, frozen=True):
    project: str


class ProjectOpen(msgspec.Struct, tag=True, frozen=True):
    path: str


class ProjectList(msgspec.Struct, tag=True, frozen=True):
    limit: int = 100


class ProjectUnload(msgspec.Struct, tag=True, frozen=True):
    project: str = ""
    force: bool = False


class ProjectReload(msgspec.Struct, tag=True, frozen=True):
    project: str = ""


class ProjectFocusResult(msgspec.Struct, tag=True, frozen=True):
    project: ProjectRef


class ProjectOpenResult(msgspec.Struct, tag=True, frozen=True):
    project: ProjectRef
    created: bool


class ProjectRow(msgspec.Struct, frozen=True):
    project: ProjectRef
    loaded: bool
    exists: bool
    sessions: int = 0
    running: int = 0
    queued: int = 0
    worktrees: int = 0
    behind: int = 0
    last_activity: float = 0.0
    loaded_at: float = 0.0
    environment_from: str = ""


class ProjectListResult(msgspec.Struct, tag=True, frozen=True):
    projects: list[ProjectRow] = msgspec.field(default_factory=list)
    truncated: bool = False


class SessionList(msgspec.Struct, tag=True, frozen=True):
    """List every session as a transport-neutral summary."""

    include_children: bool = False
    project: str = ""


class SessionResolve(msgspec.Struct, tag=True, frozen=True):
    ref: str
    namespace: str | None = None
    include_retired: bool = False
    project: str = ""


class SessionResolveResult(msgspec.Struct, tag=True, frozen=True):
    resolution: dict[str, Any] = msgspec.field(default_factory=dict)


class SessionTree(msgspec.Struct, tag=True, frozen=True):
    """Read the bounded ownership tree for this workspace; never open a handle."""

    session: str
    namespace: str = "main"
    project: str = ""


class SessionTreeResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    tree: dict[str, Any] = msgspec.field(default_factory=dict)


class ProjectSessionsList(msgspec.Struct, tag=True, frozen=True):
    """List saved sessions across projects in this Nexus home."""

    limit: int = 1000
    query: str = ""
    #: Also return up to this many of the newest archived sessions (``archived=True``
    #: rows after the regular ones); 0 returns none. Bounded at 100 by the host.
    archived: int = 0
    #: Also return up to this many registered projects (``ProjectSessionsListResult.projects``,
    #: newest opened first, the active workspace always included) whether or not they have a
    #: listed session; 0 returns none. Bounded at 500 by the host.
    projects: int = 0
    project: str = ""


class ProjectSessionOpen(msgspec.Struct, tag=True, frozen=True):
    workspace: str
    session: str
    browser: bool = False
    project: str = ""


class ProjectSession(msgspec.Struct, frozen=True):
    workspace: str
    project_id: str
    session: SessionSummary
    #: The repository's main checkout when ``workspace`` is a linked Git worktree
    #: (sessions there belong with the repository), else "".
    repo: str = ""
    #: The worktree's branch (or its name when detached); "" outside a linked worktree.
    worktree: str = ""
    #: Bounded display snippets from the session row. Not part of ``SessionSummary``.
    last_user: str = ""
    last_agent: str = ""
    #: An archived session returned for ``ProjectSessionsList.archived``.
    archived: bool = False
    #: The unanswered permission request of a session in ``awaiting_permission``:
    #: ``{request_id, kind, summary, ts}`` (bounded, redacted), else empty.
    pending: dict[str, Any] = msgspec.field(default_factory=dict)


class ProjectEntry(msgspec.Struct, frozen=True):
    """A listed project root: the current workspace, or one with an eligible (maybe archived) session."""

    workspace: str
    project_id: str = ""
    repo: str = ""
    worktree: str = ""
    #: Newest user activity (epoch seconds) among the project's eligible sessions; 0 when none.
    updated: float = 0.0
    #: Whether the root is a directory on disk; clients omit missing historical roots.
    exists: bool = True


class ProjectSessionsListResult(msgspec.Struct, tag=True, frozen=True):
    workspace: str = ""
    sessions: list[ProjectSession] = msgspec.field(default_factory=list)
    truncated: bool = False
    #: Registered projects, only when ``ProjectSessionsList.projects`` > 0.
    projects: list[ProjectEntry] = msgspec.field(default_factory=list)


class ProjectSessionOpenResult(msgspec.Struct, tag=True, frozen=True):
    project: ProjectRef | None = None
    session: str = ""
    url: str = ""


class SettingsInventory(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    project: str = ""


class SettingsRead(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    category: str
    id: str
    project: str = ""


class SettingsWrite(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    category: str
    id: str
    body: str
    expected_sha256: str | None = None
    project: str = ""


class SettingsMcpLoadingSet(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    server: str
    mode: Literal["search", "all"]
    expected_sha256: str
    project: str = ""


class SettingsMcpEnabledSet(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    server: str
    enabled: bool
    expected_sha256: str
    project: str = ""


class SettingsMcpAdd(msgspec.Struct, tag=True, frozen=True):
    """Insert one new server into the scope's ``mcp.json`` without touching anything else.

    ``entry`` may carry literal credentials (``env``, ``headers``, URL userinfo), so the
    repr masks it. ``expected_sha256``: ``None`` accepts the current file, ``""`` requires
    that the file does not exist, a hex digest must match the file as read. Settings only:
    never a model tool, never reachable by a paired device (default-denied by the gateway).

    ``dry_run`` validates and checks the target exactly like a real add (conflict, legacy
    file, global root) but writes and starts nothing: the result carries ``status="preview"``
    with the masked confirmation ``lines`` and the structured ``transport``.
    """

    scope: Literal["global", "project"]
    name: str
    entry: dict[str, Any]
    expected_sha256: str | None = None
    dry_run: bool = False

    def __repr__(self) -> str:
        return (f"SettingsMcpAdd(scope={self.scope!r}, name={self.name!r}, entry=<masked>, "
                f"expected_sha256={self.expected_sha256!r}, dry_run={self.dry_run!r})")
    project: str = ""


class SettingsReset(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    category: str
    project: str = ""


class SettingsDelete(msgspec.Struct, tag=True, frozen=True):
    scope: Literal["global", "project"]
    category: str
    id: str
    project: str = ""


class SkillsInstall(msgspec.Struct, tag=True, frozen=True):
    """Install skills from an ``npx skills`` source into the scope's skills folder.

    Runs third-party code (``npx``) at the user's explicit request, so it is a
    Settings action only: never a model tool, never reachable by a paired device.
    An empty ``skills`` installs everything the source offers.
    """

    scope: Literal["global", "project"]
    source: str
    skills: list[str] = msgspec.field(default_factory=list)
    overwrite: bool = False
    project: str = ""


class SetupStatus(msgspec.Struct, tag=True, frozen=True):
    """Read-only first-run provider connection state; no credentials cross the wire."""
    project: str = ""


class SetupSave(msgspec.Struct, tag=True, frozen=True):
    """Save a connected provider as the user-global default and reload routes.

    A blank ``model`` picks the provider's newest tool-calling model.
    """

    provider: str
    model: str = ""
    project: str = ""


class ProvidersStatus(msgspec.Struct, tag=True, frozen=True):
    """Settings → Providers: sign-in state per provider; never a credential."""
    project: str = ""


class ProviderLogin(msgspec.Struct, tag=True, frozen=True):
    """Start a browser or device-code sign-in; poll it with ``ProviderLoginPoll``.

    ``domain`` names a GitHub Enterprise host for ``github-copilot``.
    """

    provider: str
    method: str = ""
    domain: str = ""
    project: str = ""


class ProviderLoginPoll(msgspec.Struct, tag=True, frozen=True):
    login_id: str
    project: str = ""


class ProviderLoginCancel(msgspec.Struct, tag=True, frozen=True):
    login_id: str
    project: str = ""


class ProviderKeySet(msgspec.Struct, tag=True, frozen=True, repr_omit_defaults=True):
    """Store a pasted API key in the daemon's private credential file.

    The one command that carries a credential, and only inward: the key is
    never echoed in a result, an error, a log, or config.
    """

    provider: str
    key: str

    def __repr__(self) -> str:
        return f"ProviderKeySet(provider={self.provider!r}, key='***')"
    project: str = ""


class ProviderLogout(msgspec.Struct, tag=True, frozen=True):
    """Remove one provider's credential from the private credential file."""

    provider: str
    project: str = ""


class ProviderModelsRefresh(msgspec.Struct, tag=True, frozen=True):
    """Re-run one provider's own model discovery (today only Antigravity ``agy models``).

    Separate from ``ModelsRefresh`` (the models.dev catalogue), which never re-probes ``agy``.
    """

    provider: str
    project: str = ""


class ProviderLoginCode(msgspec.Struct, tag=True, frozen=True, repr_omit_defaults=True):
    """Paste the one-time code a ``code_entry`` sign-in page shows (Claude).

    Inward only, like ``ProviderKeySet``: never echoed, logged or stored.
    """

    login_id: str
    code: str

    def __repr__(self) -> str:
        return f"ProviderLoginCode(login_id={self.login_id!r}, code='***')"
    project: str = ""


class ProvidersUsage(msgspec.Struct, tag=True, frozen=True):
    """Plan limits (5-hour, weekly, monthly…) for every connected provider."""
    project: str = ""


class SessionHandoff(msgspec.Struct, tag=True, frozen=True):
    """Idle same-session cutover; continuation is admitted in the same commit."""

    session: str
    summary: str | None = None
    continuation: str | None = None
    request_id: str = ""
    project: str = ""


class SessionHandoffRead(msgspec.Struct, tag=True, frozen=True):
    session: str
    namespace: str = "main"
    handoff_id: str = ""
    project: str = ""


class SessionInspect(msgspec.Struct, tag=True, frozen=True):
    """Bounded durable read without opening a session handle."""
    session: str
    namespace: str = "agents"
    project: str = ""


class SessionInspectResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    namespace: str
    view: dict[str, Any]
    request: dict[str, Any]
    truncated: bool = False


class SessionOpen(msgspec.Struct, tag=True, frozen=True):
    session: str
    create: bool = True
    recover: bool = True
    project: str = ""
    #: Optional absolute external linked-worktree path for a newly created session.
    root: str = ""


class SessionWorktreeSet(msgspec.Struct, tag=True, frozen=True):
    """Set a draft session's worktree preference before its first prompt."""

    session: str
    enabled: bool
    base: str = "current"


class SessionPromote(msgspec.Struct, tag=True, frozen=True):
    """Promote a main/external session into an owned worktree at a turn boundary."""

    session: str
    reason: str = ""
    continuation: str = ""


class SessionDetach(msgspec.Struct, tag=True, frozen=True):
    """Detach a session from its owned worktree and continue in the main checkout."""

    session: str


class SessionWorktreeStatus(msgspec.Struct, tag=True, frozen=True):
    """Read the worktree selection and repository state for a root session."""

    session: str


class SessionWorktreeTrust(msgspec.Struct, tag=True, frozen=True):
    """Answer setup trust for the exact setup digest shown in status."""

    session: str
    decision: Literal["allow_once", "allow_project", "skip"]
    digest: str


class AttachmentPrepare(msgspec.Struct, tag=True, frozen=True):
    """Prepare a user-selected local file or uploaded bytes for a prompt."""

    name: str = ""
    path: str = ""
    data: bytes = b""
    project: str = ""


class AttachmentPrepareResult(msgspec.Struct, tag=True, frozen=True):
    attachment_id: str
    name: str
    kind: str
    preview: str


class AttachmentPreview(msgspec.Struct, tag=True, frozen=True):
    """Retrieve bounded daemon-owned image bytes for local terminal rendering."""

    attachment_id: str
    max_bytes: int = 4 * 1024 * 1024
    project: str = ""


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

    client_request_id: str = ""
    project: str = ""


class SessionEnqueue(msgspec.Struct, tag=True, frozen=True):
    session: str
    mode: str = "steer"  # queue | steer | interrupt
    content: str = ""
    blocks: list[dict[str, Any]] = msgspec.field(default_factory=list)
    attachments: list[str] = msgspec.field(default_factory=list)
    attachment_labels: list[str] = msgspec.field(default_factory=list)

    client_request_id: str = ""
    project: str = ""


class SessionShell(msgspec.Struct, tag=True, frozen=True):
    """Run a user-typed ``!`` command with bash in the workspace.

    The output (bounded like the ``bash`` tool) is added to the model context;
    it never starts a turn. A running turn sees it at its next safe boundary.
    """

    session: str
    command: str
    project: str = ""


class SessionJobs(msgspec.Struct, tag=True, frozen=True):
    """List the ``shell`` tool jobs the named session owns (running first, bounded).

    Another session's jobs are never included: the host answers from the job
    registry partition of ``session`` only.
    """

    session: str
    project: str = ""


class SessionJobLog(msgspec.Struct, tag=True, frozen=True):
    """Read a bounded slice of one of the session's job logs.

    ``job_id`` is the registry-owned id the tool printed, never a path. With
    ``offset`` unset the reply is the last ``tail_lines`` lines; with it set, up to
    ``limit`` bytes from that byte offset (``next_offset`` continues the read).
    """

    session: str
    job_id: str
    tail_lines: int = 200
    offset: int | None = None
    limit: int = 32768
    project: str = ""


class SessionJobStop(msgspec.Struct, tag=True, frozen=True):
    """Stop one of the session's running jobs (the whole process group)."""

    session: str
    job_id: str
    project: str = ""


class SessionJobBackground(msgspec.Struct, tag=True, frozen=True):
    """Ask the tool call blocking on a foreground job to hand it to the background.

    Only a job that a tool call is awaiting in the foreground can be moved; the
    reply says whether the request was accepted (``outcome``).
    """

    session: str
    job_id: str
    project: str = ""


class SessionCancel(msgspec.Struct, tag=True, frozen=True):
    session: str
    reason: str = ""
    drop_queue: bool = True
    return_queue: bool = False  # recover removed queued text for the composer
    project: str = ""


class SessionQueueMove(msgspec.Struct, tag=True, frozen=True):
    """Swap one pending queued/steering message with its neighbour (durable ``input.moved``)."""

    session: str
    queued_id: str
    offset: int = -1  # -1 earlier, 1 later
    project: str = ""


class SessionQueueRemove(msgspec.Struct, tag=True, frozen=True):
    """Drop one pending queued/steering message (durable ``input.dropped``).

    ``return_content`` also returns the removed message's full content blocks
    (the queue view cuts text), so a client can restore it to the composer.
    """

    session: str
    queued_id: str
    return_content: bool = False
    project: str = ""


class SessionQueueSetMode(msgspec.Struct, tag=True, frozen=True):
    """Promote one pending message: ``queue -> steer -> interrupt`` (durable ``input.mode_changed``)."""

    session: str
    queued_id: str
    mode: str  # steer | interrupt
    project: str = ""


class SessionSubscribe(msgspec.Struct, tag=True, frozen=True):
    session: str
    from_seq: int = 0
    follow: bool = True
    project: str = ""


class SessionFocus(msgspec.Struct, tag=True, frozen=True):
    """Report a client's foreground root session; never inferred from subscriptions."""

    session: str | None
    client_id: str
    project: str = ""


class SessionFocusResult(msgspec.Struct, tag=True, frozen=True):
    session: str | None
    client_id: str


class SessionState(msgspec.Struct, tag=True, frozen=True):
    session: str
    from_seq: int = 0
    project: str = ""


class SessionActivity(msgspec.Struct, tag=True, frozen=True):
    """Is the session quiescent? Used by one-shot runs that wait for background subagents."""

    session: str
    project: str = ""


class LogsRead(msgspec.Struct, tag=True, frozen=True):
    """Read bounded, redacted daemon and session lifecycle diagnostics."""

    session: str | None = None
    daemon_cursor: str | None = None
    session_cursor: int | None = None
    limit: int = 50
    project: str = ""


class AgentCancel(msgspec.Struct, tag=True, frozen=True):
    """Cancel one running child agent (not its parent turn or siblings).

    An external Claude Code worker's process group is killed by its own
    cancellation path; the parent turn continues with a ``cancelled`` result.
    """

    session: str
    agent_id: str
    reason: str = ""
    project: str = ""


class AgentClose(msgspec.Struct, tag=True, frozen=True):
    """Close one child agent for good (``agent.stopped``), cancelling a run in flight.

    Unlike ``AgentCancel`` (which ends the current run and leaves the child idle),
    a closed child cannot be continued and gives its live slot back. Idempotent.
    """

    session: str
    agent_id: str
    reason: str = ""
    project: str = ""


class AgentTranscript(msgspec.Struct, tag=True, frozen=True):
    """Request the reducer-backed transcript for one child agent."""

    session: str
    agent_id: str
    project: str = ""


class SessionFork(msgspec.Struct, tag=True, frozen=True):
    session: str
    at_seq: int | None = None
    new_id: str | None = None
    project: str = ""


class SessionDelete(msgspec.Struct, tag=True, frozen=True):
    session: str
    force: bool = False
    reason: str = ""
    project: str = ""


class SessionRestore(msgspec.Struct, tag=True, frozen=True):
    trash_id: str
    project: str = ""


class SessionRename(msgspec.Struct, tag=True, frozen=True):
    """Set a session's title durably (``title_source='user'``); auto titles never replace it."""

    session_id: str
    title: str
    project: str = ""


class SessionExport(msgspec.Struct, tag=True, frozen=True):
    session: str
    format: str = "json"
    project: str = ""


class PermissionResolve(msgspec.Struct, tag=True, frozen=True):
    session: str
    request_id: str
    decision: str
    client_id: str | None = None
    project: str = ""


class QuestionAnswer(msgspec.Struct, tag=True, frozen=True):
    """Answer one agent question: a choice id, or free text when it has none.

    Clients see a pending question as the running ``question`` tool call, so
    they address it by ``call_id``; ``question_id`` is accepted when known.
    """

    session: str
    answer: str
    call_id: str = ""
    question_id: str = ""
    project: str = ""


class ExtensionsReload(msgspec.Struct, tag=True, frozen=True):
    trigger: str = "api"
    project: str = ""


class ExtensionsList(msgspec.Struct, tag=True, frozen=True):
    """A sanitized view of the live external modules."""
    project: str = ""


class ExtensionsValidate(msgspec.Struct, tag=True, frozen=True):
    """Validate candidate extension files without swapping the manifest.

    ``target`` names one workspace-relative file (or a path) to check; ``None``
    validates every candidate in the configured hot directories.
    """

    target: str | None = None
    project: str = ""


class ExtensionsTrash(msgspec.Struct, tag=True, frozen=True):
    """Move one managed extension file to trash, then rebuild the manifest.

    ``target`` is a workspace-relative or absolute path that must resolve to a
    discoverable candidate under ``[ext].dirs``; the facade refuses anything
    else. ``force`` keeps the trashed file even if the rebuild aborts.
    """

    target: str
    reason: str = ""
    force: bool = False
    project: str = ""


class ModelsRefresh(msgspec.Struct, tag=True, frozen=True):
    """Force a catalogue acquisition and report the resulting status."""
    project: str = ""


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
    scope: Literal["main", "subagents", "all"] = "all"
    project: str = ""


class ModelShow(msgspec.Struct, tag=True, frozen=True):
    """Resolve one model reference to its descriptive registry row."""

    ref: str
    project: str = ""


class ModelTiers(msgspec.Struct, tag=True, frozen=True):
    """The tier table: ordering, default, curated map, and user overrides."""
    project: str = ""


class ModelTierSet(msgspec.Struct, tag=True, frozen=True):
    """Settings -> Models: save the ordered models of one tier (global config).

    The first model whose provider can run is used. Returns the refreshed rows.
    ``efforts`` optionally replaces this tier's per-model effort selections;
    ``None`` preserves selections for models still in the list.
    """

    tier: str
    refs: list[str]
    efforts: dict[str, str] | None = None
    project: str = ""


class ModelTierReset(msgspec.Struct, tag=True, frozen=True):
    """Remove the user's list for ``tier`` (back to the built-in or price rule)."""

    tier: str
    project: str = ""


class AgentMaxTierSet(msgspec.Struct, tag=True, frozen=True):
    """Legacy command, refused: the configurable subagent ceiling was removed."""

    tier: str
    project: str = ""


class DefaultModelSettings(msgspec.Struct, tag=True, frozen=True):
    """Read the ordered global default model chain."""
    project: str = ""


class DefaultModelSet(msgspec.Struct, tag=True, frozen=True):
    """Atomically save the first model and its ordered global fallbacks."""

    refs: list[str]
    project: str = ""


class SessionTitleSettings(msgspec.Struct, tag=True, frozen=True):
    """Read the automatic session-title settings (Settings -> Session titles)."""
    project: str = ""


class SessionTitleSettingsSet(msgspec.Struct, tag=True, frozen=True):
    """Switch automatic titles on or off and/or choose the title model."""

    enabled: bool | None = None
    model: str | None = None
    project: str = ""


class UsageRefreshSettings(msgspec.Struct, tag=True, frozen=True):
    """Read ``[models] usage_refresh_seconds`` (Settings -> Providers)."""
    project: str = ""


class UsageRefreshSettingsSet(msgspec.Struct, tag=True, frozen=True):
    """Set how often the daemon re-reads provider plan usage (60-3600 seconds); applies at once."""

    seconds: int
    project: str = ""


class ClaudeCodeSettings(msgspec.Struct, tag=True, frozen=True):
    """Read ``[models] claude_code_main_enabled`` and ``claude_code_subagent_backend``."""
    project: str = ""


class ClaudeCodeSettingsSet(msgspec.Struct, tag=True, frozen=True):
    """Change the Claude Code settings; ``None`` leaves a field unchanged.

    ``main_enabled`` allows or hides Claude Code as a selectable main model
    (default hidden; always the Agent SDK). ``subagent_backend`` is how
    ``claude-code/<model>`` subagents run: ``"worker"`` (interactive Claude Code,
    its own tools) or ``"sdk"`` (Agent SDK in a Nexus child loop, Nexus tools).
    """

    main_enabled: bool | None = None
    subagent_backend: str | None = None
    project: str = ""


class ModelSelect(msgspec.Struct, tag=True, frozen=True):
    """Select and persist one session's model for its subsequent turns.

    ``ref`` is a tier name, ``"provider/model"``, or a bare id; it is validated
    daemon-side (a bad reference is an ``ErrorResult``). The choice is durable in
    the session log and never disturbs a turn already running.
    """

    session: str
    ref: str
    project: str = ""


class ReasoningEffortSelect(msgspec.Struct, tag=True, frozen=True):
    """Select a root-session reasoning effort for subsequent turns."""

    session: str
    effort: str | None
    project: str = ""


class AgentsList(msgspec.Struct, tag=True, frozen=True):
    """The discovered subagent definitions (sanitized index rows)."""
    project: str = ""


class AgentCurrent(msgspec.Struct, tag=True, frozen=True):
    session: str
    project: str = ""


class AgentSelect(msgspec.Struct, tag=True, frozen=True):
    session: str
    name: str
    project: str = ""


class AgentReset(msgspec.Struct, tag=True, frozen=True):
    session: str
    project: str = ""


class AgentDefaultSet(msgspec.Struct, tag=True, frozen=True):
    """Persist the root agent new sessions start with (``[agent] name``)."""

    name: str
    scope: Literal["global", "project"] = "global"
    project: str = ""


class ToolsList(msgspec.Struct, tag=True, frozen=True):
    """The model-facing tool catalog for the current config and manifest."""
    project: str = ""


class ContextExtensionSelect(msgspec.Struct, tag=True, frozen=True):
    session: str
    category: Literal["skills", "mcp", "tools"]
    name: str
    enabled: bool
    project: str = ""


class ContextMcpLoadingSelect(msgspec.Struct, tag=True, frozen=True):
    session: str
    server: str
    mode: Literal["search", "all"] | None
    project: str = ""


class ContextInspect(msgspec.Struct, tag=True, frozen=True):
    """Preview one session's next-turn standing context without starting a turn."""

    session: str
    project: str = ""


class McpServerShow(msgspec.Struct, tag=True, frozen=True):
    """Read retained MCP state without connecting or listing remote catalogs."""

    session: str
    name: str
    max_bytes: int = 262_144
    project: str = ""


class McpServerRestart(msgspec.Struct, tag=True, frozen=True):
    """Close one MCP server's connection and connect it again (the Restart button).

    The session names who asked; the server is shared by the daemon, so every
    session sees the new connection. A failed connect is reported, never raised.
    """

    session: str
    name: str
    project: str = ""


class SkillInspect(msgspec.Struct, tag=True, frozen=True):
    """Read a pinned skill snapshot without enabling or invoking it."""

    session: str
    name: str
    max_body_bytes: int = 65_536
    project: str = ""


class FileSearch(msgspec.Struct, tag=True, frozen=True):
    """Search workspace-relative file paths for composer completion."""

    query: str
    limit: int = 30
    include_directories: bool = False
    session: str = ""
    project: str = ""


class GitDiff(msgspec.Struct, tag=True, frozen=True):
    """Bounded diff for the current workspace, optionally staged or against a ref."""

    staged: bool = False
    ref: str = ""
    session: str = ""
    project: str = ""


class DirectoryList(msgspec.Struct, tag=True, frozen=True):
    """List one workspace directory for the sidebar Files tab (read-only, bounded).

    ``path`` is workspace-relative POSIX (``""`` is the root); the host refuses absolute
    paths, ``..``, hidden segments, symlink crossings and read-deny roots.
    """

    path: str = ""
    limit: int = 200
    session: str = ""
    project: str = ""


class FilePreview(msgspec.Struct, tag=True, frozen=True):
    """Read the start of one workspace file as text for the sidebar Files tab (read-only, bounded).

    ``path`` follows the ``DirectoryList`` rules and must name a regular file; ``max_bytes`` is
    1..262,144.
    """

    path: str
    max_bytes: int = 65536
    session: str = ""
    project: str = ""


class GitStatus(msgspec.Struct, tag=True, frozen=True):
    """Read-only, bounded ``git status`` of the current workspace for the sidebar Git tab."""
    session: str = ""
    project: str = ""


class WorktreeList(msgspec.Struct, tag=True, frozen=True):
    """List daemon-owned agent worktrees using sanitized metadata only."""
    project: str = ""


class WorktreeInspect(msgspec.Struct, tag=True, frozen=True):
    """Inspect one daemon-owned child worktree by its authenticated child id."""

    child_id: str
    project: str = ""


class WorktreeReview(msgspec.Struct, tag=True, frozen=True):
    """Read a bounded page from one finalized child worktree review."""

    child_id: str
    review_id: str | None = None
    cursor: int = 0
    limit: int = 1
    project: str = ""


class WorktreeAcknowledge(msgspec.Struct, tag=True, frozen=True):
    """Acknowledge the exact current finalized child review."""

    child_id: str
    review_id: str
    digest: str
    project: str = ""


class WorktreeIntegrate(msgspec.Struct, tag=True, frozen=True):
    """Preview or confirm integration of the exact acknowledged review."""

    child_id: str
    review_id: str
    digest: str
    confirmation_token: str = ""
    project: str = ""


class WorktreeDiscard(msgspec.Struct, tag=True, frozen=True):
    """Preview or confirm removal of one authenticated child worktree."""

    child_id: str
    force: bool = False
    review_id: str | None = None
    confirmation_token: str = ""
    project: str = ""


class Speak(msgspec.Struct, tag=True, frozen=True):
    """Speak only the latest completed assistant answer on the daemon host."""

    session_id: str
    download: bool = False
    project: str = ""


class SpeakStop(msgspec.Struct, tag=True, frozen=True):
    """Stop the speech that is playing now (Esc); a no-op when nothing is playing."""
    project: str = ""


class SpeakResult(msgspec.Struct, tag=True, frozen=True):
    message: str
    backend: str


class SpeechStatus(msgspec.Struct, tag=True, frozen=True):
    """Read the local speech model status (what ``/speak download`` fetches)."""
    project: str = ""


class SpeechPrepare(msgspec.Struct, tag=True, frozen=True):
    """Start the one-time speech model download (after the user consented)."""
    project: str = ""


class SpeechStatusResult(msgspec.Struct, tag=True, frozen=True):
    """``state``: unsupported (packages missing), absent, downloading, ready or error."""

    state: str
    progress: float = 0.0
    bytes_done: int = 0
    bytes_total: int = 0
    message: str = ""


class VoiceStatus(msgspec.Struct, tag=True, frozen=True):
    """Read the bounded local voice model status."""
    project: str = ""


class VoicePrepare(msgspec.Struct, tag=True, frozen=True):
    force: bool = False
    allow_download: bool = True
    project: str = ""


class VoiceTranscribe(msgspec.Struct, tag=True, frozen=True, repr_omit_defaults=True):
    audio: bytes
    request_id: str
    session: str = ""
    #: A live preview of a recording still in progress. The daemon answers
    #: ``voice_busy`` at once instead of queueing it behind other inference.
    partial: bool = False

    def __repr__(self) -> str:
        return f"VoiceTranscribe(request_id={self.request_id!r}, partial={self.partial!r}, audio=<redacted>)"
    project: str = ""


class VoiceCancel(msgspec.Struct, tag=True, frozen=True):
    request_id: str
    project: str = ""


class VoiceRemove(msgspec.Struct, tag=True, frozen=True):
    """Remove the cached local voice model."""
    project: str = ""


class LiveStatus(msgspec.Struct, tag=True, frozen=True):
    """Read the GPT Live availability and the active call (no secrets)."""
    project: str = ""


class LiveStart(msgspec.Struct, tag=True, frozen=True, repr_omit_defaults=True):
    """Start a GPT Live call in ``session``.

    ``offer_sdp`` (at most 64 KiB) comes from the client's audio helper; the
    daemon signals with OpenAI and returns only the SDP answer.
    ``new_conversation`` skips the carried-over transcript.
    """

    session: str
    offer_sdp: str
    voice: str = ""
    new_conversation: bool = False

    def __repr__(self) -> str:
        return f"LiveStart(session={self.session!r}, offer_sdp=<{len(self.offer_sdp)} chars>)"
    project: str = ""


class LiveMute(msgspec.Struct, tag=True, frozen=True):
    """Set (``True``/``False``) or toggle (``None``) the call's mute state."""

    session: str
    muted: bool | None = None
    project: str = ""


class LivePause(msgspec.Struct, tag=True, frozen=True):
    """Set (``True``/``False``) or toggle (``None``) the call's pause state (docs/live.md)."""

    session: str
    paused: bool | None = None
    project: str = ""


class LiveStop(msgspec.Struct, tag=True, frozen=True):
    """End the call. Idempotent; never cancels the agent's running turn."""

    session: str
    reason: str = "user"
    project: str = ""


class LiveHelperEvent(msgspec.Struct, tag=True, frozen=True):
    """An audio-helper event forwarded by its client: ready, levels, phase, dc_error, dropped, closed."""

    session: str
    kind: str
    detail: str = ""
    project: str = ""


class LiveConsent(msgspec.Struct, tag=True, frozen=True):
    """Read (``None``), record (``True``) or revoke (``False``) the once-per-machine GPT Live consent.

    The consent covers sending microphone audio, summaries of the agent's work and a
    bounded excerpt of the session's conversation to OpenAI through the user's ChatGPT account (docs/security.md).

    ``scope="manager"`` is the additional cross-session notice: a call on the manager
    session also sends what the manager reads of *other* sessions (titles, status, final
    reports). It needs the base consent too; recording it records both, revoking it
    keeps the base. An empty scope is the base consent.
    """

    accept: bool | None = None
    scope: Literal["", "manager"] = ""
    project: str = ""


class ManagerOpen(msgspec.Struct, tag=True, frozen=True):
    """Open or create the workspace's manager session (idempotent; docs/manager.md).

    Refused unless ``[manager] enabled`` is true. The manager is a singleton of kind
    ``manager`` in the main namespace; ``SessionList`` and the project index never list it.
    """
    project: str = ""


class ManagerState(msgspec.Struct, tag=True, frozen=True):
    """Read the manager's id, pinned model, sign-in state and managed sessions (read-only)."""
    project: str = ""


class Doctor(msgspec.Struct, tag=True, frozen=True):
    """A redacted health report: config, providers, registry, extensions, MCP.

    ``explain_reload`` adds the hot-vs-restart boundary (PLAN section 6.6).
    """

    explain_reload: bool = False
    project: str = ""


class UpdateStatus(msgspec.Struct, tag=True, frozen=True):
    """Is a newer release available? Answered from the daemon's cached check."""

    #: Claim the one-time announcement of the available release (a client toast).
    announce: bool = False


class Health(msgspec.Struct, tag=True, frozen=True):
    """Daemon-level liveness and scheduling counters."""


class MockList(msgspec.Struct, tag=True, frozen=True):
    """List the mock scenarios (dev mode only; MOCK_PLAN §4.1)."""
    project: str = ""


class MockStart(msgspec.Struct, tag=True, frozen=True):
    """Start a scripted mock scenario in a new session (dev mode only).

    ``speed`` scales streaming/tool pacing (``0`` is instant); ``seed`` fixes
    jitter. ``session`` may name the session; the host generates one otherwise.
    """

    scenario: str
    speed: float = 1.0
    seed: int = 0
    session: str = ""
    project: str = ""


class MockClean(msgspec.Struct, tag=True, frozen=True):
    """Restore the dev sandbox workspace to its seeded state (dev mode only)."""
    project: str = ""


class Shutdown(msgspec.Struct, tag=True, frozen=True):
    reason: str = ""
    force: bool = False


class WebLaunch(msgspec.Struct, tag=True, frozen=True):
    """Ask the local daemon to start its browser surface and issue a ticket.

    This command is intercepted by the authenticated Unix-socket transport and
    is intentionally unavailable through the HTTP peer API.
    """


#: The complete command union. ``msgspec`` decodes it by the ``type`` tag.
Command = (
    ProjectFocus
    | ProjectOpen
    | ProjectList
    | ProjectUnload
    | ProjectReload
    | SessionList
    | SessionTree
    | SessionResolve
    | SessionInspect
    | SessionHandoff
    | SessionHandoffRead
    | ProjectSessionsList
    | ProjectSessionOpen
    | SettingsInventory
    | SettingsRead
    | SettingsMcpLoadingSet
    | SettingsMcpEnabledSet
    | SettingsMcpAdd
    | SettingsWrite
    | SettingsDelete
    | SettingsReset
    | SkillsInstall
    | SetupStatus
    | SetupSave
    | ProvidersStatus
    | ProviderLogin
    | ProviderLoginPoll
    | ProviderLoginCancel
    | ProviderKeySet
    | ProviderLogout
    | ProviderModelsRefresh
    | ProviderLoginCode
    | ProvidersUsage
    | SessionArchive
    | SessionUnarchive
    | SessionListArchived
    | SessionPreview
    | SessionSearch
    | SessionOpen
    | SessionWorktreeSet
    | SessionPromote
    | SessionDetach
    | SessionWorktreeStatus
    | SessionWorktreeTrust
    | AttachmentPrepare
    | AttachmentPreview
    | SessionStart
    | SessionEnqueue
    | SessionShell
    | SessionJobs
    | SessionJobLog
    | SessionJobStop
    | SessionJobBackground
    | SessionCancel
    | SessionQueueMove
    | SessionQueueRemove
    | SessionQueueSetMode
    | SessionSubscribe
    | SessionFocus
    | SessionState
    | SessionActivity
    | LogsRead
    | AgentTranscript
    | AgentCancel
    | AgentClose
    | SessionFork
    | SessionDelete
    | SessionRestore
    | SessionRename
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
    | ClaudeCodeSettings
    | ClaudeCodeSettingsSet
    | UsageRefreshSettings
    | UsageRefreshSettingsSet
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
    | DirectoryList
    | FilePreview
    | GitStatus
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
    | LiveStatus
    | LiveStart
    | LiveMute
    | LivePause
    | LiveStop
    | LiveHelperEvent
    | LiveConsent
    | ManagerOpen
    | ManagerState
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
    ProjectFocus,
    ProjectOpen,
    ProjectList,
    ProjectUnload,
    ProjectReload,
    SessionList,
    SessionTree,
    SessionResolve,
    SessionInspect,
    SessionHandoff,
    SessionHandoffRead,
    SettingsInventory,
    SettingsRead,
    SettingsMcpLoadingSet,
    SettingsMcpEnabledSet,
    SettingsMcpAdd,
    SettingsWrite,
    SettingsDelete,
    SettingsReset,
    SkillsInstall,
    SetupStatus,
    SetupSave,
    ProvidersStatus,
    ProviderLogin,
    ProviderLoginPoll,
    ProviderLoginCancel,
    ProviderKeySet,
    ProviderLogout,
    ProviderModelsRefresh,
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
    SessionJobs,
    SessionJobLog,
    SessionJobStop,
    SessionJobBackground,
    SessionCancel,
    SessionQueueMove,
    SessionQueueRemove,
    SessionQueueSetMode,
    SessionSubscribe,
    SessionFocus,
    SessionState,
    SessionActivity,
    LogsRead,
    AgentTranscript,
    AgentCancel,
    AgentClose,
    SessionFork,
    SessionDelete,
    SessionRestore,
    SessionRename,
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
    ClaudeCodeSettings,
    ClaudeCodeSettingsSet,
    UsageRefreshSettings,
    UsageRefreshSettingsSet,
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
    DirectoryList,
    FilePreview,
    GitStatus,
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
    LiveStatus,
    LiveStart,
    LiveMute,
    LivePause,
    LiveStop,
    LiveHelperEvent,
    LiveConsent,
    ManagerOpen,
    ManagerState,
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
    #: ``SettingsMcpAdd`` only: ``status`` is written | exists | conflict | legacy | preview; a human note.
    message: str = ""
    #: ``SettingsMcpAdd(dry_run=True)`` only: ``transport`` is stdio | http | sse ...; ``lines`` are masked
    #: (no env/header value, no URL credential).
    transport: str = ""
    lines: list[str] = msgspec.field(default_factory=list)


class SettingsResetResult(msgspec.Struct, tag=True, frozen=True):
    status: str
    trash_ids: list[str] = msgspec.field(default_factory=list)


class SettingsDeleteResult(msgspec.Struct, tag=True, frozen=True):
    status: str
    trash_id: str = ""


class SkillsInstallResult(msgspec.Struct, tag=True, frozen=True):
    """``status``: installed | partial | conflict | nothing | failed | unavailable | timeout | busy."""

    status: str
    installed: list[str] = msgspec.field(default_factory=list)
    conflicts: list[str] = msgspec.field(default_factory=list)
    skipped: list[str] = msgspec.field(default_factory=list)
    failed: list[str] = msgspec.field(default_factory=list)
    reload_failed: list[str] = msgspec.field(default_factory=list)
    message: str = ""


class SetupStatusResult(msgspec.Struct, tag=True, frozen=True):
    required: bool
    global_model: str = ""
    effective_model: str = ""
    providers: list[dict[str, Any]] = msgspec.field(default_factory=list)


class SetupSaveResult(msgspec.Struct, tag=True, frozen=True):
    global_model: str
    restart_required: bool = True


class ProvidersStatusResult(msgspec.Struct, tag=True, frozen=True):
    """Rows: ``id``, ``label``, ``methods``, ``help``, ``connected``, ``detail``, ``login``.

    A ``methods`` entry of ``cli`` marks a manual, provider-owned sign-in (the
    Antigravity ``agy`` CLI): Nexus starts no login flow, reads no credential and
    never signs it out, so ``login`` stays ``None`` and ``can_logout`` is false.
    Such a row also carries ``cli_present`` and ``auth_verified``; presence of the
    CLI is not proof of sign-in, so ``connected`` and ``auth_verified`` stay
    ``False`` and ``detail`` says to run ``agy`` in a terminal.
    """

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


class ProviderModelsRefreshResult(msgspec.Struct, tag=True, frozen=True):
    """``count`` models in the refreshed list, ``refreshed_at`` (epoch seconds, 0 = never), redacted ``error``."""

    provider: str
    count: int = 0
    refreshed_at: float = 0.0
    error: str = ""
    message: str = ""


class SessionHandoffResult(msgspec.Struct, tag=True, frozen=True):
    data: dict[str, Any]


class SessionOpenResult(msgspec.Struct, tag=True, frozen=True):
    session: SessionSummary


class SessionWorktreeResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    state: dict[str, Any] = msgspec.field(default_factory=dict)


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


class JobSummary(msgspec.Struct, frozen=True):
    """One shell job as the host shows it; no output, environment or file path."""

    job_id: str
    command: str = ""
    cwd: str = ""
    status: str = "running"  # running|completed|failed|timed_out|killed
    background: bool = False
    pid: int | None = None
    exit_code: int | None = None
    signal: int | None = None
    started_at: float = 0.0
    finished_at: float | None = None
    log_bytes: int = 0
    log_lines: int = 0
    stderr_bytes: int = 0
    #: ``SessionJobLog`` can read something (a log file was written).
    log_available: bool = False
    #: A tool call is awaiting it in the foreground (``SessionJobBackground`` applies).
    can_background: bool = False
    #: ``tool`` (the ``shell`` tool) or ``user`` (a composer ``!`` run).
    source: str = "tool"
    #: A background job with no lifetime limit that runs until stopped.
    persistent: bool = False


class SessionJobsResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    jobs: list[JobSummary] = msgspec.field(default_factory=list)
    running: int = 0
    truncated: bool = False


class SessionJobLogResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    job_id: str
    #: ``ok`` | ``unknown`` (no such job in this session) | ``unavailable`` (log gone).
    status: str = "ok"
    text: str = ""
    start: int = 0
    end: int = 0
    size: int = 0
    next_offset: int | None = None
    message: str = ""


class SessionJobStopResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    job_id: str
    #: ``killed`` | ``finished`` (already over) | ``unknown``.
    outcome: str
    exit_code: int | None = None
    signal: int | None = None


class SessionJobBackgroundResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    job_id: str
    #: ``requested`` | ``finished`` | ``already_background`` | ``not_foreground`` | ``unknown``.
    outcome: str
    requested: bool = False


class SessionCancelResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    cancelled: bool = False
    dropped: int = 0
    returned_messages: list[str] = msgspec.field(default_factory=list)


class SessionQueueEditResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    #: False when the message already ran, was dropped, or sits at the edge.
    changed: bool = False
    #: Content blocks of a removed message (``SessionQueueRemove.return_content``).
    content: list[dict[str, Any]] = msgspec.field(default_factory=list)


class SessionSubscribeResult(msgspec.Struct, tag=True, frozen=True):
    """Names the stream a transport attaches to; events follow out of band."""

    session: str
    subscribed: bool = True
    from_seq: int = 0


class SessionStateResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    seq: int = 0
    view: dict[str, Any] = msgspec.field(default_factory=dict)


class SessionActivityResult(msgspec.Struct, tag=True, frozen=True):
    """What keeps a session busy after its turn ended (plan 3.8).

    ``wait_seconds`` is the effective ``[agents] oneshot_wait_seconds``: how long a
    one-shot run may follow the session; ``0`` keeps the old exit-at-first-terminal.
    """

    session: str
    turn_active: bool = False
    queued: int = 0
    running_agents: int = 0
    reports_pending: int = 0
    wait_seconds: int = 0


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


class AgentCancelResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    agent_id: str
    #: True when a running child was signalled; False when it was not running.
    cancelled: bool = False
    message: str = ""


class AgentCloseResult(msgspec.Struct, tag=True, frozen=True):
    session: str
    agent_id: str
    #: True when the child is now closed (including one that already was).
    closed: bool = False
    message: str = ""


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


class SessionRenameResult(msgspec.Struct, tag=True, frozen=True):
    session: SessionSummary


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


class UsageRefreshSettingsResult(msgspec.Struct, tag=True, frozen=True):
    """The effective interval and its bounds."""

    seconds: int = 300
    minimum: int = 60
    maximum: int = 3600


class ClaudeCodeSettingsResult(msgspec.Struct, tag=True, frozen=True):
    """``message`` explains scope: main selection is SDK-only; subagents follow ``subagent_backend``."""

    main_enabled: bool = False
    message: str = ""
    subagent_backend: str = "worker"
    #: Shown beside the backend choice: what each backend does to authority.
    backend_message: str = ""
    restart_required: bool = False


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
    default: str = "orchestrator"


class AgentCurrentResult(msgspec.Struct, tag=True, frozen=True):
    """Current root-agent metadata for the session's next turn.

    ``reasoning_effort`` is the effort actually applied to the root turn;
    supported levels, the stored session override, and its source describe the
    Ctrl+T selection state.
    """

    session: str
    name: str = "orchestrator"
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
    name: str = "orchestrator"
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


class DirectoryListResult(msgspec.Struct, tag=True, frozen=True):
    #: The canonical listed path (``""`` for the root).
    path: str = ""
    #: ``{name, path, kind: dir|file|link, size}``; directories first, hidden entries omitted.
    entries: list[dict[str, Any]] = msgspec.field(default_factory=list)
    truncated: bool = False


class FilePreviewResult(msgspec.Struct, tag=True, frozen=True):
    #: The canonical workspace-relative path.
    path: str = ""
    #: ``text``, ``binary`` (nothing shown) or ``empty``.
    status: str = "text"
    #: Redacted UTF-8 start of the file (``text`` status only).
    text: str = ""
    #: File size in bytes and the bytes of it that ``text`` covers.
    size: int = 0
    shown: int = 0
    #: True when the file is longer than what was read.
    truncated: bool = False
    message: str = ""


class GitStatusResult(msgspec.Struct, tag=True, frozen=True):
    #: False outside a Git work tree (``message`` says why).
    available: bool = False
    message: str = ""
    branch: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    #: ``{group: staged|unstaged|untracked|conflicts, code, label, path, from}`` rows, bounded.
    rows: list[dict[str, Any]] = msgspec.field(default_factory=list)
    counts: dict[str, int] = msgspec.field(default_factory=dict)
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


class LiveStatusResult(msgspec.Struct, tag=True, frozen=True):
    """Redacted Live state. ``reason``: disabled|unsupported|no_credential|busy (empty when available)."""

    available: bool = True
    reason: str = ""
    state: str = "idle"  # idle|connecting|active|muted|paused|closing
    phase: str = ""
    active_session: str = ""
    call_id: str = ""
    voice: str = ""
    muted: bool = False
    #: Paused (docs/live.md "Pause vs stop"); ``pause_remaining_s`` counts down to ``pause_timeout``.
    paused: bool = False
    pause_remaining_s: float = 0.0
    elapsed_s: float = 0.0
    max_minutes: int = 30
    levels_in: float = 0.0
    levels_out: float = 0.0
    dropped_frames: int = 0
    last_error: str = ""
    #: Whether the user has accepted the GPT Live data-sharing notice on this machine.
    consent: bool = False


class LiveConsentResult(msgspec.Struct, tag=True, frozen=True):
    """``accepted``/``accepted_at`` answer for the requested ``scope`` (the manager scope needs both)."""

    accepted: bool = False
    accepted_at: str = ""
    scope: str = ""


class ManagerOpenResult(msgspec.Struct, tag=True, frozen=True):
    session: SessionSummary
    #: True only for the call that created the session; every later call is False.
    created: bool = False


class ManagerStateResult(msgspec.Struct, tag=True, frozen=True):
    """The manager's state. Never an error for a disabled or not-yet-created manager.

    ``session`` is ``""`` until ``ManagerOpen`` created it. ``model`` is the pinned
    route (no fallback of any kind). ``signed_in`` says whether Codex (ChatGPT), which
    runs the manager, is connected; ``sign_in`` then names the exact fix. ``live_ready``
    is the GPT Live side: Codex connected and the base and cross-session consents given.
    """

    enabled: bool = False
    session: str = ""
    state: str = ""
    model: str = ""
    provider: str = ""
    signed_in: bool = False
    sign_in: str = ""
    codex_signed_in: bool = False
    live_consent: bool = False
    live_manager_consent: bool = False
    live_ready: bool = False
    #: Ids of the roots this manager launched (newest last), bounded.
    managed: list[str] = msgspec.field(default_factory=list)
    max_launches_per_turn: int = 3
    max_live_managed: int = 8


class LiveStartResult(msgspec.Struct, tag=True, frozen=True):
    """The SDP answer for the client's peer; never a credential."""

    call_id: str
    answer_sdp: str
    voice: str = ""


class LiveHelperEventResult(msgspec.Struct, tag=True, frozen=True):
    ok: bool = True
    state: str = "idle"


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
    projects: list[ProjectRow] = msgspec.field(default_factory=list)
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
    #: Running persistent ``shell`` jobs (``nexus daemon status``): the total and
    #: a bounded list of ``{session, job_id, command, cwd, pid, started_at}``.
    persistent_jobs_total: int = 0
    persistent_jobs: list[dict[str, Any]] = msgspec.field(default_factory=list)


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
    ProjectFocusResult
    | ProjectOpenResult
    | ProjectListResult |
    ProjectSessionsListResult
    | ProjectSessionOpenResult
    | SettingsInventoryResult
    | SettingsReadResult
    | SettingsWriteResult
    | SettingsDeleteResult
    | SettingsResetResult
    | SkillsInstallResult
    | SetupStatusResult
    | SetupSaveResult
    | ProvidersStatusResult
    | ProviderLoginResult
    | ProviderAuthResult
    | ProviderModelsRefreshResult
    | ProvidersUsageResult
    | SessionListResult
    | SessionTreeResult
    | SessionResolveResult
    | SessionInspectResult
    | SessionHandoffResult
    | SessionArchiveResult
    | SessionUnarchiveResult
    | SessionListArchivedResult
    | SessionPreviewResult
    | SessionSearchResult
    | SessionOpenResult
    | SessionWorktreeResult
    | AttachmentPrepareResult
    | AttachmentPreviewResult
    | SessionStartResult
    | SessionEnqueueResult
    | SessionShellResult
    | SessionJobsResult
    | SessionJobLogResult
    | SessionJobStopResult
    | SessionJobBackgroundResult
    | SessionCancelResult
    | SessionQueueEditResult
    | SessionSubscribeResult
    | SessionFocusResult
    | SessionStateResult
    | SessionActivityResult
    | LogsReadResult
    | AgentTranscriptResult
    | AgentCancelResult
    | AgentCloseResult
    | SessionForkResult
    | SessionDeleteResult
    | SessionRestoreResult
    | SessionRenameResult
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
    | ClaudeCodeSettingsResult
    | UsageRefreshSettingsResult
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
    | DirectoryListResult
    | FilePreviewResult
    | GitStatusResult
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
    | LiveStatusResult
    | LiveStartResult
    | LiveHelperEventResult
    | LiveConsentResult
    | ManagerOpenResult
    | ManagerStateResult
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
    ProjectFocusResult,
    ProjectOpenResult,
    ProjectListResult,
    SettingsInventoryResult,
    SettingsReadResult,
    SettingsWriteResult,
    SettingsDeleteResult,
    SettingsResetResult,
    SkillsInstallResult,
    SetupStatusResult,
    SetupSaveResult,
    ProvidersStatusResult,
    ProviderLoginResult,
    ProviderAuthResult,
    ProviderModelsRefreshResult,
    ProvidersUsageResult,
    SessionListResult,
    SessionTreeResult,
    SessionResolveResult,
    SessionInspectResult,
    SessionHandoffResult,
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
    SessionJobsResult,
    SessionJobLogResult,
    SessionJobStopResult,
    SessionJobBackgroundResult,
    SessionCancelResult,
    SessionQueueEditResult,
    SessionSubscribeResult,
    SessionFocusResult,
    SessionStateResult,
    SessionActivityResult,
    LogsReadResult,
    AgentTranscriptResult,
    AgentCancelResult,
    AgentCloseResult,
    SessionForkResult,
    SessionDeleteResult,
    SessionRestoreResult,
    SessionRenameResult,
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
    ClaudeCodeSettingsResult,
    UsageRefreshSettingsResult,
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
    DirectoryListResult,
    FilePreviewResult,
    GitStatusResult,
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
    LiveStatusResult,
    LiveStartResult,
    LiveHelperEventResult,
    LiveConsentResult,
    ManagerOpenResult,
    ManagerStateResult,
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
    "AgentCancel",
    "AgentCancelResult",
    "AgentClose",
    "AgentCloseResult",
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
    "DirectoryList",
    "DirectoryListResult",
    "FilePreview",
    "FilePreviewResult",
    "GitStatus",
    "GitStatusResult",
    "Health",
    "HealthResult",
    "LogEntry",
    "LogsRead",
    "LogsReadResult",
    "ManagerOpen",
    "ManagerOpenResult",
    "ManagerState",
    "ManagerStateResult",
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
    "ClaudeCodeSettings",
    "ClaudeCodeSettingsResult",
    "ClaudeCodeSettingsSet",
    "UsageRefreshSettings",
    "UsageRefreshSettingsResult",
    "UsageRefreshSettingsSet",
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
    "ProviderModelsRefresh",
    "ProviderModelsRefreshResult",
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
    "SessionQueueSetMode",
    "SessionDelete",
    "SessionDeleteResult",
    "SessionEnqueue",
    "SessionEnqueueResult",
    "SessionShell",
    "SessionShellResult",
    "JobSummary",
    "SessionJobBackground",
    "SessionJobBackgroundResult",
    "SessionJobLog",
    "SessionJobLogResult",
    "SessionJobStop",
    "SessionJobStopResult",
    "SessionJobs",
    "SessionJobsResult",
    "SessionExport",
    "SessionExportResult",
    "SessionFork",
    "SessionForkResult",
    "SessionList",
    "SessionTree",
    "SessionResolve",
    "SessionResolveResult",
    "SessionTreeResult",
    "SessionInspect",
    "SessionInspectResult",
    "SessionHandoff",
    "SessionHandoffRead",
    "SessionHandoffResult",
    "SessionListArchived",
    "SessionListArchivedResult",
    "SessionListResult",
    "SessionLogPage",
    "SessionOpen",
    "SessionOpenResult",
    "SessionWorktreeSet",
    "SessionPromote",
    "SessionDetach",
    "SessionWorktreeStatus",
    "SessionWorktreeTrust",
    "SessionWorktreeResult",
    "SessionPreview",
    "SessionPreviewResult",
    "SessionRestore",
    "SessionRestoreResult",
    "SessionRename",
    "SessionRenameResult",
    "SessionSearch",
    "SessionSearchResult",
    "SessionStart",
    "SessionStartResult",
    "SessionActivity",
    "SessionActivityResult",
    "SessionState",
    "SessionStateResult",
    "SessionSubscribe",
    "SessionSubscribeResult",
    "SessionUnarchive",
    "SessionUnarchiveResult",
    "SettingsCategory",
    "SettingsReset",
    "SettingsResetResult",
    "SkillsInstall",
    "SkillsInstallResult",
    "SettingsDelete",
    "SettingsDeleteResult",
    "SettingsInventory",
    "SettingsInventoryResult",
    "SettingsItem",
    "SettingsRead",
    "SettingsReadResult",
    "SettingsMcpLoadingSet",
    "SettingsMcpEnabledSet",
    "SettingsMcpAdd",
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
    "LiveConsent",
    "LiveConsentResult",
    "LiveHelperEvent",
    "LiveHelperEventResult",
    "LiveMute",
    "LivePause",
    "LiveStart",
    "LiveStartResult",
    "LiveStatus",
    "LiveStatusResult",
    "LiveStop",
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
