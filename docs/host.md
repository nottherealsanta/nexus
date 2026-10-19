# Host: daemon, protocol, facade, transports

The optional device gateway is separate from and does not change the host
service. See [gateway documentation](gateway.md) before enabling it.

`nexus/host/` (L4½) is the transport-neutral surface over one `Runtime`. **A UI
may use it and nothing else** (`nexus.host`, plus the pure `view`, `events`,
`client`, `host_support`, `ui_support` helpers).

## Files

| File | Owns |
| --- | --- |
| `protocol.py` | the wire contract: frozen, tagged `msgspec` commands and `*Result` structs; `PROTOCOL_VERSION` (3); `decode_command`; `ErrorResult` |
| `facade.py` | `HostFacade`: implements every command (`handle`), `subscribe`, `state`, `web_snapshot`, `subscribe_web`, `subscribe_workspace` |
| `daemon.py` | `Daemon`: lifecycle, socket, handshake, idle shutdown, `web_launch`, `ensure_daemon`/`status`/`stop`/`logs`, daemon entrypoint |
| `supervisor.py` | `Supervisor`: turn scheduling under a global cap |
| `presence.py` | `Presence`: attached views and first-responder approval leases |
| `transports/__init__.py` | shared frames (`Hello`, `Welcome`, `CommandFrame`, `ResultFrame`, `EventFrame`, `Ping`/`Pong`, `Reject`, `Bye`, `SubscribeDone`, `Unsubscribe`), `MAX_FRAME_BYTES` (16 MiB), error taxonomy |
| `transports/uds.py` | client half of the Unix-socket link (`UDSClient.connect(...).call(cmd)`) |
| `transports/http_sse.py` | `HTTPSSEServer`: commands as POST, events as SSE; dispatches browser routes |
| `web.py` | `BrowserRoutes`: tickets, cookies, CSRF, static files, `/v1/web/*` |
| `doctor.py`, `diagnostics.py`, `session_diagnostics.py` | compatibility import paths for `host_support` / `observability` |

## Commands

The protocol carries no credential, environment value or raw config back to a
client. `ProviderKeySet` and `ProviderLoginCode` (a Claude sign-in code) are the
inward-only exceptions. Errors are
`ErrorResult(kind, message)` with the message redacted. Groups (full list in
`protocol.py`):

