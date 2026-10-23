# Host: daemon, protocol, facade, transports

## Shared daemon (protocol v6)

One socket per Nexus home and Python install serves up to 32 project runtimes:
`shared-v6-<sha8(sys.prefix)>.sock`. Runtime construction is lazy. `Hello.focus`
is an absolute launch path; its bounded environment (1,024 variables / 256 KiB)
is captured once per project and is never returned or logged. `Welcome.focus`
identifies the resolved project. Administrative connections use `client=control`
and do not load a runtime.

`ProjectFocus` changes the connection default without cancelling subscriptions.
Session commands route to the owning project, including drafts that are still
in memory. An explicit `project` key selects a project; ambiguous historical
session ids require it when the focus cannot disambiguate them. `ProjectOpen`
registers/loads a path and `ProjectList` lists known roots, including empty ones.
`ProjectSessionOpen` focuses and opens the session on the existing connection;
its result contains `project` and `session`, with no reconnect socket.

Each runtime has its own settings, grants, presence, MCP connections, shell jobs,
environment and diagnostics. A shared supervisor names sessions by project and
schedules projects round-robin under a ceiling of 32 turns. Shell jobs and model
requests also take process-wide resource leases (16 processes; 6 requests per
provider). Those leases are fair by project then session and cancellation-safe.

Unreferenced idle projects are evicted independently; connections and active
subscriptions pin their runtime. The empty daemon then waits another idle window
before exiting. A failed project load does not stop other projects. Browser tabs
use `/w/<project-key>/` URLs with separate path-scoped cookies and tickets on one
listener. HTTP peers can address loaded projects explicitly; loading a project
and supplying its environment requires the owner-only Unix socket.

No per-workspace compatibility mode or migration of
old linked-worktree project rows is provided.


The optional device gateway is separate from and does not change the host
service. See [gateway documentation](gateway.md) before enabling it.

`nexus/host/` (L4½) is the transport-neutral surface over one `Runtime`. **A UI
may use it and nothing else** (`nexus.host`, plus the pure `view`, `events`,
`client`, `host_support`, `ui_support` helpers).

## Files

