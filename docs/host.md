# Host: daemon, protocol, facade, transports

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
| Sessions | `SessionList` `ProjectSessionsList` `ProjectSessionOpen` `SessionOpen` `SessionStart` `SessionEnqueue` `SessionCancel` `SessionSubscribe` `SessionState` `SessionFork` `SessionDelete` `SessionRestore` `SessionExport` `SessionArchive` `SessionUnarchive` `SessionListArchived` `SessionSearch` `SessionPreview` |
| Approvals | `PermissionResolve` `QuestionAnswer` |
| Models | `ModelsList` `ModelShow` `ModelTiers` `ModelsRefresh` `ModelSelect` `ReasoningEffortSelect` |
| Agents | `AgentsList` `AgentCurrent` `AgentSelect` `AgentReset` `AgentDefaultSet` `AgentTranscript` |
| Context and tools | `ContextInspect` `ContextExtensionSelect` `ToolsList` |
| Extensions | `ExtensionsReload` `ExtensionsList` `ExtensionsValidate` `ExtensionsTrash` |
| Settings and setup | `SettingsInventory` `SettingsRead` `SettingsWrite` `SettingsReset` `SettingsDelete` `SetupStatus` `SetupSave` |
| Providers | `ProvidersStatus` `ProviderLogin` `ProviderLoginPoll` `ProviderLoginCode` `ProviderLoginCancel` `ProviderKeySet` `ProviderLogout` `ProvidersUsage` |
| Worktrees | `WorktreeList` `WorktreeInspect` `WorktreeReview` `WorktreeAcknowledge` `WorktreeIntegrate` `WorktreeDiscard` |
| Workspace | `FileSearch` `GitDiff` `LogsRead` |
| Voice | `VoiceStatus` `VoicePrepare` `VoiceTranscribe` `VoiceCancel` `VoiceRemove` |
| Daemon | `Doctor` `Health` `UpdateStatus` `Shutdown` `WebLaunch` |
| Dev mode | `MockList` `MockStart` `MockClean` (error outside dev mode) |

The browser may send any command except `Shutdown` and `WebLaunch`.
Streaming is the one verb a request/response cannot model: `SessionSubscribe`
names it and the transport attaches through `HostFacade.subscribe`.

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
- `state` folds the log through `view.reduce` into a baseline plus `seq`.
- `web_snapshot` / `subscribe_web` produce the versioned browser snapshot and
  JSON-Pointer patches (`host_support/browser_view.py`).
- `delete` moves a session to trash and never cancels a running turn.
- `doctor` is a redacted report over config, providers, registry, extensions and
  MCP; it performs no model request. It also carries `git` (`root`, `branch`,
  `detached`, `worktree`, `worktree_name`; `{}` outside a repository), read
  without subprocesses by `host_support/git_head.py`, for the top-bar breadcrumb.

## Supervisor and presence

`Supervisor`: a global cap on running turns (`DEFAULT_MAX_CONCURRENT_TURNS` = 4:
"five sessions × parallel tools × subagents is a fork bomb"), a per-session FIFO,
round-robin fairness across sessions, cancellation that drops that session's
queue, and **independence from views**: scheduling never consults a subscriber
count, so a turn with no viewer runs to completion.

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
- **Idle shutdown:** exit after 300s with no viewers, no running and no queued
  turn; never with a turn in flight.
- SIGINT/SIGTERM stop the accept loop and close every session through the facade.
  Every log line and peer-visible error passes `redact_secrets`. ≤ 64
  subscriptions per client.
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
| `settings_inventory.py`, `settings_scope.py` | Settings console reads/writes and path policy ([extensions.md](extensions.md#settings-files-host)) |
| `setup.py`, `provider_auth.py` | first-run setup and provider sign-in ([models.md](models.md#authentication)) |
| `doctor.py` | bounded redacted health; aggregates durable `registry.mismatch` events from a bounded tail of a bounded set of logs, never opening a session |
| `voice.py` | voice dispatch and Doctor projection ([voice.md](voice.md)) |
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

`SessionCancel(return_queue=True)` atomically captures pending queued text before
removing it from scheduling and returns `returned_messages` in queue order.
The TUI and web Stop actions prepend these messages to the current draft.
Consumed inputs are excluded; the original input records remain in the durable
log. Other callers retain the existing cancellation contract by default.

### Cross-project session navigation

`ProjectSessionsList` reads the shared SQLite index, returning the workspace and
project identity beside each session (up to 1,000; `truncated` is explicit). It
excludes child, archived and trashed sessions. The active workspace retains live
status; other workspaces expose saved activity. `ProjectSessionOpen` validates a
recorded workspace/session pair before connecting to its owning daemon, returning
a socket path for the terminal or a one-use browser launch URL. Each workspace
keeps its own runtime, settings and permissions. This does not implement the
shared-daemon plan.
