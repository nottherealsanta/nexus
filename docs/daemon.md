# Daemon, projects and sessions

Nexus Core runs as one background daemon per Nexus home (`~/.nexus`). The
daemon owns projects, sessions, turns, Monty kernels, background bash jobs,
tool approvals, pending questions, workspace control operations, session
archiving, pending directory moves and startup recovery, and exposes them over
the [HTTP API](api.md). Clients never read Nexus state files. Core also owns
interactive terminal PTYs, with a separate bounded stream and lifecycle described
in [terminals.md](terminals.md); they never enter model context automatically.
Terminal routes are the exception to tokenless network access: a tokenless daemon
accepts them only from a loopback TCP peer with a loopback `Host`/`Origin` and no
forwarding headers. A remote client needs the bearer token.
Terminal viewer overflow, disconnect and restart revoke attachment authority;
no controller identity survives restart, including on already-exited resources.
Background bash jobs are pipe processes, separate from terminals: they keep their
ordinary tool-output path and never create a terminal resource.

The daemon also keeps the latest provider-usage snapshot in its database, refreshing it every 10 minutes by default
while running. `[usage] refresh_minutes` changes the interval (1–1440); clients can request an immediate refresh.

An explicit reasoning-effort selection on a root session is remembered for new
root sessions, including after a daemon restart. Selecting default remembers the
absence of an override; otherwise the configured default applies until a choice
is made. Subagent selections do not change this memory.

## Lifecycle (`nexus/daemon.py`)

| Command | What it does |
| --- | --- |
| `nexus serve [--port N]` | Runs the daemon in the foreground. |
| `nexus daemon start` | Starts it in the background if none answers, then waits until it does (15 s at most). It does not spawn a second daemon while one is registered and still starting, and names the process when an unregistered one already holds the port. |
| `nexus daemon stop` | Sends SIGTERM to the pid in `daemon.json`, even when the daemon no longer answers; SIGKILL follows it if a live event stream holds the process past the grace period. |
| `nexus daemon status` | Prints the URL, pid and log path. |
| `nexus pair` | Prints how a phone connects to the running Core: its Tailscale and LAN addresses, the token (none when Core serves the network anonymously), the `nexus://pair?url=…&token=…&name=…` link and a terminal QR code of the best address (`--no-qr`, `--json`). No relay service; it refuses a loopback-only Core and says to restart with `NEXUS_HOST=0.0.0.0`. |

`nexus run`, `chat`, `project` and `session` start the daemon automatically.

`nexus doctor` (`nexus/doctor.py`; `--json` for rows) prints one `ok | warn |
FAIL` row each for: the daemon (reachable, and its `code` fingerprint from
`/api/info` equals the hash of the checkout's `nexus/**/*.py`, so a daemon
started before a code change fails), sign-ins (fails with none), the models.dev
cache age (warns past 7 days), config errors (fails), the `[web] search_url`
container, MCP servers (fails on `failed`/`invalid`), voice STT/TTS and their
Hugging Face cache, and `rg`. It exits 1 on any FAIL and never starts the daemon,
loads a model or downloads. `nexus update` compares `/api/info`'s version with
the latest GitHub release and never installs. The daemon's version (also sent
in the SSE `connected` event) comes from the installed `nexus10` distribution
metadata, so release and development versions follow the package version.

- **Discovery.** While it runs, the daemon writes `~/.nexus/daemon.json` (0600)
  with `{url, pid, token, started}` and removes it on exit. It binds the port
  *before* writing the file, so a second daemon that cannot get the port exits
  without touching the running daemon's registration. A client trusts the file
  only after `GET /api/info` answers (with the token when one is required).
- **Token.** An explicit loopback bind makes a new random token on each start.
  It keeps other local users and web pages off the loopback port. Network binds,
  including the default, record an empty token.
- **Port and address.** `$NEXUS_PORT` (default 4747) on `$NEXUS_HOST` (default
  `0.0.0.0`; `nexus serve --host` overrides it). This listens on all IPv4
  interfaces; `daemon.json` still names the loopback URL for local clients.
  **The default exposes the API without authentication: use only trusted networks
  or restrict access with a firewall.** Set `NEXUS_HOST=127.0.0.1` (or
  `nexus serve --host 127.0.0.1`) for local-only, token-protected access.
- **Remote clients.** A non-loopback bind serves `/api/*` without a token, so a
  phone on the same LAN or Tailscale network connects to `http://<host>:<port>`
  by address alone (the iOS NSH app asks for it once). Only bind on a trusted
  network: anyone who can reach the address can use the API. A specific
  non-loopback address is what `daemon.json` then names, since that bind does
  not accept loopback connections.
- **Logs.** `~/.nexus/logs/daemon.log`.
- **Same code.** The background daemon runs `python -m nexus serve` with the
  same interpreter as the client that started it.

## Projects and sessions (`nexus/store.py`, `nexus/runtime.py`)

- **Project.** A directory, unique by its resolved absolute path.
  `POST /api/project {path}` returns the existing project or creates one, named
  after the folder by default.