| Group | Commands |
| --- | --- |
| Sessions | `SessionList` `ProjectSessionsList` `ProjectSessionOpen` `SessionOpen` `SessionStart` `SessionEnqueue` `SessionShell` `SessionQueueMove` `SessionQueueRemove` `SessionQueueSetMode` `SessionCancel` `SessionSubscribe` `SessionState` `SessionFork` `SessionDelete` `SessionRestore` `SessionExport` `SessionArchive` `SessionUnarchive` `SessionListArchived` `SessionSearch` `SessionPreview` |
| Approvals | `PermissionResolve` `QuestionAnswer` |
| Models | `ModelsList` `ModelShow` `ModelTiers` `ModelTierSet` `ModelTierReset` `AgentMaxTierSet` `ModelsRefresh` `ModelSelect` `ReasoningEffortSelect` |
| Session titles | `SessionTitleSettings` `SessionTitleSettingsSet` |
| Claude Code | `ClaudeCodeSettings` `ClaudeCodeSettingsSet` |
| Attachments | `AttachmentPrepare` `AttachmentPreview` |
| Speech | `Speak` `SpeakStop` `SpeechStatus` `SpeechPrepare` |
| Agents | `AgentsList` `AgentCurrent` `AgentSelect` `AgentReset` `AgentDefaultSet` `AgentTranscript` `AgentCancel` |
| Context and tools | `ContextInspect` `ContextMcpLoadingSelect` `ContextExtensionSelect` `ToolsList` |
| Extensions | `ExtensionsReload` `ExtensionsList` `ExtensionsValidate` `ExtensionsTrash` |
| Settings and setup | `SettingsInventory` `SettingsRead` `SettingsMcpLoadingSet` `SettingsWrite` `SettingsReset` `SettingsDelete` `SetupStatus` `SetupSave` |
| Providers | `ProvidersStatus` `ProviderLogin` `ProviderLoginPoll` `ProviderLoginCode` `ProviderLoginCancel` `ProviderKeySet` `ProviderLogout` `ProviderModelsRefresh` (agy model-list probe; result `ProviderModelsRefreshResult`) `ProvidersUsage` |
| Worktrees | `WorktreeList` `WorktreeInspect` `WorktreeReview` `WorktreeAcknowledge` `WorktreeIntegrate` `WorktreeDiscard` |
| Workspace | `FileSearch` `GitDiff` `LogsRead` |
| Voice | `VoiceStatus` `VoicePrepare` `VoiceTranscribe` `VoiceCancel` `VoiceRemove` |
| Live (GPT Live voice calls) | `LiveStatus` `LiveStart` `LiveMute` `LivePause` `LiveStop` `LiveHelperEvent` `LiveConsent` (`scope` `""` or `"manager"`) ([live.md](live.md#host-commands)) |
| Manager (off by default, `[manager] enabled`) | `ManagerOpen` (idempotent open-or-create of the singleton manager session) `ManagerState` (read-only: id, pinned model, Copilot/Codex sign-in, Live consents, managed roots, caps) ([manager.md](manager.md)) |
| Daemon | `Doctor` `Health` `UpdateStatus` `Shutdown` `WebLaunch` |
| Dev mode | `MockList` `MockStart` `MockClean` (error outside dev mode) |

The manager session (kind `manager`, id `nexus-manager`) is never listed by
`SessionList`, `ProjectSessionsList` or the archive commands; `ManagerState` returns its id.
`archive`, `delete`, `fork`, `SessionShell`, `ModelSelect`, `ReasoningEffortSelect`,
`AgentSelect`/`AgentReset`, `SessionHandoff` and the skill/MCP selectors refuse it, and every
operation on it fails while `[manager] enabled = false`.

The browser may send any command except `Shutdown` and `WebLaunch`.
Streaming is the one verb a request/response cannot model: `SessionSubscribe`
names it and the transport attaches through `HostFacade.subscribe`.

### Session context selection

`ContextExtensionSelect(session, category, name, enabled)` accepts `tools`,
`skills`, and `mcp`. Names must exist in the current context inspection; tool
and MCP names match exactly, while skill names are case-insensitive. Selection
returns the refreshed `ContextInspectResult` and persists a
`context.extension_selected` event. It is refused while a turn is active or
once the session has its first `turn.started` event.

### Skill and MCP rows in the context preview

Each `skills_index` row also carries `frontmatter` (flat, ≤ 32 keys, ≤ 300 chars per
value), `index_line` (the exact line in this request, empty when off or dropped),
`context_tokens` (the index line: the cost now), `skill_tokens` (recorded
`SKILL.md` size ÷ 4: no file is read during preview) and `resources` (a count).
Each `mcp_servers` row also carries `context_tokens` (its index lines plus, when
`tool_loading` is `all` and the server is on, its schemas), `resource_count`,
`prompt_count` and a redacted `error` (≤ 300 chars). Clients use these for the
Skills and MCP cards; the pages use `SkillInspect` and `McpServerShow`
(`client.skill_inspect`, `client.mcp_server_show`).

### Read-only skill inspection

`SkillInspect(session, name, max_body_bytes=65536)` reads an individual skill,
including disabled skills, from a pinned manifest snapshot. `SkillInspectResult`
reports `status`/`error`, `manifest_generation`, enabled state, scope, origin,
metadata, `frontmatter_text`, and a redacted body. The body limit is in UTF-8
bytes (0 through 262144); `truncated` explicitly marks omitted display text and
`body_bytes` gives the original snapshot size. Missing sessions, skills, or
source snapshots and invalid limits return explicit errors. Inspection never
enables, invokes, activates, or recovers a session.

`frontmatter_text` is the original declaration payload, redacted for display;
it is not reconstructed metadata or a complete source file with delimiters.
Both declaration and body come from refresh-time bytes, not current disk reads.

### MCP restart

`McpServerRestart(session, name)` returns `McpServerRestartResult(name, status,
error)` (`client.mcp_server_restart`). The host closes that server's connection
(`MCPManager.disconnect`, reason `restart`) and connects it again; a failed connect
is recorded as server health and returned as `status="failed"` with a redacted
`error`, never raised. The connection is daemon-wide, so every session sees it.
An unknown name returns `error="Unknown MCP server"`. To pick up servers added to
`mcp.json`, clients use `ExtensionsReload`, which reconciles the definition set.

### Read-only MCP detail

`McpServerShow(session, name, max_bytes=262144)` returns `McpServerShowResult`.
`Client.mcp_server_show(...)` exposes the same request to local and remote
clients. This reads existing manager snapshots only: it does not call runtime
startup, open/unarchive a session, connect a server, list remote catalogs, or
change selection/loading. A live session's existing loading/selection overrides
are read when available; unavailable per-tool sent state is labeled, not inferred.

The result contains status, scope when known, effective enabled/loading state,
safe transport summary, stored server info/instructions, full input schemas,
resources/templates and prompts in `detail`. Unknown servers or absent manager
state return an explicit error. Strings (including errors and schema contents)
are redacted using configured MCP secrets and the general display redactor.
Transport summaries omit endpoint URLs, headers, environment, argument contents,
and working directories. `max_bytes` accepts 0 through 1048576 UTF-8 bytes.
Oversized detail becomes a labeled JSON preview with `clipped=true`; increase the
limit to retrieve complete schemas. Missing retained metadata is explicitly
unavailable; no remote fetch is attempted to fill it in. Resource, template and
prompt catalogs each have a 256-entry cap and an explicit per-catalog clipping
flag. Summary fields are also exposed at the result's top level when the complete
snapshot fits; `detail` carries availability notices and resource templates.

### Adding a capability a UI can use

1. Add `FooCommand` and `FooResult` to `protocol.py` and register both in the
   command/result unions (bump `PROTOCOL_VERSION` for an incompatible change).
2. Handle it in `HostFacade._dispatch` (or a `host_support/` dispatcher it
   delegates to). Return redacted, bounded data.
3. Expose it on `client/protocol.py` if the TUI or CLI needs it; the browser calls
   `api.command({type: 'FooCommand', …})` directly.
4. Tests: `tests/test_host_facade.py` style, `tests/test_web_transport.py` if the
   browser uses it. Test doubles reject unknown commands, so add a fake response
   in any UI test transport.
5. Put read-only projections in `host_support/`, not `host/`.

## Facade behavior worth knowing

- `start_turn` → `Supervisor.submit` → `Session.start_turn`. `enqueue` persists
  the submission first (`input.queued`), then the supervisor decides *when*, so
  the global cap applies to queued work; `mode` is `queue`, `steer` or
  `interrupt`.
- `subscribe(session, from_seq)` registers a view in `Presence`, catches up from
  the log and follows live; closing it frees the view's approval leases.
- Session summaries retain `last_seq` for stream cursors and expose
  `completion_seq` separately for unread/done status. The latter advances only
  on durable `turn.completed`, `turn.failed` or `turn.cancelled` records; presence
  events emitted when a view closes do not retrigger completion indicators.
- `state` folds the log through `view.reduce` into a baseline plus `seq`.
- `web_snapshot` / `subscribe_web` produce the versioned browser snapshot and
  JSON-Pointer patches (`host_support/browser_view.py`).
- `delete` moves a session to trash and never cancels a running turn.
- `doctor` is a redacted report over config, providers, registry, extensions and
  MCP; it performs no model request. It also carries `git` (`root`, `branch`,
  `detached`, `worktree`, `worktree_name`; `{}` outside a repository), read
  without subprocesses by `host_support/git_head.py`, for the top-bar breadcrumb.
  Its database section lists the 10 largest session logs of the project (record
  count and total body bytes, computed in SQL; no content, titles or paths),
  cached for `HEAVY_TTL` across namespaces.

## Supervisor and presence

`Supervisor`: a global cap on running turns (`DEFAULT_MAX_CONCURRENT_TURNS` = 4:
"five sessions × parallel tools × subagents is a fork bomb"), a per-session FIFO,
round-robin fairness across sessions, cancellation that drops that session's
queue, and **independence from views**: scheduling never consults a subscriber
count, so a turn with no viewer runs to completion.

**`slot_wait`.** When every slot is busy and a session has parked submissions but no
turn of its own in flight, `Supervisor.slot_wait(session)` is
`{"waiting": n_parked, "running": n, "limit": max_concurrent}`, else `None` (a message
behind the session's own running turn is an ordinary queue entry, not a slot wait).
It rides every `daemon.session_*` event of that session (`queued`, `scheduled`,
`cancelled`, `failed`) and a dedicated `daemon.session_slot` sent when a parked queue
is cancelled and as the first frame of every follow subscription, so a reconnecting
client learns the state from supervisor state rather than only from past events.
`SessionController.slot_wait` mirrors it. No new command: it is derived, never durable.

**`lent_slot`.** `Supervisor.lent_slot(session)` is an async context manager for a turn that
only waits on other sessions (the manager's `session_wait`, [manager.md](manager.md)). Inside
it the session stays active but does not count in `slots_used`, and parked turns start at
once; on exit it takes the slot back without queueing, so it may exceed the cap by one turn
until another turn ends, and nothing new starts while `slots_used` is at or over the cap.

`Presence`: a subscriber *count*, not identity. `attended` = any view attached;
a view that disconnects mid-turn applies the session's unattended policy instead
of blocking forever. `claim`/`release` implement first-responder leases for
approvals (the session's `resolve_permission` is the final arbiter); a lease held
by a dead view is released.

## Daemon

One daemon per workspace, addressed by a deterministic socket
`~/.nexus/daemon/<hash>.sock` (mode `0600` in a `0700` dir; a private short
fallback dir when the path exceeds the ~104-byte UDS limit on macOS).

- `flock` on a sibling lock plus a pid file prevent duplicates; a stale socket
  from a dead daemon is reclaimed under the lock.
- **Handshake:** `Hello` → `Welcome`; a client built for another
  `PROTOCOL_VERSION` is rejected (`VersionMismatch`), never retried.
- **Auto-start:** `ensure_daemon` spawns one and waits for readiness (10s).
- Each runtime captures its supplied environment (or the process environment)
  at construction. Later caller mapping or process-environment changes do not
  alter its configuration/provider inputs or shell job inheritance.
- **Idle shutdown:** exit after 300s with no viewers, no running and no queued
  turn; never with a turn in flight.
- SIGINT/SIGTERM stop the accept loop and close every session through the facade.
  Every log line and peer-visible error passes `redact_secrets`. ≤ 64
  subscriptions per client.
- **Command dispatch:** a connection's read loop awaits each command, so ordering
  is preserved, except `SLOW_COMMANDS` (`Doctor`, `UpdateStatus`, `ModelsList`,
  `ModelsRefresh`, `SetupStatus`), which run as background tasks (at most 8 per
  client, cancelled on teardown) and reply by frame id, possibly out of order. The
  chat client sends `Doctor` at startup; on a large database it takes seconds and
  used to hold up an immediate session open on the same connection.
- CLI: `nexus daemon status|stop|restart|logs`, `nexus restart`.

### HTTP/SSE and the browser

The opt-in peer transport (`NEXUS_HTTP=1`, `python -m nexus.host.daemon --http`
or `Daemon(http=True)`; off by default; the `nexus` CLI has no flag for it):

- commands `POST /v1/command`, events `GET /v1/events?session=…`; SSE
  `Last-Event-ID` maps one-to-one onto the log `seq`;
- binds `127.0.0.1` only (a non-loopback host is refused at construction), bearer
  token compared in constant time on every request, strict `Origin` allow-list on
  every request (a missing `Origin` is refused), never
  `Access-Control-Allow-Credentials`;
- everything bounded: headers 16 KiB / 64, body 1 MiB (8 MiB for the web voice
  route), 64 connections, 100 requests per connection, SSE queue 256 events /
  4 MiB, 30s read and 10s write timeouts; a consumer that falls behind is dropped
  and never stalls a turn;
- host, port, token and origins are published in a mode-`0600` discovery file
  beside the socket (`default_http_path` / `read_http_endpoint`), removed on
  shutdown; the token is never logged or placed in health.

The browser surface rides the same listener: `nexus web` sends `WebLaunch` over
UDS; `Daemon.web_launch()` starts the listener and returns
`http://127.0.0.1:<port>/#ticket=…`. Routes, auth and CSP are in [web.md](web.md).

## `host_support/` and `observability/`

Read-only projections and helpers kept out of `host/`:

| File | Role |
| --- | --- |
| `browser_view.py` | browser-safe reducer projection and structural patches |
| `context_preview.py` | privacy projection of the next-turn context preview |
| `agent_context.py` | the request a subagent actually sent, shaped for the context header |
| `approval.py` | bounded permission-request projection |
| `workspace.py` | bounded file search respecting read-deny roots |
| `git_diff.py` | bounded read-only Git diff |
| `worktree_projection.py` | allow-listed worktree records and diffs |
| `session_archive.py`, `archive_protocol.py` | bounded archive projections and wire records |
| `model_settings.py`, `auto_title.py` | Tier lists, the subagent ceiling and the title switch (global config); the background title task |
| `settings_inventory.py`, `settings_scope.py` | Settings console reads/writes and path policy ([extensions.md](extensions.md#settings-files-host)) |
| `setup.py`, `provider_auth.py` | first-run setup and provider sign-in ([models.md](models.md#authentication)) |
| `doctor.py` | redacted, bounded-output health; aggregates durable `registry.mismatch` events from a bounded tail of a bounded set of logs, never opening a session; the largest-logs rows aggregate all of the project's records (output bounded, scan cost not); the `quick_check`, tail scan and aggregate are cached per store for 10 min (`HEAVY_TTL`) and can be costly on a large store |
| `voice.py` | voice dispatch and Doctor projection ([voice.md](voice.md)) |
| `live.py`, `live_consent.py` | `Live*` dispatch and `FacadeLiveHost`, the call manager's per-call seam ([live.md](live.md)); the once-per-machine consent with its separate cross-session `manager` scope |
| `manager_port.py` | `HostManagerPort` (the workspace-manager session's tool seam) and `FacadeManagerHost` (starts and cancels its roots through the facade) ([manager.md](manager.md)) |
| `mock.py` | `Mock*` dispatch (dev mode) |
| `update_check.py` | "X.Y.Z available" notice, cached daily in `~/.nexus/cache/update-check.json`; off with `NEXUS_NO_UPDATE_CHECK=1`, `[updates] check = false`, `CI` or an editable install ([release.md](release.md)) |
| `install.py` | install method detection, `uv` lookup, daemon hygiene for `nexus update` |
| `searchserver.py` (+ `searchserver/`) | loopback SearXNG Docker Compose assets (`nexus searchserver start`; service at `127.0.0.1:18765`) |
| `socket_dir.py` | private fallback dir for long socket paths |

`observability/daemon.py` keeps bounded in-memory diagnostics for one daemon
generation (≤ 512 entries / 128 KiB; only reviewed event names, fixed templates,
no caller text); `observability/session.py` projects allow-listed session
lifecycle events (pages ≤ 100, scan window 4096 records). Both back `LogsRead`.
Tool names, error text, previews and message payloads stay private.

## Rules

- A surface never reads session files or touches a manager. If it needs data or
  an action, add a command.
- Never return credentials, environment values or raw config; redact errors.
- Bound every list, page, size and timeout.
- Durable first: state a reconnecting client needs must come from the log or a
  host command, never only from memory.

`SessionStart` and `SessionEnqueue` accept optional `attachment_labels` parallel
to draft IDs. The host validates unique, kind-matching numbered labels and stores
them in attachment metadata; omitted labels are numbered in attachment order.

`SessionQueueMove(session, queued_id, offset=±1)` and
`SessionQueueRemove(session, queued_id)` edit one pending message and return
`SessionQueueEditResult(changed)`; `changed` is false when the message already
ran, was dropped, or sits at the edge. A move also reorders the supervisor's parked
turns (`Supervisor.reorder`), because each parked queued turn consumes the session's
queue head and is skipped once its own id is gone.

`SessionQueueSetMode(session, queued_id, mode)` promotes one pending message
(`mode` is `steer` or `interrupt`; only upgrades succeed, anything else returns
`changed=false`, an unknown `mode` is an error). It is durable (`input.mode_changed`),
reorders the supervisor like a move, and for `interrupt` cancels the running turn without
dropping the queue ([loop.md](loop.md#steering-queue-interrupt)). Client method:
`queue_set_mode`. `SessionQueueRemove(return_content=True)` also returns the removed
message's full content blocks in `SessionQueueEditResult.content` (the queue view cuts
text at 160 characters); the native client uses it to return a cancelled steer message to
the composer (`queue_remove_content`). The gateway does not expose `SessionQueueSetMode`.

`SessionCancel(return_queue=True)` atomically captures pending queued text before
removing it from scheduling and returns `returned_messages` in queue order.
The TUI and web Stop actions prepend these messages to the current draft.
Consumed inputs are excluded; the original input records remain in the durable
log. Other callers retain the existing cancellation contract by default.

### Cross-project session navigation

`ProjectSessionsList` reads the shared SQLite index, returning the workspace and
project identity beside each session (`limit` defaults to 1,000 and is clamped
to 1–1,000; `truncated` is explicit via one extra SQL row). Clients can request
the recent 20 initially and use `query` to find older sessions without loading
the full index. The optional query defaults to empty, is limited to 256
characters, and matches literal case-insensitive substrings in title, ID,
saved user/agent snippets and workspace root before the SQL limit. It
excludes child, archived and trashed sessions, unless `archived=N` (0 to 100, default 0,
ignored when `query` is set) appends the newest N archived sessions with
`ProjectSession.archived = true`. A session that is `awaiting_permission` in the active
workspace carries `ProjectSession.pending` = `{request_id, kind, summary, ts}` (the oldest
unanswered request, redacted and bounded to 160 characters of target) so a dashboard can
answer it through `PermissionResolve` without opening the session. The active workspace retains live
status using only cached handles for returned local rows (no full session list
or log rehydration); other workspaces expose saved activity. When a workspace is a linked Git
worktree, its rows also carry `repo` (the main checkout, from `git_head`'s
`main_root`; one bounded `.git` read per distinct workspace, cached for five
minutes so the sidebar's polls never touch the disk, no subprocess) and
`worktree` (its branch), so clients can group worktrees under their repository.
`ProjectSessionOpen` validates a
recorded workspace/session pair before connecting to its owning daemon, returning
a socket path for the terminal or a one-use browser launch URL. Each workspace
keeps its own runtime, settings and permissions. This does not implement the
shared-daemon plan.

### Native diagnostics identity

The daemon attaches its PID and socket path to the host doctor report as
`daemon`. The native Logs header consumes this report through the existing
host command, together with session and protocol identity. Embedded clients may
have an empty daemon identity. The UI does not inspect daemon files or managers.

## MCP loading commands

`ContextMcpLoadingSelect(session, server, mode)` persists a per-session choice;
`mode` is `search`, `all`, or null to follow config. Unknown servers and choices
after the first turn are refused. It returns refreshed `ContextInspectResult`.

`SettingsMcpLoadingSet(scope, server, mode, expected_sha256)` changes one
server's loading key in the defining `mcp.json` through the Settings path policy.
It preserves JSONC comments and surrounding formatting, refuses ambiguous
objects, checks the supplied hash and returns `SettingsWriteResult`. Successful
writes reload extensions. Existing sessions keep frozen modes.

### Cancelling one child agent

`AgentCancel(session, agent_id, reason="")` stops one running child (including an
external Claude Code worker, whose process group is killed by its own cancellation
path) and nothing else: the parent turn and sibling children continue, and the
parent's `Task` call returns a `cancelled` result (for an external worker it still
carries the authority line, unknown usage and the verified `files_changed`, because
Claude may already have changed files). Each child sees a token that is
cancelled by its parent's token or by its own (`agents/runner.py`, a process-wide
registry keyed by child session id, removed when the child ends).
`AgentCancelResult.cancelled` is `false` with a message when the agent is unknown
to that session (`No such agent in this session.`) or is no longer running; the
reason is clipped to 200 characters. The native client reaches it from the `/tasks`
menu (a `Cancel <agent>` row under each running agent). Human handoff into the live
Claude PTY is not implemented.

### Claude Code settings

`ClaudeCodeSettings` / `ClaudeCodeSettingsSet(main_enabled, subagent_backend)` read
and write `[models] claude_code_main_enabled` and `claude_code_subagent_backend`
(`worker` | `sdk`; a field left `None` is unchanged, any other backend is a
`ConfigError`) in the global config (same writer, hash check
and read-back as session titles) and rebuild model routes. The set is **rejected**
(`SessionBusy`, nothing written) while any turn runs, so the config file and the live
router never disagree; `claude_code_settings_set` itself refuses without an idle
`reload`. The result's `message` says that main selection
uses the Agent SDK and that subagents are configured per role; `subagent_backend`
and `backend_message` state the backend and what it mediates.
`Runtime.claude_code_main_enabled()` reads the effective config on every call, so
the model picker follows the toggle without a restart.
# Sessions dashboard has no preview command

There is no `SessionDashboardPreview` host command, `SessionDashboardPreviewResult`
type, or client `dashboard_preview` method, and the dashboard snapshot has no
`preview` key. The list (`ProjectSessionsList`) is the whole dashboard data path;
its rows carry the schema 4 snippets. Preserve the durable user-facing title
**Sessions** and the `/sessions` command. The archive `SessionPreview` command is
unchanged. Why it was removed: [decisions.md](decisions.md#sessions-dashboard-has-no-preview-pane-2026-10).

### Read-only ownership tree

`SessionTree(session, namespace="main")` returns `SessionTreeResult(session,
tree)`. Namespace is restricted to this workspace's `main` or `agents`; session
ids are validated stored ids, not logical agent paths. The host uses configured
agent max depth plus one, capped at 64. The response contains `nodes`,
`unattached`, `truncated`, `max_nodes` (500), `max_depth` and `read_only`.

Each node exposes stored `id`, `namespace`, `kind`, redacted `title`, `status`,
`depth`, `root_id`, `owner_namespace`, `owner_id`, `owner_agent_id`,
`owner_turn_id`, `owner_spawn_seq`, `owner_call_id`, `agent_id`,
`logical_session_id`, historical `parent_id`/`fork_seq`, `last_seq`,
`context_head_seq`, resolved model/tier, handoff count, last activity/idle seconds,
`running_children`, nested `children` and `actions`. Unknown facts are null.
`handoff_count_bounded` marks a tail-derived lower bound before the later handoff
writer exists. Action flags advertise transcript/request/context inspection only;
send/resume/fork are false. This endpoint grants no child execution authority,
never recovers interrupted logs and never adopts a fork as a task child.

`SessionList(include_children=False)` remains root-only by default. Opting in
adds child summaries with namespace/kind/root identity. Summaries add direct
`child_count` and bounded conservative `running_children` counts. Unknown legacy
children remain explicitly unattached; Doctor's database section exposes
`unattached_children` and `largest_session_logs`: the 10 largest logs of the
current project across namespaces (`scope: "project"`), each only
`{namespace, session, records, bytes}` from one bounded SQL aggregate over
`records` (cached with the `quick_check`); bodies, titles and paths never leave
SQL. These additive host contracts do not add a web feature or
change terminal navigation in P2.

`SessionInspect(session, namespace="agents")` returns `SessionInspectResult`
with a pure-reduced durable `view`, last persisted `context.assembled.request`
(including provider/model), and `truncated`. It validates project visibility via
the ownership contract, reads at most 4,000 tail records and does not open a
handle, recover dangling tools, start a turn, or create/acquire a turn lock.
Display values preserve newlines, redact likely secrets and strip unsafe control
characters; fields cap at 16,000 characters, collections at 500, nesting at 24
and text at 256,000 characters per result. Missing request evidence is `{}`, not
an inferred live context. Tree detail fields are nullable: task/description,
resolved model/tier, current tool, running elapsed time, bounded latest model
usage, optional cost, branch/worktree, terminal result/error. Usage is explicitly
bounded rather than a lifetime total. Existing `AgentTranscript` remains a
parent-log projection (including fork boundaries); ownership-tree inspection
uses the separate durable `SessionInspect` endpoint.

### Submission request identity

`SessionStart` and `SessionEnqueue` accept optional `client_request_id: str = ""`.
Nonempty ids must match `[A-Za-z0-9_-]{1,64}`. One durable admission per
`(project, namespace, session, request id)` is guaranteed: a matching retry returns
the original result, including ids and admission depth, while changed content,
blocks, attachment tokens/labels, operation or mode returns typed
`RequestIdConflict`. Retry lookup precedes attachment resolution, so a lost reply
can be retried after the host has released prepared attachment bytes. Empty ids
keep legacy submission behavior. Shell submissions and queue edits are excluded.

Python `Client.start_turn`/`enqueue` generate an id unless explicitly supplied;
callers retrying after a lost reply should retain and pass that id. The shared
stream helper accepts an explicit retry id. Native/desktop shared composer actions
and the existing web composer retain a pending id across a failed send, replacing
it when content/session/mode/attachments change or a send succeeds. This adds no
new web UI. Durable queue/index commit precedes scheduling, cancellation for an
interrupt, observation and publication. Idempotency is not a promise of
exactly-once external effects or crash-time turn resumption.

On startup the daemon runs a read-only admission scan
(`HostFacade.recover_admissions`): admitted input with no consumed/dropped
disposition in a live session is only counted in a `daemon.admissions_pending`
log line. Nothing is dropped, replayed or scheduled and archived/trashed
sessions are untouched; the durable queue runs at the session's next turn.

Recovery limitation after a daemon crash: a retry with the same
`client_request_id` returns the original (historical) `turn_id`, but that turn is
not resumed; an interrupted turn stays failed. A retained queued entry runs on a
fresh explicit turn under a new turn id, so clients must not read a retry as
restarted execution. Completed tool results are durable and not re-executed;
in-flight external shell recovery is not covered
(`tests/test_host_daemon_crash.py`).

### Handoff contract

`SessionHandoff(session, summary?, continuation?, request_id="")` returns
`SessionHandoffResult(data)` with `handoff_id` on fresh success, `queued_id`,
`turn_id`, and `fresh`. A continuation is admitted atomically with the head;
only fresh admission is scheduled. Manual writes require an idle session and
refuse a competing turn lease. Text continuations use the existing queue;
there is no attachment upload or permission bypass in this command.
`SessionHandoffRead(session, namespace="main", handoff_id="")` is project/namespace scoped,
read-only, and returns a bounded redacted brief plus head/version/boundary.
It does not open/recover a live handle. Both commands share protocol codecs
across direct, socket and desktop clients. `LogsRead` projects a constant
`context.handoff` message, never the private brief/preview.

### Immutable session names

Saved sessions receive an immutable ASCII adjective-color-animal name, globally unique across projects and root and child namespaces. Names survive title changes, archive, and trash; purged names are never reused. Drafts remain unnamed until persistence. Exact ids take precedence over exact, case-sensitive names; prefixes and Unicode lookalikes are not normalized. `nexus sessions show <name-or-id>` resolves without opening a handle. The host `SessionResolve` command accepts `ref` and optional `namespace`, returning canonical identity and lifecycle metadata; resolution does not grant access or revive deleted sessions. There is no rename operation.

### Named identities and code telemetry

`SessionResolve` accepts exact `ref`, optional `namespace`, and
`include_retired` (default false). It returns canonical logical identity and
lifecycle metadata without opening a session: `{logical_id, project, name, kind,
state, trashed, retired, namespace}`. Only this workspace's project resolves; another
project's name or id is "missing or ambiguous". A trashed session answers
`trashed: true` (`state: "trashed"`); a purged name answers `retired: true`
(`state: "retired"`) only with `include_retired`, otherwise it is missing. The manager is
never resolvable by name (its reserved id still works, and its usual refusals apply).
`_dispatch` resolves names for `SessionTree/Inspect/Fork/Delete/Export/Archive/Unarchive`
and `SessionOpen`; a ref that does not resolve is passed through unchanged so each
handler keeps its own refusal (reserved manager id first). `name` is on `SessionSummary`,
`SessionTree` nodes and `ProjectSessionsList` rows, and `query` matches names.
Retired results are tombstones, not handles. `session.named` is durable on allocation/backfill with `name`,
`names_version`, `logical_id`, and `namespace`.

The public event catalogue also includes `code.started`, `code.call`,
`code.store`, and `code.finished`. They correlate by outer `call_id`;
`code.call` updates a nested call by `nested_id`, carrying labelled tool, kind,
status, arguments, output/error and timing. `code.store` applies `set`/`delete`
to bounded preview entries. Native bridges project these into labelled program,
parameters, nested calls, results, diagnostics and store rows, not raw JSON.
Unknown event types remain forward-compatible.