| File | Owns |
| --- | --- |
| `protocol.py` | the wire contract: frozen, tagged `msgspec` commands and `*Result` structs; `PROTOCOL_VERSION` (6); `decode_command`; `ErrorResult` |
| `facade.py` | `HostFacade`: implements every command (`handle`), `subscribe`, `state`, `web_snapshot`, `subscribe_web`, `subscribe_workspace` |
| `daemon.py` | `Daemon`: lifecycle, socket, handshake, idle shutdown, `web_launch`, `ensure_daemon`/`status`/`stop`/`logs`, daemon entrypoint |
| `supervisor.py` | `Supervisor`: turn scheduling under a global cap; a cancel keeps queued subagent reports |
| `reports.py` | `ReportDelivery`: legacy module for subagent reports; `SessionActivity` probe and historical report seams |
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
| Sessions | `SessionList` `ProjectSessionsList` `ProjectSessionOpen` `SessionOpen` `SessionStart` `SessionEnqueue` `SessionShell` `SessionJobs` `SessionJobLog` `SessionJobStop` `SessionJobBackground` `SessionQueueMove` `SessionQueueRemove` `SessionQueueSetMode` `SessionCancel` `SessionSubscribe` `SessionState` `SessionActivity` `SessionFork` `SessionDelete` `SessionRestore` `SessionExport` `SessionArchive` `SessionUnarchive` `SessionListArchived` `SessionSearch` `SessionPreview` `SessionWorktreeChoice` `SessionWorktreeSet` `SessionPromote` `SessionDetach` `SessionWorktreeStatus` `SessionWorktreeTrust` |
| Approvals | `PermissionResolve` `QuestionAnswer` |
| Models | `ModelsList` `ModelShow` `ModelTiers` `ModelTierSet` `ModelTierReset` `AgentMaxTierSet` `ModelsRefresh` `ModelSelect` `ReasoningEffortSelect` |
| Session titles | `SessionTitleSettings` `SessionTitleSettingsSet` |
| Claude Code | `ClaudeCodeSettings` `ClaudeCodeSettingsSet` |
| Attachments | `AttachmentPrepare` `AttachmentPreview` |
| Speech | `Speak` `SpeakStop` `SpeechStatus` `SpeechPrepare` |
| Agents | `AgentsList` `AgentCurrent` `AgentSelect` `AgentReset` `AgentDefaultSet` `AgentTranscript` `AgentCancel` `AgentClose` |
| Context and tools | `ContextInspect` `ContextMcpLoadingSelect` `ContextExtensionSelect` `ToolsList` |
| Extensions | `ExtensionsReload` `ExtensionsList` `ExtensionsValidate` `ExtensionsTrash` |
| Settings and setup | `SettingsInventory` `SettingsRead` `SettingsMcpLoadingSet` `SettingsMcpAdd` `SettingsWrite` `SettingsReset` `SettingsDelete` `SkillsInstall` `SetupStatus` `SetupSave` |
| Providers | `UsageRefreshSettings` `UsageRefreshSettingsSet` (result `UsageRefreshSettingsResult`) `ProvidersStatus` `ProviderLogin` `ProviderLoginPoll` `ProviderLoginCode` `ProviderLoginCancel` `ProviderKeySet` `ProviderLogout` `ProviderModelsRefresh` (agy model-list probe; result `ProviderModelsRefreshResult`) `ProvidersUsage` |
| Worktrees | `SessionWorktreeChoice` `SessionWorktreeSet` `SessionPromote` `SessionDetach` `SessionWorktreeStatus` `SessionWorktreeTrust` `WorktreeList` `WorktreeInspect` `WorktreeReview` `WorktreeAcknowledge` `WorktreeIntegrate` `WorktreeDiscard` `WorktreeRemove` `SessionIntegrate` `SessionTurnDiff` |
| Workspace | `FileSearch` (`query`, `limit`, `include_directories=false`; true adds visible `dir/` rows; `session=""` selects the main root) `GitDiff` `DirectoryList` `FilePreview` `GitStatus` `LogsRead` |
| Voice | `VoiceStatus` `VoicePrepare` `VoiceTranscribe` `VoiceCancel` `VoiceRemove` |
| Live (GPT Live voice calls) | `LiveStatus` `LiveStart` `LiveMute` `LivePause` `LiveStop` `LiveHelperEvent` `LiveConsent` (`scope` `""` or `"manager"`) ([live.md](live.md#host-commands)) |
| Manager (off by default, `[manager] enabled`) | `ManagerOpen` (idempotent open-or-create of the singleton manager session) `ManagerState` (read-only: id, pinned model, Codex sign-in, Live consents, managed roots, caps) ([manager.md](manager.md)) |
| Daemon | `Doctor` `Health` `UpdateStatus` `Shutdown` `WebLaunch` |
| Dev mode | `MockList` `MockStart` `MockClean` (error outside dev mode) |

`DirectoryList`, `FilePreview` and `GitStatus` feed the native sidebar's Files and Git tabs
(`host_support/workspace_browse.py`; the UI never touches the filesystem or runs Git):

| Command | Contract |
| --- | --- |
| `DirectoryList(path="", limit=200, session="")` | One directory under the selected root → `DirectoryListResult(path, entries, truncated)`. `session` selects that session's root; empty selects the project root. `path` is root-relative POSIX; absolute paths, `..`, empty or hidden segments, backslashes, NUL/control characters and paths over 1,024 characters are refused, as is any path through a symlink or under a configured read-deny root. Entries are `{name, path, kind: dir\|file\|link, size}`, directories first, then case-insensitive names. Symlinks are listed (`link`) and never followed; hidden entries, special files, control-character names and credential-looking names are omitted. `limit` is 1..500 (default 200); at most 5,000 directory entries are read; `truncated` says when either cut applied. |
| `FilePreview(path, max_bytes=65536, session="")` | The start of one file under the selected root as text → `FilePreviewResult(path, status, text, size, shown, truncated, message)`. `session` selects that session's root; empty selects the project root. `path` follows the `DirectoryList` rules and must also be a regular file (no symlink on the path, no special files, no credential-looking name, no read-deny root); the file is opened with `O_NOFOLLOW` and at most `max_bytes` (1..262,144) are read. `status` is `text` (redacted; a clip never splits a UTF-8 character; `truncated` says the file is longer), `binary` (NUL bytes or not UTF-8: no text is returned, `message` says so) or `empty`. Unsafe or unreadable paths are errors. |
| `GitStatus(session="")` | Read-only `git status --porcelain=v2` of the selected root (`session` empty means project root; no shell, `GIT_OPTIONAL_LOCKS=0`, `core.fsmonitor=false`, 5 s deadline, 512 KiB output cap) → `GitStatusResult(available, message, branch, upstream, ahead, behind, rows, counts, truncated)`. Rows are `{group: conflicts\|staged\|unstaged\|untracked, code, label, path, from}`, at most 500, paths redacted and relative to the repository root. Outside a work tree `available` is false with a `message`. `GitDiff` uses the same session-root selection. File reads retain the session path guard, including project policy read-deny roots. |

`SessionOpen.root` optionally links a newly created session to an absolute external linked-worktree path. It is applied only after the session is created; opening an existing session with a different root is refused. The runtime validates that the path belongs to the project's repository. An empty root keeps the session on the project root.

`SessionWorktreeChoice(session, mode="")` reads the project mode (`none`, `ask`, or `always`); a nonempty mode remembers it and updates an editable draft. Its status includes `mode`, `chosen`, `editable`, and the current root. A boolean `SessionWorktreeSet` answers Ask for this session without changing the project mode.

The session worktree commands share `SessionWorktreeResult(session, state)`. `SessionWorktreeStatus` reports the current selection, root/worktree state, Git availability and a bounded local-branch list; a non-Git project reports a disabled reason. `SessionWorktreeSet(session, enabled, base="current", name="")` changes a draft's first-turn preference; a nonempty `name` (with `enabled`) is validated (letters, digits, `-`, `_`, 1-60 chars, starting with a letter or digit; branch `nexus/session/<name>` and the worktree folder must be free), becomes the session title (a user title) and is recorded durably so the first turn creates that folder and branch. An invalid or taken name returns an `ErrorResult` the UI can show and re-ask; a session that already has a root or has started refuses a name. The `state` of `SessionWorktreeChoice`, `SessionWorktreeSet` and `SessionWorktreeStatus` also carries `selected` (bool, worktree selected for this draft or already in one), `pending_name` (chosen name before creation, `""` otherwise), `worktree_name` (folder name once created, `""` before), `main_path_display` (the main checkout, absolute) and `base_branch` (branch the worktree is or will be created from), next to the existing `mode`, `kind`, `path_display`, `branch`, `editable`. `SessionPromote(session, reason="", continuation="")` moves a main or external session at a turn boundary; `SessionDetach(session)` returns an owned session to the main checkout. Setup trust choices use `SessionWorktreeTrust(session, decision, digest)`, where decision is `allow_once`, `allow_project`, or `skip`; `digest` must match the setup digest shown by status, so a stale review cannot authorize changed setup. The runtime validates draft/boundary state and owns worktree mutations; clients receive only sanitized status data. New-session UI can call these commands without reading settings or Git directly.

`WorktreeRemove(child_id, confirm_token="")` previews removal of an authenticated modern checkout. Confirmation is single-use, expires after 120 seconds and binds the fresh checkout state. The owner must be idle and the checkout clean, including ignored files; removal preserves its branch and session record.

`SessionIntegrate(session, confirm_token="")` uses the same result envelope. Its first call returns a bounded preview of the owned session branch, original target branch, commits and changed files, plus a short-lived confirmation token. The confirmed call rechecks the full preview and performs a local fast-forward to the reviewed session commit only when the target checkout is on its original branch, both checkouts are clean and idle, and no Git operation is active. It refuses divergence, stale state and ignored-file overwrites. It never pushes.

The shell job commands (`host_support/session_jobs.py`, [tools.md](tools.md#shell-jobs)) read the
runtime's job registry for the **named session only**, so a job id from another session answers
`unknown`; they accept registry job ids (`job_` + 12 hex), never paths or PIDs:

| Command | Contract |
| --- | --- |
| `SessionJobs(session)` | → `SessionJobsResult(jobs, running, truncated)`: at most 64 `JobSummary` rows, running first then newest (`job_id`, `command` ≤ 500 chars, `cwd`, `status`, `background`, `pid`, `exit_code`, `signal`, times, log byte/line counts, `log_available`, `can_background`, `source` `tool`\|`user`). No output, environment or file path. |
| `SessionJobLog(session, job_id, tail_lines=200, offset=None, limit=32768)` | → `SessionJobLogResult(status, text, start, end, size, next_offset, message)`. Without `offset`: the last `tail_lines` (1..2,000) lines; with it: `limit` (1..51,200) bytes from that offset and `next_offset` to continue. `status` is `ok`, `unknown` (no such job in this session, or its log is gone) or `unavailable`. Works after the job left memory. Text is raw program output: a client must sanitise control sequences before drawing it. |
| `SessionJobStop(session, job_id)` | Stops the job's process group → `outcome` `killed`, `finished` or `unknown`. |
| `SessionJobBackground(session, job_id)` | Asks the tool call blocking on a foreground job to hand it to the background → `outcome` `requested`, `already_background`, `finished`, `not_foreground` (nothing is awaiting it) or `unknown`; `requested` is true only for the first. The move and `shell.job_yielded` happen in that tool call. |

The daemon does not idle-exit while any shell job is running (`Runtime.has_active_shell_jobs`); the idle
window restarts when the last job ends. A *persistent* job (`shell` with `background` and no `timeout_s`,
[tools.md](tools.md)) is also bounded by the orphan policy: with no viewer, no running or queued turn and no
live voice call for `tools.shell_orphan_s` (3600 s, monotonic; `0` disables, `--idle-timeout 0` does not),
the daemon stops those jobs (`shell.job_exited` with `stopped: "no viewer for 1 h"`) and exits. `Health`
carries `persistent_jobs_total` and a bounded `persistent_jobs` list, shown by `nexus daemon status`;
`nexus daemon stop` ends the groups through runtime shutdown. `JobSummary.persistent` marks the row.

**Restart recovery (SHELL_PLAN P7).** `Daemon.start`, holding the daemon lock and before binding the socket,
sweeps the process groups a killed predecessor left behind (`Runtime.recover_shell_orphans`: dead owner
locks only, members re-verified by pid + start time + pgid + uid before SIGTERM and SIGKILL, 6 s budget, 8 s
timeout; `daemon.shell_orphans_swept` / `daemon.shell_recovery_failed` in the log). It writes no session
event and starts no turn. A session records `shell.job_exited` (`interrupted`, `delivery: none`) for each lost
job when it is next opened. Details and limits: [tools.md](tools.md).

None of these commands is available to paired gateway devices (the gateway policy is default-deny).

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
- **Legacy subagent reports** (`reports.py`; subagents are foreground-only,
  [agents.md](agents.md#foreground-only); the module is legacy and detached-run
  reports are historical records, not replayed). `SessionActivity` answers
  `SessionActivityResult(turn_active, queued, running_agents, reports_pending,
  wait_seconds)`; `nexus run` and the JSONL stream poll it after the first
  terminal event to follow the session until quiescent, bounded by
  `wait_seconds` (`[agents] oneshot_wait_seconds`, 0 = exit at once with a
  "still running" note).
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

One daemon per Nexus home and Python install, addressed by a deterministic socket
`~/.nexus/daemon/shared-v6-<sha8(sys.prefix)>.sock` (mode `0600` in a `0700` dir; a private short
fallback dir when the path exceeds the ~104-byte UDS limit on macOS).

- `flock` on a sibling lock plus a pid file prevent duplicates; a stale socket
  from a dead daemon is reclaimed under the lock.
- **Handshake:** `Hello` → `Welcome`; a client built for another
  `PROTOCOL_VERSION` is rejected (`VersionMismatch`), never retried.
- **Auto-start:** `ensure_daemon` spawns one and waits for readiness (10s).
- Each runtime captures its supplied environment (or the process environment)
  at construction. Later caller mapping or process-environment changes do not
  alter its configuration/provider inputs or shell job inheritance.
- **Idle shutdown:** idle unreferenced projects unload after 300s; an empty
  daemon exits after another idle window. Active turns and jobs pin their project.
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
`http://127.0.0.1:<port>/w/<project-key>/#ticket=…`. Routes, auth and CSP are in [web.md](web.md).

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
`projects=N` (0 to 500, default 0) also returns `ProjectSessionsListResult.projects`: up to N
project roots (`ProjectEntry`: `workspace`, `project_id`, `repo`, `worktree`, `updated`, `exists`),
only those with an eligible session (archived included, so a project whose sessions are all archived
stays listed), newest activity first (`updated` = newest user activity). Registrations without
sessions are not listed. The active workspace is always first, eligible or not. `exists` is false
when a historical root is no longer a directory (one bounded `isdir` per entry; no worktree read
for it) and clients omit such entries; the current workspace is never omitted. Session rows
under a vanished root are dropped from `sessions` as well (same `isdir` check), so clients cannot
regroup them into projects.
`ProjectSessionOpen` validates the session's owning project, opens it in that
project's loaded runtime and changes the connection focus. The terminal retains
its socket and existing subscriptions. Browser navigation returns a one-use URL
under `/w/<project-key>/`. Settings and permissions remain project-local.

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

`SettingsMcpAdd(scope, name, entry, expected_sha256=None)` inserts one new server
(see `docs/extensions.md`, "Adding an MCP server from Settings") and returns
`SettingsWriteResult` with `status` `written | exists | conflict | legacy` and a
`message`. `expected_sha256`: `None` = current file, `""` = the file must not exist, hex =
must match. Its repr masks `entry`. It is a daemon slow command (the reload starts the
server's process) and default-denied to paired devices.

`SettingsMcpAdd(..., dry_run=True)` (client: `settings_mcp_add_preview`) runs the same
validation and target checks (exists, stale hash, legacy `.nexus/mcp.json`, global root vs
`NEXUS_HOME`) but writes and starts nothing. It returns `status="preview"` with the masked
confirmation `lines` (no env/header value, no URL credential) and the structured
`transport`; the ratatui Add server flow shows these lines, so the UI imports no MCP code.
`SkillsInstall` for the global scope is likewise refused when `NEXUS_HOME` points away from
`~/.nexus`, the folder Settings writes.

### Plan-usage snapshot

`HostFacade.usage` is a `UsageSnapshotService` (`host_support/usage_snapshot.py`). The
daemon starts its periodic poll (`start_usage_polling` → `UsageSnapshotService.start`): a
30 s ticker that refreshes whenever `[models] usage_refresh_seconds` (default 300, read live
from config each tick; `UsageRefreshSettings` / `UsageRefreshSettingsSet` read and write it)
has passed, even idle and even with the usage gate off. At most one fetch is in flight; a
failed fetch keeps the previous snapshot and waits for the next interval. The runtime also
kicks the service before a sub-agent spawn and after a child ends (`kick()`, gate-enabled
only, at most once per `[models.subagent_usage] refresh_seconds`). `shutdown` closes the
poll and any in-flight fetch. A `ProvidersUsage` result is always a fresh fetch (never the
snapshot) and is ingested into the same snapshot; it counts as a refresh for the poll, and
a result older than one already ingested is dropped. The runtime only receives the snapshot
(`set_usage_snapshot`) and a `usage_kick` callable, so `runtime.py` imports nothing from
`host_support`. A failed fetch keeps the previous snapshot and fails open
([models.md](models.md#usage-aware-sub-agent-routing)).

### Cancelling or closing one child agent

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

`AgentClose(session, agent_id, reason="")` -> `AgentCloseResult(session, agent_id, closed,
message)` closes one child **for good** (kept for compatibility with legacy persistent
children; the root no longer has a stop tool): a running child is cancelled first through its own token (`AgentCancel` semantics; the
cancelled run is not reported to the root), then the pool records `agent.stopped`
(`by: "user"`, the reason clipped to 200 characters), frees the child's `max_live` slot,
and the child can no longer be continued. Its log and any worktree are kept and stay
reviewable. `AgentCancel` ends one run; `AgentClose` makes the child terminal. Idempotent (`closed` is true with "already stopped" for a stopped
child); `closed` is false with `No such agent in this session.` for an unknown id. A
child the process does not know yet (after a daemon restart) is rebuilt from the root
log first (`Runtime.close_agent`). The command is in the gateway's `prompt` scope with
`AgentCancel`; client method `Client.close_agent(session, agent_id, reason="")`.
There is deliberately no `AgentList`/`AgentSend` command: surfaces read agent state
(running, finished, stopped, `runs`) from the reduced `ConversationView` they already
subscribe to, and nothing sends follow-ups to a child. `AgentCancel`'s "not running" message
points at `AgentClose`.

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
ids are validated stored ids, not logical agent paths. The host uses a fixed depth of 8
(independent of `agents.max_depth`) so legacy nested sessions still render. The response contains `nodes`,
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

### Foreground session hints

Native clients report `SessionFocus(session, client_id)` independently of subscriptions
and receive `SessionFocusResult`. Empty/null session clears the claim. IDs are printable
ASCII without spaces, at most 128 characters; the host retains at most 64 claims and
expires them after five minutes (clients heartbeat every 15 seconds). These ephemeral
claims are hints, not authority: multiple clients may focus different sessions. Manager
`session_find` rows expose `focused_clients`; uncertain spoken targets must be clarified.
GPT Live starts only on the singleton manager, never on a foreground root session.

`SessionTurnDiff(session, seq, path, max_bytes)` returns a bounded saved-commit or checkpoint diff in `SessionWorktreeResult.state`. `SessionTurnRevert(session, seq, include_later, confirm_token)` previews an idle owned-worktree revert and issues a 120-second single-use token bound to HEAD and selected commits. Shared checkouts cannot restore; overlapping later turns require `include_later`. Merge commits are currently refused.