- **Rail order and status.** Each project has a `position` (user-defined order) and a `status` (`current`, `passive`,
  `inactive`; names confirmed by the user, 2026-11-06). Both are plain columns added in place to the
  existing 0.10 database (`Store._migrate_manager_columns`, schema version unchanged): an old store reads fine, and
  its projects start in creation order, `current`. See [api.md](api.md#project-rail-order-status-and-removal).
- **Removing a project** (`POST /api/project/{id}/remove`) only unregisters it and never touches files, worktrees or
  the directory. It is refused (409) while any of its sessions runs a turn, waits for an approval or question,
  holds pending input, runs a background job or has a busy kernel. Otherwise the project row is marked removed
  (`hidden = 2`) and its sessions become kind `detached`: still stored, with messages, events and snapshots, but
  excluded from every list, search, snapshot and stream like the Manager's. Clients receive `session.deleted`
  tombstones and `project.deleted` (`removed: true`). Adding the same path again (`POST /api/project`) restores
  the project, `current`, at the end of the rail, with its sessions. `DELETE /api/project/{id}` remains the
  permanent delete.
- **Hidden Manager sessions.** The Manager ([manager.md](manager.md)) is `kind = "manager"` sessions in a hidden
  project. Every default list, search, snapshot, idle-archive sweep and event stream excludes them; their events stay
  in the durable log and open by id.
- **Session.** Belongs to one project and runs one agent ([agents.md](agents.md)).
  Each session has:
  - `directory`: where tools run. It starts as the project path and can move to
    another existing directory or a worktree through the control plane. Project
    identity does not change. `starting_directory` is the directory frozen into
    the system prompt; `directory_changed` says whether they differ.
  - `workspace`: `{"entry": bool, "active": bool}`. A new session has the
    `expose_workspace_tools` entry point; activating it makes the two detailed
    controls available. `directory_change` is the pending move while a running
    turn has not reached a safe boundary yet.
  - `agent`: default `build`.
  - `model`: `provider/model`. There is no default model ([Model selection](#model-selection)): a root
    session stores the model it starts with, an agent's own `model` applies while the session holds none,
    and a subagent without either inherits its parent's. `""` with `model_source: "none"` means none was ever
    chosen; starting a turn then fails with 400 "no model selected".
  - `permission`: `ask` or `allow` (default).
  - `effort`: reasoning effort, or none for the provider's default.
  - `title`: set from the first prompt (`Image` for an image-only prompt).
  - `archived`: whether this session's whole tree is archived. Archive state is
    separate from busy/idle `status`; see [Session archiving](#session-archiving).
  - `status`: busy while its own turn or any child turn is running, even if its
    own turn has finished. A `session.status` event with `turn_finished: true`
    closes that turn while retaining the busy session status.
  - `snapshot`: the frozen agent, prompt, tool definitions, child catalog and
    instruction file resolved at creation ([agents.md](agents.md#session-snapshots)).
- **Messages.** Append-only rows in session order, saved as each one is
  produced (user, assistant, tool). Parts keep their `raw` wire items, so a
  resumed session replays to its provider without loss.
  Both clients show a new session's first submission immediately as a `You` message
  below the unchanged context preview while creation (including MCP startup) runs.
  This pending UI is not inserted into canonical messages. The confirmed user
  message and loaded starting history replace it together; failures restore the
  draft. Clients open the created session and submit its prompt without waiting
  for its history reads. Core's snapshot and MCP readiness guarantees are unchanged.
- **Images.** A prompt may carry images (`POST …/prompt {text?, images?}`;
  [api.md](api.md#image-prompts)). `nexus/images.py` validates them before the
  turn is created: at most eight base64 PNG, JPEG, GIF or WebP data URLs whose
  bytes match their media type, 8 MiB decoded in total, a 1–255 character name.
  The user message is the text, then a `[Image A]`, `[Image B]`, … label and an
  `Image` part per attachment ([llm-providers.md](llm-providers.md#user-images)).
  The image bytes are stored in the message row, so they survive restarts,
  continuation, export and replay to any provider. The prompt route accepts
  bodies up to 12 MiB; every other route keeps 1 MiB.
- **Storage.** SQLite at `~/.nexus/nexus.db` (WAL). Tables: `project`,
  `session`, `session_snapshot`, `message`, `event`, `turn`,
  `turn_request`, `turn_tool`, `turn_attempt`, `session_usage`, `mutation`,
  `preference`, `log`, `event_body` and `storage_metrics`.
  `session_snapshot` is
  the JSON configuration frozen for one session; `event` is the durable event
  log, in publish order (see [Durable event log](#durable-event-log)); `log`
  holds that database's log generation, retention floor and head high-water
  mark; `event_body` holds content-addressed, compressed wire bodies and
  `storage_metrics` is the native SQLite counter row (event/payload/wire/body
  bytes) used instead of rescanning the log on every pass; `mutation` is the
  idempotency journal; `turn_attempt` records provider
  retry attempts; `turn`, `turn_request` and `turn_tool` are the durable execution
  lifecycle ([Crash recovery](#crash-recovery)) and, with `session_usage`, the
  turn summaries ([Turn summaries](#turn-summaries-and-the-spend-ledger));
  `preference` holds machine-wide name/value settings (`Store.preference`,
  `set_preference`).
  The file's `PRAGMA user_version` is the schema version (`store.SCHEMA_VERSION`).
  A file with another version (an earlier release, no version at all,
  or an unreadable file) is renamed with its `-wal` and `-shm` files to
  `nexus.db.v<version>-<YYYYmmdd-HHMMSS>.bak` (`v0` for none, `vunreadable`), and
  a new database starts in its place; the old one is never read. The new
  database is built and integrity-checked as `nexus.db.new` first, so a failure
  at any step puts the old files back and leaves the database as it was.
  `Store.backup` holds the backup path (also logged as a warning) when one was
  made. An empty file is replaced without a backup; a database with a *newer*
  version is refused (`StoreError`) and left alone. Backups are never deleted;
  a release that needs a destructive schema change raises `SCHEMA_VERSION`.
  Additive payload storage (the `event` payload columns, `event_body`,
  `storage_metrics`, triggers and indexes) is created in place on the current
  version, so an existing 0.10 database keeps its event ids, messages and
  snapshots. Old event rows are measured/compressed in small resumable batches
  (`backfill_event_payloads`, `backfill_wire_turn_ids`) during startup and
  maintenance; a crash only leaves the remaining rows unmeasured.
  A `message` row also records the id of the `message.added` event that announced it.
  Deleting a project or session deletes everything under it, including its child
  sessions, snapshots, execution records and session events.
  Archiving changes the `archived` column on every session in the tree and
  releases its idle owned kernels; stored history is preserved.

## Model selection

Nexus has no default model. It remembers the user's selections in the `preference` table: `model.last` (the last model
selected anywhere) and `model.agent.<project>.<agent>` (the last selected for that agent in that project). A selection
is a model chosen with `POST /api/session` (`model`), `POST /api/session/{id}/model` or `PATCH` on a root session; a
subagent's model, a handoff continuation and an inherited model are not selections.

A new root session resolves its model in this order and **stores** it, so the session keeps it for life:

1. the `model` in the request;
2. the agent's own `model` (kept as `model_source: "agent"` until the session switches away from that agent);
3. the model last selected for this agent in this project;
4. the model last selected anywhere;
5. none: the session is created with `model: ""`, and `POST .../prompt` and `.../continue` return 400
   "no model selected; choose one first".

`GET /api/model/last?project=&agent=` returns steps 3 to 5 for a draft. Nothing replaces a remembered model that has become unavailable (a
provider signed out, a model gone): clients show the state and ask for another choice.

Switching agents never changes a session's model. A session that was running its agent's pinned model stores it first.
A subagent without an explicit model, a configured `[agents.models]` pool (with a connected provider) or a
pinning agent inherits its parent's current model.

## Durable event log

`nexus/bus.py` commits durable events before delivering them. Their stable row
ids form cursors `<generation>:<id>`; the generation survives restarts and
copies, and a replacement database gets a new generation. Globals survive a
session deletion; its minimal `session.deleted` tombstone lets clients remove it.

`text_delta`, `reasoning_delta`, `tool_call_started` and `tool_args_delta` are
live-only (`id: null`, no SSE id or cursor). The bus still delivers every wire
event live. Overflow reports the last **durable** id and explicitly requires a
resync. Clients must fetch the running request's partial after registering their
stream; a request-local sequence merges queued deltas with that checkpoint.

`LivePartial` folds ordered text, reasoning and tool-argument parts. Runtime
checkpoints it to `turn_request.partial` at most once a second while streaming,
and at completed-part and terminal boundaries. `/turn` substitutes the current
in-memory partial while the request runs. Saving its assistant message clears
the partial atomically. Interrupted/failed requests retain diagnostic partials;
recovery describes their counts, never puts them into model context. Legacy
requests without checkpoints still use their retained events during transition.

Wire payloads remain content-addressed in `event_body`. A versioned request
manifest shares `input`, `messages`, `contents` and tool-schema items, instructions,
and exact inline data-URI strings across requests. Inline media uses independent
SHA references in the same store, so reassembly does not depend on an artifact
file surviving; exact URI spelling is preserved. Reference edges keep components
alive until the last owning envelope expires. Whole-body raw/zlib encodings stay
readable; maintenance upgrades retained requests in batches of 50. Body counters
measure actual compressed component bytes separately from envelope bytes.

Wire expiry keeps each envelope/id/type and returns an explicit expired marker.
Successful terminal requests retain wire for one hour; failed/interrupted ones
for 24 hours. Environment settings: `NEXUS10_WIRE_SUCCESS_RETENTION_SECONDS`,
`NEXUS10_WIRE_FAILURE_RETENTION_SECONDS`, `NEXUS10_WIRE_PAYLOAD_BYTES` (soft
256 MiB budget), and `NEXUS10_WIRE_DEBUG_RETENTION_DAYS` (0 by default).

Finished sessions are compacted in bounded event batches once archived or idle
for `NEXUS10_SESSION_EVENT_IDLE_SECONDS` (default 86400). Running turns/requests,
pending recovery, uninserted tool results, open waits/jobs, replay pins and
unannounced messages prevent compaction. `session_diagnostics.keep` protects
a debugging session from both compaction and wire expiry; set it through
`POST /api/session/{id}/diagnostics` with `{"keep": true}`. Legacy lifecycle
history is adopted once per session into `wait_record`, `bash_job` and a code-use
flag; recovery subsequently reads state tables rather than scanning old events.

Compaction deletes only diagnostics and commits a `session.compacted` record
in the same transaction, replacing the session's previous marker. The
`session_compaction` table keeps cumulative counts/range/time for the inspector
and exports, even if prefix retention later removes the marker. Global events,
messages, snapshots, usage and turn records survive. Continuing a session writes
new events normally. Raw wire, fine step detail and old export events are lost
explicitly; model context is unchanged.

The global floor advances conservatively to the highest removed diagnostic id.
Retained events can exist below that floor when sessions interleave: clients
must resync for **any cursor below it**, even if that cursor's own row remains.
A cursor at the floor is valid. Prefix retention still removes old diagnostics
(default 14 days or 256 MiB), including retained rows below the floor, while
honouring protections. Turn rows have their separate 30-day / keep-200 policy;
canonical usage outlives them. Mutation entries expire seven days after resolution.

Maintenance wakes on terminal turns, pressure and a periodic interval (at most
60 s). Passes limit event batches (`NEXUS10_RETENTION_BATCH_SIZE` 2000,
`NEXUS10_RETENTION_EVENT_BATCHES` 4), then yield for 50 ms while catching up.
`log.retention` summaries are rate-limited to once per 60 s. `/api/info` reports
storage, blockers, migration, compaction, reclamation and errors.

New databases use `auto_vacuum = incremental` before any table is created.
Maintenance reclaims at most `NEXUS10_RECLAIM_PAGES` (1000) free pages per pass
when free space reaches `NEXUS10_RECLAIM_THRESHOLD_BYTES` (8 MiB). A passive WAL
checkpoint makes the smaller file visible as readers permit. A full vacuum
is never automatic. `Store.reclaim_space` remains dry-run by default. Older
files require the offline `nexus db compact --apply` conversion described in
[development.md](development.md#offline-database-compaction).

Replay registers a subscriber and reads the head atomically, pins the replay
range, pages through durable history, then hands off to the live queue. Cursor
errors (malformed, wrong generation, ahead, below floor) return actionable
400/409/410 errors before opening the stream. The Python client deduplicates
by durable id and preserves its cursor across id-less live events. A storage
error propagates before a durable event can be delivered.

## Mutations and provider retries

A keyed mutation (`Idempotency-Key`) is journaled by fingerprint. Same key and
same request replay the recorded status/body without work; a different payload
is 409. Other routes persist intent before execution; a crash before their
response is recorded becomes `mutation_outcome_unknown` rather than repeating
the action. The journal includes revision preconditions in its fingerprint. Prompt and continue write their journal row in the same transaction as
the turn, so a crash cannot separate acceptance from its replay record. Unkeyed
requests keep single-attempt behavior. The full route matrix, status codes and
snapshot contract are in
[api.md](api.md#mutations-retries-continuation-and-reconnect).

Project and session reads carry a monotonic `revision`; `PATCH` and model/agent
updates accept `If-Match` or `expected_revision` and reject stale values with
409 before changing anything. A keyed update replay happens before the revision
recheck.

Provider retries are bounded and pre-stream: transport, 429, 502/503/504 only,
with jittered backoff. Each attempt is a `turn_attempt` row and a visible
`retry_scheduled`/`retry_started`/`retry_exhausted` event. Partial streams and
completed tools are never replayed automatically. `GET /api/info` reports the
active policy.

## Session archiving

`POST /api/session/{id}/archive` and `/unarchive` act on the whole root tree,
in one SQLite transaction, and are idempotent. A child-only request is a 400
that names the root; clients and the CLI use the root. A transition publishes
one `session.archived` or `session.unarchived` event (stored in the root's event
log) with the tree ids; repeating the same state publishes nothing.

- **Refusals.** A root or any descendant running a turn, or waiting for an
  approval or question, gives 409 and is left alone. A busy owned kernel (including use by another tree) also gives 409. Archive
  releases idle owned kernels, and never removes a worktree, branch or background job. A running background job anywhere in the tree refuses archive with a clear error until it finishes or is stopped.
- **Kernels.** Archive detaches kernels created by the tree (including children)
  and its default kernel immediately, then closes their workers asynchronously.
  It publishes `kernel.deleted` with the state-loss reason. Variables have no
  saved checkpoint; cell history and outputs remain durable. Unarchive leaves
  kernels absent until the next code call creates fresh state. Borrowing another
  tree's kernel does not transfer ownership.
- **While archived.** Messages, events, snapshots,
  directories and tree relationships stay readable. A prompt gets 409 with the
  unarchive command, and a new child in the tree gets 409, until the root is
  unarchived. Unarchive does not start a turn.
- **Resume in the clients.** Both clients allow composing a message in an archived
  session. Sending asks **Resume archived session** before changing anything: a
  large uncached context can make the next request slower and more expensive, and
  archived code kernels restart with fresh state. Cancel keeps the draft and tree
  archived. Resume unarchives the root tree first, then sends to the selected
  session; a failed unarchive never sends the prompt. Explicit Unarchive shows
  the same warning but does not send a message.
- **Notifications.** A background job may still finish. Its durable event is
  published as usual, and its model-visible notice is saved as a context
  message without calling the provider. The notice is there when the user
  unarchives and resumes.
- **Lists.** `GET /api/session` excludes archived trees by default;
  `?archived=only` returns archived trees and `?archived=include` returns both.
  Explicit session, tree, message, event and snapshot reads ignore the filter.

### Automatic archive

`nexus/autoarchive.py` runs one bounded sweep at daemon startup and then about
once a minute. It archives a whole root tree when no session in it has changed
for `[sessions] auto_archive_idle_seconds` (default 3600; `0` disables,
[config.md](config.md#sessions-settings)). Idleness is the newest
`session.updated` in the tree: message inserts and configuration changes set
it, while reads, listings and status polls do not. Work in a child keeps its
root unarchived.

A sweep uses the ordinary archive transition, so each tree gets one
`session.archived` event and releases its idle owned kernels; stored history remains. Trees with a
running turn, a pending approval or question, pending input or a live
background bash job are skipped and retried by a later sweep. The config value
is re-read every sweep, so changing it does not require a daemon restart. A
sweep considers at most 500 roots per pass. Unarchiving refreshes the tree's
activity time, so an explicitly restored session gets a fresh idle window
instead of being archived again on the next sweep.

## Session export

`nexus session export ID` is an API client for `GET /api/session/{id}/export`
([api.md](api.md#session-export)). It writes a Markdown transcript or a
structured JSON document for the selected session (`--scope session`, the
default) or that session and its descendants (`--scope tree`). It never reads
SQLite and never calls a provider. `--format` is required.

```sh
nexus session export ses_… --format markdown --output transcript.md
nexus session export ses_… --format json --scope tree --output session.json
nexus session export ses_… --format markdown            # stdout (or --output -)
```

- **Files.** An existing destination is refused unless `--force` is passed. The
  download goes to a temporary `.part` file in the same directory and is
  published only after the complete response (exclusive hard link without
  `--force`, atomic replacement with it); a failure removes it and leaves any
  earlier destination untouched. Without `--output` (or with `-`) the export is
  written to stdout.
- **Archived trees.** They export as they are and are not unarchived.
- **Running trees.** The daemon captures one read boundary and labels the
  session's durable partial state; the turn continues. The CLI does not wait for
  or interrupt it.
- **Large logs.** The daemon pages the stored event log internally and streams
  the response; the CLI does not use the `/event` page limit.

## Session trees: who launched whom

Each session records `launched_by` (`user` or `agent`), `parent_id` and
`parent_call_id` (the parent's tool call that started it).

- **Agents.** An agent whose file lists `control: [session]` gets the `session`
  tool (`agent`, `prompt`, `wait`, `title`), but only in a root session. Among the
  built-ins that is `orchestrate`. A child never gets it, so trees are one level
  deep. The tool offers only agents whose `mode` allows a sub agent
  (built-ins: `build`, `quick`, `scout`, `plan`).
- **Modes.** A root session may run only `primary`/`all` agents (`orchestrate`,
  `build`), and a child only `subagent`/`all` agents. Creating a session or
  switching its agent with the wrong mode returns 400.
- **The child.** It runs in the parent's directory with the parent's
  permission and `disabled_tools`, and its own agent, model and messages.
  User-created children may explicitly override the inherited switches.
- **`wait: true` (default).** The tool returns the child's final answer, or its
  error: `Sub agent ID (agent A) was interrupted by the user.` or `… failed: ERROR.`
  A provider HTTP failure adds the provider's own reason from the response body
  (`… failed: copilot: HTTP 400 (model_not_supported: …).`). For a rejection
  (400, 401, 402, 403, 404, 413, 422) it adds that resuming on the same model will
  most likely fail the same way. For 429 it says to wait for the limit to reset or use another
  model (`failure_advice` in `runtime.py`). Clients' `session.error` text is unchanged.
  When the parent has `message_session`, a stopped child's report adds that it is
  idle, keeps its conversation, and should be resumed or redirected with
  `message_session` (naming its id) rather than replaced by a new child, and that
  its work is dropped only if the user says so. The `orchestrate` prompt says the
  same: continue a child whose task the new work belongs to, and treat an
  interrupted child's task as unfinished, not cancelled.
- **`wait: false`.** The tool returns at once. When the child finishes, its
  answer goes to the parent (`session.notified`): it is added before the
  parent's next model call if the parent is busy, or it starts a new parent turn
  if the parent is idle.
- **`escalate`.** Every sub agent (never a root) has `escalate {needed, why}`,
  recorded in its snapshot as `escalation: true`; an agent file cannot grant it
  and a switch can turn it off. It is a control tool: it grants nothing, asks
  nobody and does not wait. It records the block (each field at most 600
  characters, at most 5 per child until the parent reads them) and publishes
  `session.escalated` on the child. The child is told to stop and report. When
  the parent receives the child's report (the `session` result, or the
  `session.notified` text for `wait: false`), it ends with an `Attention:` block
  listing each `needed (why: …)`, saying the parent is the escalation point:
  resolve what it can, continue the child with `message_session`, and ask the
  user only for what the user alone can decide. The user sees it in the parent's
  timeline (the report) and the child's (the call and its result). A child
  session from before this existed has no `escalation` flag and does not gain
  the tool. Pending escalations are in memory: a daemon restart interrupts the
  turn that would have carried them.
- **`message_session`.** The first successful `session` call also exposes
  `message_session {session, message}` to the calling session, once, recorded
  in its snapshot as `messaging: {activated: true}` and announced with
  `session.message_tool`. That call's result carries the new tool's description;
  later requests include its schema. It messages only a direct
  child of the calling session (any other id is a tool error). A working child gets the message before its next model call
  (a steer); an idle child starts a new turn on its existing conversation. The
  call returns at once and the child's answer comes back as a `session.notified`
  notification, as for `wait: false`. It can be switched off in
  `disabled_tools`, and it is never bound inside `code`.
- **Forks.** the `fork` tool starts a child that copies the parent's request
  prefix: its snapshot, resolved model and effort, permission, switches and
  settled history (see [agents.md](agents.md#fork)). The session row records
  `fork = {parent_id, inherited}` (a JSON column, added in place to existing
  databases); `inherited` counts the leading messages copied from the parent,
  which keep their original timestamps and are published as `message.added`
  after `session.created`. Everything else (`parent_id`, `parent_call_id`,
  `launched_by: "agent"`, `wait`, notifications, `message_session` from the
  parent) is as for any child; reports name it `(fork)` instead of
  `(agent A)`. The daemon refuses every `session`, `fork` and `message_session` call a fork makes, with an explanatory error; only roots can fork. A fork is read-only (its `edit`, `write`, `patch`, workspace control and non-read-only MCP calls are refused) and each of its turns has at most 20 steps.
- **Users.** A user can start a child too: `POST /api/session {parent}`.
- **Interrupt and delete.** Interrupting a session interrupts its descendants'
  turns, and an interrupted parent is not woken by them. The session itself is
  cancelled first, then its descendants. A parent waiting on a `session` call
  therefore stops inside that call: it takes in no queued steer and sends no
  further request, so its timeline reads tool result → `interrupted`, and a steer
  queued before the interrupt stays queued for the next turn. The dangling call's
  result names the child, says it was interrupted too, and carries the same
  `message_session` hint. Interrupting only the child gives the waiting parent the
  child's interrupted report, and the parent's turn carries on. Deleting cascades.

## Turns

- One turn per session at a time; a second prompt gets 409. A `turn` row is
  written before its task starts, and each provider request and tool call
  records its own durable lifecycle state ([Crash recovery](#crash-recovery)).
- Each turn builds the loop's `Agent` from the session's stored snapshot
  (prompt, tools, steps), the live session settings (model, effort, permission,
  tool switches) and the saved messages. It does not reload agent files or
  `AGENTS.md`, so edits affect new sessions only. It runs in
  `session.directory` with the session's model, using the session id as the
  prompt-cache key (a fork uses its parent's id).
- A running call keeps the configuration it started with. Explicit model,
  effort, agent or tool-switch changes through the API apply before subsequent
  model/tool calls, including within a turn. Tool/agent notices enter context
  after outstanding tool results and before the next model request. An agent
  switch replaces the snapshot deliberately.
- **Old sessions.** A session from before `session_snapshot` existed gets a
  snapshot on first use, reconstructed from the current files. Its origin is
  `reconstructed` and its system prompt says this is not the exact historical
  prompt; after that it is stored and stable.
  `GET /api/session/{id}/snapshot` shows the full snapshot.
- **Approval.** read, `bash_output`, `bash_kill`, `ask`, `edit`, `write`, the
  outer code call, `session`, `message_session` and the workspace control tools
  never ask; `edit` and `write` are approval-free inside the session directory
  as fixed policy, and code delegates approval to its nested task calls. bash
  (including background) still asks unless the session is `allow` (the
  default). The turn waits on `permission.asked` until a client replies, or the
  turn is interrupted.
- **Questions.** `ask` publishes `question.asked` and waits with no daemon-side
  timeout. A client lists them (`GET /api/session/{id}/question` or
  `GET /api/question`) and replies with a complete answer set
  (`POST …/question/{qid}`). Questions are not approvals; an answer is data.
  Interrupting the turn, deleting its session/project, shutting down, or ending
  the enclosing code cell publishes `question.replied` with
  `outcome: "cancelled"`. A late answer gets 409 (404 once the session is
  deleted) and cannot resume the work.
  Each request resolves once ([tools.md](tools.md#ask)).
- **Interrupt.** Cancels the turn task, then its descendants'. A tool call left
  without a result gets an error result, so the conversation stays valid: "while
  this tool was running; its result is unknown" when it started, "while this call
  was waiting for approval; it did not run" when the approval was still pending,
  or "before this tool ran" otherwise. `session.error` is published with
  `kind: "interrupted"`.
- **Manual continuation.** `POST /api/session/{id}/continue` (CLI `/continue` or
  `nexus session continue ID`) starts an ordinary turn from the stored context
  with no new user message. It uses the current model, effort, tools and
  directory, and records requests, responses and tools normally. It refuses a
  running turn (409), an archived tree (409), an empty conversation (400), an
  unfinished turn record (409; restart so startup recovery reconciles it) and an
  unknown session (404). Before the request it closes any tool call a crash left
  dangling, using durable evidence and one labelled recovery notice, and never
  re-runs a call or reconstructs partial output. The turn's `data` records
  `origin: "continuation"` and a `predecessor` link (turn/request, or an honest
  `none` for a legacy conversation); the `session.status` busy/idle events carry
  the same provenance. A client must not repeat an ambiguous POST.
- **Failures.** Provider, sign-in and request errors end the turn with
  `session.error`. Saved messages stay as they are.
- **Unexpected failures.** Any other exception (a Nexus bug, or a store write
  failing on a full disk) is logged with its traceback and ends the turn as
  `failed` with `kind: "internal"` and the exception text. Tool calls left
  without a result are closed as for an interrupt, but the results say
  "Nexus failed during this turn (…)", and a running sub agent is not stopped.
  Queued inputs are held, as after an interrupt. Without this, the next prompt
  sent calls without results and the provider refused it. If these writes fail
  too, the turn is left for startup recovery.
- **Summary.** Every closed turn (completed, failed or interrupted) is followed
  by a durable `turn.summary` event before the idle `session.status`
  ([Turn summaries](#turn-summaries-and-the-spend-ledger)).
- **Sign-ins.** One sign-in object per provider for the daemon's lifetime, so
  OAuth access tokens are reused instead of refreshed every turn.
  `nexus login` writes `auth.json` and the daemon reads it on the next refresh.

## Turn summaries and the spend ledger

`nexus/store.py` keeps what clients show about a turn in the state tables, so
it never depends on the event log (archived plan: `git show e438b61:TURN_SUMMARY_PLAN.md`):

| Table / column | Holds |
| --- | --- |
| `turn.ended` | `{stop, steps}` from the loop's `TurnEnd`, written before the turn closes; `null` for a failed or interrupted turn |
| `turn_request.model`, `effort` | the `provider/model` ref and reasoning effort the request ran with, written at `StepStart` |
| `turn_request` `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_tokens` | the sum over the request's attempts that reported usage; `null` when none did |
| `turn_request.reasoning_seconds` | thinking time measured from reasoning deltas; `null` when no reasoning streamed |
| `turn_attempt` usage columns | each attempt's own last reported usage, so a retried or failed attempt's spend is kept |
| `session_usage` | per session and model: `requests` and the four token totals, plus `since` (when the row first counted) |

`Store.record_usage` updates the attempt, the request and the ledger in one
transaction and is idempotent for a repeated report. `GET /api/session/{id}/turn`
serves the summaries and the ledger ([api.md](api.md#turn-summaries)); a
`turn.summary` event follows every close.

**Retention.** `prune_turns` removes a turn with its requests, attempts and
tools, so its summary goes too; the response reports the oldest covered message
as `floor.seq`. `session_usage` references only the session, so totals survive
turn pruning and event retention and go only with the session. Event retention
removes raw wire traffic and deltas; it no longer takes a turn's footer data.

**Recovery.** A running turn with `ended` set had finished from the loop's
point of view, so startup recovery closes it as `completed` without reading the
event log; a turn left open with `ended` unset is interrupted
([Crash recovery](#crash-recovery)); the old `turn_end` event fallback is gone.

## Crash recovery

`Runtime.recover()` (`nexus/recovery.py`) runs while the daemon is starting,
before the app can accept a request. It never calls a provider and never runs a
tool. A clean shutdown is not recovery: it interrupts turns normally and closes
their records.

**What counts as unfinished.** A turn is unfinished when its `turn` row is
`running`, or its `recovery` marker is still `pending` from an interrupted
recovery. Within it, the durable records distinguish:

| record | meaning |
| --- | --- |
| no `turn_request` | the prompt was accepted before any provider request |
| `turn_request` `started` | a provider request was sent and its stream never finished |
| `turn_request` `completed`, `message_id` null | the response completed but its assistant message was not saved |
| `turn_tool` `saved` | the call is in a saved assistant message and never started |
| `turn_tool` `started` | the tool was about to run; its outcome is unknown unless a wait proves otherwise |
| `turn_tool` `finished` | the result is durable but never entered conversation history |
| `turn_tool` `inserted` | the result is in a saved tool message |
| unanswered `permission.asked` / `question.asked` | a wait the restart lost |

Busy state itself is in memory (`Runtime.turns`) and is not the recovery
authority; the durable turn and request rows are.

**What recovery does.**
- If the loop ended the turn before the crash (the turn's `ended` record) but the
  turn's closing record was lost, it finishes the turn as `completed` instead:
  nothing was interrupted from the model's point of view. It publishes the idle status and a
  `turn.recovered` with `outcome: "completed"` (including any runtime losses it
  can see), and appends no recovery notice.
- Otherwise it marks the turn `interrupted` and its session idle. It publishes a
  durable `session.status` `{"status": "idle", "reason": …, "turn_id": …}` once.
- Restores a provider response that durably reached `Done` from its saved
  `PartDone` parts, exactly as produced, including `origin` and `raw`. It never
  requests the provider again.
- Preserves durable tool results and inserts a missing tool message once. A
  started call with no result says its outcome is unknown and the workspace may
  have changed. A saved call in a recorded turn says it did not run. A started
  call whose approval
  was still pending, or was denied, also says it did not run. A pending `ask`
  is reported as cancelled; an already answered question is restored from its
  durable answer.
- Publishes a cancellation outcome for every unanswered question
  (`question.replied`, `outcome: "cancelled"`) and approval
  (`permission.replied`, `outcome: "cancelled"`, `allow: false`). A later reply
  cannot resume the work: an approval gets 404, and a question gets 409 while
  its recovery tombstone is held (404 after a later restart).
- Appends one new, clearly labelled `[Nexus recovery: interrupted turn]` user
  message with the evidence: the request's partial-output summary, each call's
  outcome, cancelled waits, and lost runtime state. It names a continuation
  predecessor when the interrupted turn was a manual continuation. It never
  edits earlier context.
- Publishes one `turn.recovered` event per recovered turn with the same
  outcomes in structured form, stored in the session's log. It carries the
  turn's `origin` (`prompt` or `continuation`) and its `continuation` link, so a
  restart does not lose why the turn started. A `turn.summary` follows for each
  turn it closed.

**Partial output.** An interrupted stream is not a completed response. Its last
`turn_request.partial` checkpoint retains text, reasoning and arguments, including
completed parts with their `raw` and `origin`, for inspection. It never enters
conversation history: doing so would
present partial text, partial arguments or partial reasoning as a finished
answer. Only a response whose `Done` is durable is replayable from the stored
parts. The session's next request gets the normal history plus the recovery
notice and any restored results.

**Runtime losses.** Kernels and background bash jobs are in memory. When a
recovered turn used code, its notice says kernels did not survive and that
variable/cell state was not invented; `code.call` and `code.cell` events remain.
A `bash.started` event without `bash.finished` is reported as a lost job record:
retained output is gone and the command may still be running. Recovery never
kills a process from a persisted pid, because a pid may have been reused;
process supervision is not implemented.

**Trees and archives.** Each session is recovered on its own. A parent is never
woken and a child never resumes; an interrupted `wait: false` child produces no
completion notice. Archive state is untouched, so an archived session stays
archived and idle.

**Repeatable.** Every step is idempotent: a message is inserted with the marker
that records it, a tool message is inserted with the `inserted` states of its
results, and recovery events are checked in the durable log before publication.
A crash during recovery followed by another start therefore appends no duplicate
result, notice, cancellation or completion event. New messages saved just before
a crash but not announced are announced once; pre-recovery history is marked so
it is never republished.
The bus still commits an event before publishing it; the message row records the
event id afterwards, and that link is what startup reconciles when it is
missing. Older databases without the execution tables open
normally. If such a session's last saved message ends in tool calls and it has no
turn row, recovery treats it as a pre-upgrade interrupted turn: it creates one
labelled legacy turn, closes the calls as **unknown** (it does not say they did
not run and invents no result) and appends
`[Nexus recovery: legacy interrupted turn]` once. A normal older conversation is
untouched, and every stored message id and event id stays the same.

**Not done here.** Recovery itself does not retry, continue a turn, answer a
wait or replay a mutation; those are separate bounded actions
([api.md](api.md#mutations-retries-continuation-and-reconnect)). Retention is
described under [Durable event log](#durable-event-log). A background job that outlives an orderly turn and
whose session had no interrupted turn is not reported individually after a
restart; its record and retained output are simply gone. The same applies to a
kernel from a session with no interrupted turn: nothing durable can prove it was
live, so recovery does not invent a loss report.

**ZMX children.** A turn whose tools the Claude Code or Antigravity CLI ran itself reconciles the same way, but its
recovery text says the CLI ran the tool and it **may have finished or still be running**; nothing is retried. For
Claude Code, the rest of the CLI's transcript is read once and added as labelled recovered output, and the CLI session
id is kept so the next prompt resumes. A CLI process that outlived the daemon is never killed automatically:
`nexus doctor` lists it from `$NEXUS10_HOME/workers/records/`, and `nexus workers clean` stops it only when the pid and
its start time still match ([external-workers.md](external-workers.md#recovery-and-orphans)).

## The CLI as a client (`nexus/cli.py`, `nexus/client.py`)

```sh
nexus run "fix the test"            # new session in the current directory's project
nexus run -c "and now the docs"     # continue the project's latest session
nexus chat --session ses_…          # continue a given session
nexus chat --wire                   # also print every request and provider event
nexus session continue ID           # resume from the stored context, no new user message
nexus session list [--all] | show ID | rm ID
nexus session archive ID | unarchive ID
nexus session list --archived-only | --include-archived
nexus session export ID --format markdown --output transcript.md
nexus session export ID --format json --scope tree --output session.json
nexus project list | open [PATH]
nexus manager ["one line"]          # typed chat with the Manager (docs/manager.md)
nexus manager episodes | audit      # its past conversations (open in the inspector) | what it did
```

The CLI subscribes to `/api/event?session=…` before it posts a prompt or
continuation, and renders events (a child's under a `── child … ──` header) until
every session in the tree is idle. It advances its durable cursor only after
applying an event, reconnects with bounded backoff, deduplicates by event id and
resyncs from `/api/snapshot` on `history_unavailable`, a cursor failure or
overflow. Prompt and continue POSTs carry one idempotency key per user action
(also reused across the client's transport retries); a reconnect never resends
them. `nexus session list` shows the tree. It answers `permission.asked`
with a y/N prompt. For `question.asked` it shows each question and its options,
collects the answers and submits them; Ctrl-C, EOF or an interrupted turn
abandons the prompt without submitting, and the daemon closes the question.
Ctrl-C during a turn sends `/interrupt`. `--yes` sets the session's permission
to `allow`. In chat, `/continue` resumes from the stored context without adding a
message, `/model provider/model` changes the session's model and `/context`
prints the next request body. `nexus session continue ID` is the standalone form:
it subscribes to the event stream before posting, renders the same events, and
handles questions and approvals. Neither form repeats the POST after an
ambiguous network failure. `-c`/`--continue` skips archived sessions when
choosing the project's latest; `--session` with an archived id stops with an
unarchive command.

## Not verified / not yet

- **Crashes.** Startup recovery reconciles turns a crash left unfinished
  ([Crash recovery](#crash-recovery)): it marks them interrupted, restores
  durable output and results, cancels waits and reports lost runtime state. It
  does not retry them; a new prompt or manual continuation is a separate client
  action ([api.md](api.md#manual-continuation)). A crash during recovery is itself
  recoverable. The crash windows are covered offline by reconstructed durable
  states and a subprocess restart smoke test; no live provider stream was killed
  mid-flight.
- **Turns after restart.** Turns are not resumed after a restart. Event streams
  resume; in-memory approvals and questions are cancelled visibly and cannot be
  answered later. Manual continuation after recovery is explicit. Startup
  recovery finishes idempotently and also resolves any keyed acceptance it
  closes.
- **Retention.** Diagnostic events expire by age and payload size, and the floor
  moves. Recovery and continuation evidence is pinned; lifecycle rows and
  mutation entries have separate retention. See
  [Durable event log](#durable-event-log).
- **Retries and replay.** Provider attempts are bounded and visible; clients can
  replay keyed mutations and reconnect from a durable cursor. An ambiguous
  external Git effect is still unknown, not transactional.
- **Daemon code.** Clients check `api_version` before they send anything else.
  They do not compare source. `nexus doctor` does: `/api/info` `code` is a hash
  of `nexus/**/*.py`, and a mismatch with the checkout is a failure. Run
  `nexus daemon stop` after changing core.

## Kernels

`nexus/kernels.py` owns a lazy Monty pool and up to eight persistent kernels,
private to each session by default, shared deliberately by explicit id. Forks cannot
borrow another session's kernel. Idle heaps are snapshotted in memory and restored
in fresh checkouts between cells; tool calls are never replayed. Workers start through
a launcher that clears their environment and working directory. Kernels outlive sessions. Deleting a kernel
stops it; daemon shutdown stops any running cell and closes every worker and the
pool. Kernels do not survive a daemon restart; startup recovery reports the loss
when it reconciles a turn that used code, and never invents kernel state. See
[tools.md](tools.md#code) for limits and cancellation behavior.

## Bash jobs

`nexus/bash_jobs.py` owns background commands started with `bash background=true`.
They run in the starting session's directory, are scoped to its tree for the
tools, and outlive turns and code cells. Deleting a session or project kills its
jobs; daemon shutdown kills every job. Archiving does not; if the session is
archived when the job finishes, `bash.finished` is still stored and its notice
is saved as context without starting a turn. The notice begins
`→ Background Shell Done: <command>`; limits, output retention and the rest of
the finish notice are in [tools.md](tools.md#background-bash-jobs). Jobs do not
survive a daemon restart. `bash.started` records the command's `pid`. Startup
recovery probes each unfinished job read-only (`bash_jobs.probe`): a live process
counts as the job only when `ps` shows it as its own process-group leader started
within 10 s of the recorded start; a missing process or a reused pid is `gone`,
and no pid or an unreadable `ps` is `unknown`. The recovery notice and the closing
`bash.finished` (`outcome: "lost"`, `process: {pid, state}`) say which. A verified
survivor is reported as still running, unsupervised and not stopped; the retained
output is lost. Recovery never signals a process, and never kills from a pid alone.

## Shared Rust build folder for session workspaces

`nexus/cargo_env.py`. Every process Core starts in a workspace (foreground and background
`bash`, composer `!` commands, integrated terminals) gets
`CARGO_BUILD_BUILD_DIR=<cargo home>/build/nexus-<12 hex of the Git common dir>` when the
workspace is inside a Git repository. The main checkout and all its linked worktrees share one
common dir, so a new worktree reuses the dependencies the others compiled; two unrelated
repositories never share. Cargo puts only intermediate artifacts there; final binaries stay in
each worktree's `target/`. The common dir is found by reading `.git` files (no `git`
subprocess) and cached per path; Code kernels start with an empty environment and are
unaffected.

Nothing is added when the user already chose: `CARGO_BUILD_BUILD_DIR` or `CARGO_TARGET_DIR`
in the daemon's environment, a `build-dir` line in any `.cargo/config.toml`/`.cargo/config`
from the workspace up to `/` or in `$CARGO_HOME`, or `tools.shared_rust_builds = false` in
`config.toml` (default `true`; config file only, no settings-screen row). The variable is
process environment, not model context, so nothing appears in the timeline.

Nothing removes these folders. `nexus doctor` shows a `rust builds` row with their count and
total size and the command to clear them (`rm -rf ~/.cargo/build/nexus-*`; the next build
recompiles). Builds in two worktrees at once wait on Cargo's lock for the shared folder.

## Workspace controls

The daemon owns the three workspace control tools (`nexus/workspace.py`): the
`expose_workspace_tools` entry point, `change_session_directory`, and `worktree`.
They are control-plane operations with API endpoints and stored events, not task
tools. No permission mode, approval, or `ask` question gates them.

- **Activation.** A new session's snapshot contains the entry point; the two
  detailed tools are absent until it is called. Activation adds their definitions
  to the snapshot and the model-visible result, and persists across restarts.
  Older sessions keep their snapshot and are activated explicitly through the
  API. The entry point is never available inside `code`.
- **Directory moves.** A validated move is applied at a call boundary, never
  inside a running tool call. While a turn runs, `POST …/directory` returns 202
  with `directory_change.status: "pending"`; the move lands before the next
  model or tool dispatch (or when the turn ends) and emits
  `session.directory_changed`. Timing, pending behavior and the announcement are
  in [tools.md](tools.md#change_session_directory-directory). The project is
  unchanged; children created later inherit the new directory.
- **Worktrees.** `GET`/`POST …/worktrees` list, create/reuse and delete. Create
  and delete share one lock. Deletion refuses the main checkout, dirty/locked or
  missing worktrees, and paths used by a session, a project or a pending move;
  branches are kept. Subdirectories and daemon-owned background jobs count as
  users too; reserved deletion paths block new users until removal finishes.
  Git cancellation kills and reaps its process group before the lock is released.
  See [tools.md](tools.md#worktree-action-path-branch-base).

```sh
nexus workspace expose --session ses_…
nexus workspace directory ~/src/other --session ses_…
nexus workspace list --session ses_…
nexus workspace create ../wt-feature --branch feature --session ses_…
nexus workspace delete ../wt-feature --session ses_…
```

The CLI resolves relative worktree and directory paths from the shell's working
directory before calling the API; API and model-tool calls resolve a relative
worktree path against the main checkout and a relative directory against the
session's current directory.

## File mutations

All Nexus file mutations in one daemon share an in-process asyncio lock (`Runtime.file_lock`); a multi-file patch
validates every file before writing any. There is no read-before-edit guard and no per-session file observations
([tools.md](tools.md#file-mutations)). The lock orders Nexus writers only: unrestricted `bash` and external
processes can still write at any time.

## Questions

The runtime keeps pending `ask` requests in memory, keyed by `qst_…`, with the
session id, the tool call id (including a nested `code` call's synthetic id) and
the validated questions. They are not approvals: answers never allow a tool call.
Interrupt, session/project deletion, daemon shutdown and a cancelled code cell
close them with an explicit cancellation event. The tool waits with no timeout
of its own. A daemon crash loses the in-memory wait, and startup recovery
publishes an explicit cancellation (`question.replied`, `outcome: "cancelled"`)
so a late answer gets 409 and cannot resume the turn.
Contract, limits and endpoints: [tools.md](tools.md#ask),
[api.md](api.md#endpoints).


CLI input is serialized across questions and approvals while the SSE consumer
keeps running. A question resolved or cancelled by another client dismisses its
input reader. Cancellable stdin reads use file descriptors without abandoned
input threads, including piped input; later lines are not prefetched.


Recovery delivery review: closing a turn leaves its recovery marker pending
until normal final cleanup is announced. Final idle events carry the turn id,
so startup closes a crash in that interval without duplicating the idle event.
An approval reply is committed before releasing its waiting tool; a persistence
failure leaves the tool waiting.


Continuation delivery review: execution evidence is linked to its saved assistant
message, including when call ids recur. Existing execution rows can be matched
through their request's message link; an unlinked record is treated as unknown
rather than borrowing an earlier result. Approval and question evidence used to
classify a call is restricted to records since its assistant message. A repeated
call id in a later model step starts a fresh execution state.

## Durable composer inputs

`nexus/inputs.py` stores pending text and image input and per-session queue
holds in SQLite. Inputs are delivered at model-call boundaries as user messages;
the delivered state and message are committed together. Queue acceptance with
an idempotency key is journaled with the input, so a lost response can be retried
without duplicating work. Interruption holds automatic dispatch. Startup holds
all pending work, converts undelivered steers into queued inputs, and never
starts a queued turn by itself. A hold ends when the last pending input is
removed. Pending inputs block session archive/deletion
unless explicitly dropped. See [Pending inputs](api.md#pending-inputs-steer-and-queue).
