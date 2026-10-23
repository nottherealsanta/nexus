# Sessions and durable state

## Project identity in the shared daemon

A project's key hashes its full resolved canonical root. Main Git checkouts keep
their existing keys. Git subdirectories and linked worktrees use the main
checkout's key; a submodule remains its own repository. Non-Git directories use
the launch directory. Resolution walks at most 64 parent directories and reads
bounded Git metadata without running a subprocess.

Draft sessions also have project ownership before their first durable input.
Switching project focus does not move a session or cancel its subscriptions.
Existing independently keyed worktree rows are not migrated; export important
old sessions before adopting this identity model. Linked worktrees share this
project identity while their sessions can select independent working roots.


## Per-session working roots

A session belongs to a project and has a separate working root. No root event
means the project checkout (`main`). `session.root_set` records an `external`
linked worktree or a service-owned `worktree`, with branch/base/record metadata.
Its path is a bounded spelling relative to the project parent, including `..`
when necessary; the daemon reconstructs the absolute path and never treats the
display spelling as authorization. See [events-and-view.md](events-and-view.md).

The runtime lazily indexes the last root event for opened sessions (at most 512
entries, refreshed when the durable sequence changes). Replay and forks retain
the root. Before a turn or file/Git operation, it checks the canonical project,
linked-worktree common directory and reverse Git registration. Missing roots,
foreign repositories and copied `.git` pointers fail closed. Owned roots also
require an active service record at the expected path. Automatic creation,
promotion and recreation are implemented below; landing belongs to phase 06.

A new session opened after launching inside a user-made linked worktree adopts
that checkout as `external`. Reopening a saved session preserves its recorded
root. An in-root launch subdirectory initializes the fresh session's shell cwd;
the composer `!` command starts at the session root. Root changes hold the
session lock and require a turn boundary with no queued inputs or shell jobs.
External roots are never committed, removed or landed by root selection.

Settings, grants, extensions, memory and MCP connections remain project-owned.
`AGENTS.md` comes from the working root and is frozen for the turn. Worktree
extension edits become live after land. Selecting a root emits no routine
notice; the root is visible from session metadata. Filesystem-rooted MCP servers
continue seeing the project checkout; they are not cloned per root.

### Session worktree choice

The native session modal remembers a project preference: **No worktree**, **Ask
every time**, or **Always worktree**. Ask prompts once before a new session's
first input, and retains its answer for later turns. The preference lives in
machine state (`worktree-mode.json` in the project's state directory), separate
from repository settings. Without a remembered preference, `sessions = true`
means Always and the default is Ask. Noninteractive callers keep the existing
boolean selection API; unattended Ask sessions continue in the current checkout.
Changing the preference after a session starts affects future sessions only.
A selected worktree is created lazily on the first model turn; composer shell
commands require that first prompt to prepare it before they can execute.

**Named worktrees.** When a session goes into a worktree (Ask answered yes, or
Always) the user types a name at the first message. `SessionWorktreeSet(name=...)`
validates it (letters, digits, `-`, `_`; 1-60 characters; must start with a
letter or digit; branch `nexus/session/<name>` and the worktree folder must not
exist), records it in the `session.worktree_set` event (so replay and daemon
restart keep it) and stores it as the session's user title, which auto-titling
never overwrites. A draft has no session row until its first message, so the
title is stored again when the worktree is created on the first turn. That turn
creates the folder `<name>` and branch `nexus/session/<name>` with no random
suffix. Without a name (noninteractive `nexus run` under Always) the old
`<slug>-<token>` name is kept. A name is refused once the session has a root or
has started, and ignored when `enabled` is false.

### Owned session worktrees

`[worktrees] sessions = true` creates an isolated `nexus/session/...` branch on
the first turn of an eligible Git session. The selection can be changed only
while the session is an idle draft. Once a turn starts, it is locked; a main or
external session can later use the explicit promotion action to move into an
owned worktree. Promotion carries only verified successful file-tool writes,
commits those bytes in the new worktree, and leaves the original checkout
unchanged. Detach returns the session to the main checkout; it does not delete
the owned branch.

The worktree list includes these modern session records. Explicit removal is
available for an idle owner session when the authenticated record still points
to the exact registered checkout and branch. Cleanup refuses uncommitted,
untracked, ignored, or in-progress Git state and invokes Git without `--force`.
It removes only the checkout directory; the branch and active record remain so
the session can recreate its checkout from that branch on a later turn. A path
already missing is shown as already removed. Git errors that leave registration
or filesystem state inconsistent require manual Git repair; Nexus does not run
background cleanup or branch deletion.

Owned worktrees commit at turn boundaries. Harness commits are bounded by
`commit_max_files` and `commit_max_file_mb`; skipped commits leave changes in
the worktree for a later retry. Harness and agent-tool commits use `commit-tree` with signing
disabled, bypassing project hooks; explicit agent shell commits use
normal Git behavior. Setup commands, submodule initialization, copied files and
shared directories follow `[worktrees]` policy. Project-sourced setup requires
an explicit trust decision bound to the reviewed setup digest; stale reviews
cannot approve changed setup. One-time approval is consumed for the next setup,
while project approval is stored in private per-project state.

Manual integration is available through the host contract for one owned
session at a time. The preview shows its branch, target branch, commits and
changed files. After explicit confirmation, Nexus rechecks the preview and
fast-forwards the original local target branch only when both checkouts are
clean, idle, and the target still contains the session's base. A diverged or
stale state is refused with guidance. Integration never pushes or changes
another session.

The event log is the view contract: `session.root_set` identifies the selected
root, `worktree.created` and `worktree.setup_ran` describe provisioning,
`worktree.committed`/`worktree.commit_skipped` report commit outcomes, and
`worktree.promoted`/`worktree.detached` record lifecycle actions. The reducer
folds these into the normal conversation timeline and current root metadata;
clients render the event/view projection rather than inspect runtime-controller
or service records. See [events-and-view.md](events-and-view.md).

### Main-checkout checkpoints

Main and external-root sessions capture a hidden `refs/nexus/checkpoints/<session>/<turn>`
commit when the checkout changed during a turn. A first-turn baseline makes that turn's
diff meaningful. Capture uses a disposable Git index and `commit-tree`; it does not change
the user's `HEAD`, index, branch or files. The durable `checkpoint.captured` event records
the base, bounded file summary, foreign-change count and capture time. Capture runs at every
eligible main/external turn boundary, including when writes have no Nexus receipt (for
example, an MCP server edits the checkout). Only receipted file-tool writes, merged
subagent files and changes in a turn that started a shell are attributed to the session;
other changed files are counted as foreign. Diffs are review-only:
there is no restore action for a shared checkout. `[worktrees] checkpoints` defaults to true;
`checkpoint_keep` bounds retained session namespaces, and captures slower than five seconds
disable checkpoints for that session. Git refs remain local by default; `git push --mirror`
can publish them, so review the ref list before using mirror pushes. Submodules are captured
as gitlinks, not as snapshots of their contents. The terminal diff UI is planned in phase 08.


Opening a new chat reserves an **in-memory draft**, not a saved session. Setup
and presence events remain in memory and snapshots are not written. The first
user message atomically saves the draft's buffered records and the session row.
Promotion preserves the derived title source (`first_message`), so automatic
naming can replace that title without overwriting a user-set title.
An accepted `input.queued` submission also saves it immediately so queued user
work survives a daemon restart. Unsubmitted drafts disappear on restart and do
not appear in workspace or cross-project session lists. Existing records with
no user message or accepted queued input are hidden from those lists, not
purged. Assistant-only messages do not qualify a chat for listing.


`nexus/session/` owns the append-only record of every conversation. The log is
the source of truth: views, snapshots, exports and forks are all derived from it.

## Files

| File | Owns |
| --- | --- |
| `db.py` | `StateDatabase` (one SQLite file for every project) and `SqliteSessionStore` (scoped to one `(project_id, namespace)`) |
| `records.py` | `EventRecord`, `MessageRecord`, `SummaryRecord` (tagged, versioned), `ReadResult` |
| `session.py` | `Session`: the handle the loop and hosts use; turn lease, bus, queue, presence, detached turns |
| `manager.py` | `SessionManager`: open/fork/list/summary/archive/trash/restore/export/replay; `SessionSummary`, `TrashRecord`, `ArchiveRecord` |
| `snapshot.py` | derived, validated resume snapshots |
| `export.py` | `json` / `markdown` / `jsonl` renderers (`EXPORT_VERSION` 1, titles ≤ 80 chars) |
| `lock.py` | `SessionLock` (`flock`, exclusive/shared), `TrashLock` |
| `ids.py` | session-id validation (≤ 80 chars, `[A-Za-z0-9_-]` shaped) |
| `agent_selection.py` | durable root-agent choice |

## Storage

One SQLite database, `~/.nexus/nexus.db`, shared by every project and daemon.

| Table | Purpose |
| --- | --- |
| `projects` | workspace id (`project_key`), root, timestamps |
| `sessions` | `(project_id, namespace, id)` with `last_seq`, `completion_seq`, `last_activity`, `message_count`, `title`, `title_source`, `parent_id`, `fork_seq`, archive columns, trash columns |
| `records` | `(…, session_id, seq)` → `kind` (`event`/`message`/`summary`), `ts`, `body` |
| `snapshots` | one derived snapshot per session |
| `kv` | small per-project key/value |

- `title_source` (schema 2) says where the title came from: `''` (not set, or a
  row that predates the column), `first_message`, `auto` or `user`. Schema 2 means
  an older Nexus refuses the shared database ("newer than this Nexus").
- `completion_seq` (schema 3) is the latest durable sequence of a `turn.completed`,
  `turn.failed` or `turn.cancelled` event. Unlike `last_seq`, it ignores presence
  and other log activity, so session-unread acknowledgements remain stable when
  a view detaches or presence changes.
- `last_user` and `last_agent` (schema 4) are bounded display snippets for the
  sessions dashboard. They are the latest top-level text of a user prompt and an
  assistant reply (whitespace collapsed, 240 characters). Thinking blocks and
  tool results do not replace them. They are not fields of `SessionSummary`.
- `last_user_at` (schema 5) is the timestamp of the newest user prompt: a
  `user` message with at least one non-`ToolResult` block (tool results also carry
  the `user` role), or an `input.queued` event. It is the dashboard's ordering key
  and a field of `SessionSummary`, with `SessionSummary.order_time` =
  `last_user_at`, else `created_at`, else `last_activity`. `last_activity` keeps its
  meaning (newest record of any kind, used by auto-archive and the archive list),
  so assistant text, tool results and events never reorder the dashboard.
- `records.body` is exactly `msgspec.json.encode(record)`: the bytes a JSONL
  export line carries, so the reducer, export, fork and replay read the same data.
- **Durability:** WAL, `synchronous=FULL`, `busy_timeout=5000`, every write in
  `BEGIN IMMEDIATE` so `seq` assignment is race-free across threads, processes
  and the several daemons sharing the file. Reads always see a committed prefix.
- **Secrecy:** file `0600`, parent dir `0700`, created before the first connection.
- `namespace = "main"` for user sessions, `"agents"` for subagent child sessions
  (their own real, replayable logs; logical id `<parent>/sub/<n>`, stored under a
  sanitised id with a hash suffix).
- Schema is versioned by `PRAGMA user_version` (`SCHEMA_VERSION` 5); the record
  envelope by `SESSION_LOG_VERSION` (1). A future runtime refuses old logs
  explicitly rather than misreading them.

`StateDatabase.registered_projects` lists project roots that hold at least one eligible session
(main namespace, not manager, not trashed, with user input; **archived included**, so navigation
survives auto-archive), newest user activity first, with that `updated` time. A bare registration,
an empty project, bare-id roots and agent/manager/trash-only projects are not listed.
`StateDatabase.project_sessions` supplies a bounded cross-project sidebar index
by joining session metadata to recorded project roots; it excludes archive,
trash and child namespaces and orders by newest user prompt (`last_user_at`, else
`created_at`, else `last_activity`; ties by project then session id). Its optional `limit`
(default 1,000) bounds SQL results, and `query` (default empty, at most 256
characters) filters title, session ID, name, saved user/agent snippets and workspace
root before applying the limit. Search is case-insensitive literal substring
matching: `%` and `_` are text, not wildcard operators. It searches metadata,
not full conversation logs. `archived_project_sessions` takes an optional `limit`
(default 20), the same `query` and returns archived rows (newest archive first). Both
accept `hide_placeholders`, which leaves out throwaway sessions titled exactly `start` or
`go` (case-insensitive, trimmed; `PLACEHOLDER_TITLES`) in SQL before the limit, so they
never use up slots. It only applies to an unfiltered list: with a `query` they are
matched like any other session, and nothing is deleted. The host's dashboard listing
(`ProjectSessionsList`) enables it.

Legacy JSONL session directories are not imported. JSONL is an export format only.

## The `Session` handle

- **Turn ownership.** `begin_turn()` takes an exclusive lease (an `flock` file in
  `~/.nexus/locks/sessions/<project-hash>/`, so it holds across processes and
  dies with the process). A second turn fails fast with `SessionBusy`.
- **Detached turns.** `start_turn()` runs a turn with no consumer and returns its
  `turn_id`; any number of views `subscribe(from_seq)` (catch up from the log,
  then follow the live `Bus`, gap-free). `send()` is the attached wrapper whose
  early close still cancels.
- **Queue.** `enqueue()` persists a submission (`input.queued`) consumed at the
  next turn boundary. All submissions pending when a queue-backed turn starts
  are combined into one user message in queue order, separated by blank lines;
  attachments remain in order. Each original ID gets an `input.consumed` event
  for that same turn. Later arrivals stay queued for the following turn, and
  explicit input does not drain the queue. This applies to automatic starts,
  `start_turn(None)`, and `send(None)`; prompt hooks see the combined message.
  `consume_steering()` injects steering at a safe model
  boundary ([loop.md](loop.md#steering-queue-interrupt)). `move_queued(id, ±1)`
  swaps a pending submission with its neighbour and appends `input.moved` with the
  full new `order`; `remove_queued(id)` appends `input.dropped`
  (`reason: "removed by user"`). Replay (`_rehydrate_queue`, the reducer) applies
  both, so an edited queue survives a reopen.
  **Origin.** `input.queued` may carry `origin` (`{"kind": "live", ...}` for GPT Live,
  `{"kind": "subagent_report", "agent", "run", "status", "agents": [...]}` for a
  background subagent report). It is bounded (strings clip at 200, ints and string lists
  survive), kept on the queued item and restored by `_rehydrate_queue`. A
  `subagent_report` item skips the `UserPromptSubmit` hooks, is appended with
  `MessageMeta(notice="subagent_report")`, and **survives `cancel(drop_queue=True)`**
  (`retained_queued_ids`; the supervisor leaves it queued and uncounted). The count of
  reports waiting is `queued_report_count`.
- **Crash recovery.** `recover_dangling_tool_uses()` appends an error
  `ToolResult` for every unresolved `ToolUse`, executing nothing. Under the
  exclusive lock it also closes what a stopped process left open, once per
  handle: a last `turn.started` with no terminal event gets `turn.failed`
  (`reason: "interrupted"`); a `shell.started` with no completion gets a
  `shell.completed` with `status: "interrupted"` when no task in this process
  runs it and its recorded `pid` is gone. Every turn holds the lock and
  `turn.started` is written after the lease, so holding it proves the turn is
  dead. A reused `pid` can keep a dead `!` run open (not handled).
  Unfinished `external.started` workers get `external.finished` with status/reason
  `interrupted` and usage `unknown`, preserving worker session, agent and turn
  IDs. Every unclosed `agent.spawned` in the parent log (native or external)
  also gets `agent.completed` with status `interrupted`, unknown usage and an
  interruption error, before the parent's `turn.failed`. Native/nested children
  close in reverse spawn order, retaining `parent_agent_id` for replay routing.
  A background child that is running in this process (registered by the agent pool
  with `register_live_agent`) is skipped, so a second handle never closes it; an
  `agent.resumed` with no later `agent.completed` is an open run like an unclosed
  spawn, and an `agent.stopped` child has nothing left to close. The `agent.completed(interrupted)` it writes names the run (`run`: 1 for the spawn, the `agent.resumed` run number otherwise). `agent.resumed` is produced again by the foreground `subagent(resume=…)` continuation ([agents.md](agents.md#continuing-a-finished-subagent)); a follow-up that crashed mid-run is recovered by exactly this rule and closes `interrupted`, which makes the child not continuable. Legacy `agent.resumed` runs in old logs recover the same way. A continuation reopens the child's stored session (it never creates one), takes the child's turn lease like any run, and reads its immutable `agent.config` record from the child's own log; a child without that record (older logs, external workers) is not continuable.
  This covers crashes before child promotion or `turn.started` too. This
  parent-log recovery runs alongside owned-log recovery: proven child rows
  are traversed recursively using the project-scoped ownership index, and each
  child takes its own non-blocking exclusive lease before repair. A live child
  and its subtree are skipped. Unknown legacy logs and missing/unpromoted rows
  are not guessed or created. Descendant dangling tool uses receive error
  results; tools are never retried.
  Replay shows the recovered status; no tools are retried. The warning says the
  orphan Claude process may still be running: recovery does not kill it or claim
  it was stopped. The exclusive lease protects live workers; already-finished
  workers and child tasks are not closed twice.
- **Presence.** `attended` is derived from the live subscriber count; dropping
  to zero applies the unattended policy to pending approvals
  (`DEFAULT_UNATTENDED_DECISION = "deny_once"`, overridden by `on_unattended`).
- **Durable choices.** model (`select_model`), reasoning effort, root agent
  (next turn only), and per-session skill/MCP enablement (`select_extension`).
  `context_locked()` is true once `turn.started` is recorded: skill, MCP and
  root-agent changes are then rejected to keep the prompt cache valid. Start a
  new session to change them.
- **Summaries.** `append_summary` stores compaction artifacts as their own
  record kind, reused by `input_digest`; a summary is never mistaken for model
  output ([context.md](context.md)).

## Ownership storage (schema 6)

Ownership is independent of fork lineage (`parent_id`/`fork_seq`). Schema 6
adds kind/root and owner namespace/id/agent/turn/spawn-sequence columns, the
context-head/handoff watermarks, and indexes by project/root and project/owner.
Compatibility fields retain the logical child id, agent id and parent call id.
New child ownership metadata is published in the same transaction as its first
accepted input, never as an empty durable draft. Owner columns describe facts;
they do not grant authority or share conversation context.

Migration preserves legacy ids and backfills direct children only where a
persisted owner `agent.spawned` matches an existing hashed child log exactly.
Unknown or conflicting evidence keeps ownership empty; nested legacy inference
and removal of remaining logical-id routing are not yet implemented. Root rows
and the latest summary context-head watermark are backfilled without rewriting
record bodies. Collision-safe child allocation and full root metadata for
nested children remain follow-up work; schema 6 alone is not full P2 completion.

## Snapshots

A snapshot caches the current-state projection of a record prefix; it is used
only when it validates exactly against the log (version, schema, identity,
range, prefix equality, usage). Otherwise the full log is replayed. Written
every `session.snapshot_every` (20) completed turns. `records`, `events`,
`replay` and `fork` always read full history, so a snapshot never loses anything.

## Manager operations

| Operation | Behavior |
| --- | --- |
| `open(id)` | reuse the live handle; create/recover as asked |
| `fork(id, at_seq)` | branch into a new log (`parent_id`, `fork_seq`) |
| `list` / `summary` | `SessionSummary` (state `idle`/`running`/`awaiting_permission`/`awaiting_input`, viewers, title, activity, `last_user_at`) in one indexed query; `list` orders by `order_time` (newest user prompt first), ties by id |
| `archive` / `unarchive` / `archive_stale` | hide without touching the log; the sweep archives up to 500 sessions idle strictly more than one hour ([Automatic archive](#automatic-archive)) |
| `delete` / `restore` | move to trash in one transaction, never cancelling a turn; `purge_expired` removes rows past retention (default 7 days) |
| `export(id, format=)` | a consistent prefix as `json`, `markdown` or `jsonl` |
| `replay` | yield persisted events in order, read-only |
| `kind_of` / `manager_summary` | the row's kind; the singleton workspace manager (`kind = "manager"`, id `nexus-manager`), which `list`, the project index and the stale sweep never include and `archive`, `delete` and `fork` refuse ([manager.md](manager.md)) |

Host-side archive projections (`SessionListArchived`, `SessionSearch`,
`SessionPreview`) are bounded: pages ≤ 200, searches ≤ 50 sessions, tail reads
≤ 512 KiB (`host_support/session_archive.py`).

## Rules

- Never append from a UI or host without going through `Session`; never read the
  DB from a surface ([host.md](host.md)).
- New persisted state is a new event or record kind, folded by the reducer, so
  replay reproduces it. Bump the relevant version if an encoding changes.
- Tests: `tests/test_session_*.py`, `test_host_session_archive.py`,
  `test_runtime_shared_state.py`, `test_core_loop.py` (crash-resume).

## Automatic archive

The daemon owns the lifecycle; no UI takes part. At startup and every five
minutes (`ARCHIVE_SWEEP_INTERVAL_SECONDS`) it calls `SessionManager.archive_stale`
through `sweep_stale_sessions` (`host_support/session_archive.py`), which runs off
the event loop and then closes the cached handles of whatever was archived. The
archive is the durable `archived_at` marker (`reason = "auto"`), so it survives a
restart and the log is untouched. Unarchiving or sending a new message restores the
session as usual.

- **Threshold.** Idle strictly more than `AUTO_ARCHIVE_IDLE_SECONDS` (3,600 s):
  `last_activity < now - 3600`. Exactly one hour is not archived.
  The threshold is `[sessions] auto_archive_idle_seconds` (default 3600; `0`
  disables the sweep). The legacy `auto_archive_days` no longer sets a threshold,
  but `auto_archive_days = 0` still disables the sweep.
- **Last genuine activity.** `last_activity` is the newest appended record
  timestamp. Listing, summaries, exports and reads write nothing, and `presence.*`
  events (a view attaching or leaving) no longer advance it. Messages, turn and tool
  events, queued input and approvals do.
- **Never archived.** A running turn, queued input, a pending approval, a running background subagent (`has_live_agents`), a running `shell` job (the injected `busy_check`; such a session is also not evicted until the job ends or is stopped), a turn lease held by
  another process, or the workspace manager (`kind = "manager"`). An idle cached
  handle does not protect a session; the daemon evicts it after archiving. A
  session that is merely open in a viewer is **not** exempt, or an idle session left
  on screen would never archive.

## Automatic titles

A session's title starts as the first line of the first user message
(`export.derive_title`, instant, `title_source = first_message`). When
`[sessions] auto_title` is on, `AutoTitler` (`host_support/auto_title.py`) then
makes **one small side call** to `[sessions] title_model` (`openai/gpt-5-mini` by
default) and `SessionManager.set_auto_title` stores the result (`auto`).

- It is not a session: no loop, no tools, no memory, no events, and no cost in the
  session's usage. The turn never waits for it.
- Root sessions only. The check runs before the first message is logged, in
  `SessionStart` and in `HostFacade.enqueue` (so `SessionEnqueue`, which the
  native terminal uses, and direct host callers also title); the task starts only
  after the message is accepted, so a failed enqueue starts nothing. Forks and
  subagents never start it. One task per session, two at once, cancelled when the
  session is deleted or the host stops. A second message sent before the first is
  logged finds the running task and does not start another.
- Input is the first message (≤ 2,000 characters, `…` marks a cut, attachments as
  `@name`) in `<message>` tags, treated as data. Output is capped (256 tokens; a
  reasoning model spends hidden tokens against it), cleaned to one line of ≤ 50
  characters (`clean_title`), with a 15 s timeout. The prompt is in `session/title.py`.
- A failure, timeout, refusal or empty reply keeps the first-message title. One
  daemon-log line records model, latency, tokens and the outcome (`ok`, `empty`,
  `timeout`, or `error:<class>[:<http status>]`); the title text is debug-only.
- `set_auto_title` is one conditional `UPDATE ... WHERE title_source =
  'first_message'`, so a title the user sets (reserved source `user`) always wins.
  Clients see the new title on their normal session-list refresh.
- Settings → Session titles switches it off and shows which model titles go to.

# Session dashboard naming

The durable user-facing title remains **Sessions**, with `/sessions` as its
command. The dashboard has no preview pane and no preview command
(`SessionDashboardPreview` and `dashboard_preview` were removed). The archive
`SessionPreview` remains distinct and unchanged.

The Ratatui sessions page is a compact list grouped by status: Needs you, Working,
Idle (idle and finished-since-viewed) and Archived (the newest 20, collapsed). Within a
status group rows are ordered by newest **user** prompt (`order_time`), and the age uses
the same time, so agent output never shuffles the list. A Needs you row carries the
pending permission (`request_id`, summary, `ts`) and can be answered with `a`/`d` without
opening the session. The page has no composer; typing filters. `+ New Agent` starts a
session and Worktree opens the worktree list; `/resume` and `/archived` stay chat commands.
Opening a session in another workspace switches to that workspace.


### Ownership identity and read-only trees

Schema **7** adds durable child-allocation reservations on top of schema 6's
ownership columns. Every allocation uses `BEGIN IMMEDIATE`; ordinals survive
restart and concurrent daemons, including abandoned reservations. Existing
legacy rows and trashed ids are never reused. Reservations are not session
rows and do not appear in lists. `session/child_ids.py` preserves the old stable
logical-id mapping; lookup first checks exact stored ids and explicit logical
metadata, avoiding accidental double hashing. The mapping never establishes
ownership or grants access.

Child promotion atomically inserts the row, initial records and explicit owner
metadata. Nested children name their **physical owner session**, owner-local
spawn sequence and turn id, logical agent/call ids, and explicit root id.
`parent_id`/`fork_seq` still mean historical ancestry, not task/cancellation
ownership. Cancellation and recovery traverse only explicit owner edges.

Migration examines at most 10,000 relevant evidence records per project:
spawn/completion events, child request snapshots, and child turn metadata.
Only consistent recorded edges are adopted; relayed nested events do not lend
the root's sequence number to a child owner. Missing, conflicting, cyclic or
out-of-budget facts remain unknown rather than being reconstructed from slugs.
Existing summary records initialize `context_head_seq`; handoff execution is a
later phase. Doctor reports the project-local `unattached_children` count.

`SessionManager.tree` and host `SessionTree` are bounded read-only projections:
500 nodes including the explicit `unattached` collection; host depth is a fixed 8
(hard cap 64), independent of `agents.max_depth`. Reads neither open handles nor recover logs,
create lock files, or update project activity. Liveness comes from live handles
or existing OS turn locks, never old `turn.started` events. Dead unterminated
turns display `interrupted`. Fork relatives are not tree children. Display text
is sanitized and secret-redacted, unknown facts are null, and truncation is
explicit. `SessionList(include_children=True)` adds child summaries; its default
remains roots only. Summary child counts are direct; running descendants are a
conservative roll-up over at most 500 recently active child rows.

P3 native inspection uses `SessionTree` plus `SessionInspect`, never a UI-side
manager handle. Both native clients can traverse root/child ownership, inspect
bounded facts and durable child transcript/last request after restart, and return
to the root. Children are inspection-only. Tree usage is bounded latest-event
evidence; unavailable facts remain null, not zero or inferred. Root sublines
include running descendant and handoff counts. See [surfaces.md](surfaces.md#read-only-session-ownership-p3).

### Atomic batches and request admission

`SqliteSessionStore.append_batch(session, builders, row_updates=..., index_rows=...)`
assigns consecutive sequences under one `BEGIN IMMEDIATE`. Builders receive a
sequence and return a record; the store normalizes event session/sequence fields,
uses the same title/snippet/activity/completion bookkeeping as individual appends,
and commits bounded context-head row updates and submission indexes together.
Draft setup records promote in that transaction and remain retryable on any
failure, including COMMIT failure. Observers and live publication run only after
commit; a failed append must not change in-memory admission/permission state.

The host optionally admits `SessionStart`/`SessionEnqueue` with a
`client_request_id` (1–64 ASCII letters, digits, `_`, `-`). Its durable
`input.queued` record and `submissions` row commit together. Same session/id and
canonical content/mode/operation digest return the original ids (and original
admission queue depth), without appending, restarting a turn, interrupting again,
or requiring released attachment tokens. Different content returns
`RequestIdConflict`. No id retains the legacy path. Index rows cascade on session
deletion; no expiration sweeper is required.

Startup admission recovery is **read-only**. It finds live (not archived, not
trashed) sessions whose request-id admission has no `input.consumed` or
`input.dropped` event, and reports them through a `daemon.admissions_pending`
log line. It never drops input, writes events, takes a lease, opens or
unarchives a session, or schedules a turn; each session is checked
independently so an unreadable one does not hide the rest. Stranded and legacy
(no request id) queued input stays queued and is rehydrated on the next open;
it runs, combined FIFO, at the next turn start. A retry with the same request id
returns the original receipt and does not schedule it.

This is **admission idempotency, not exactly-once tool effects**. After a crash,
unconsumed input remains durable; duplicate retries do not launch work anew.
Recovery does not replay tools. A new request id is new work and can repeat
external side effects. Session locks, supervisor limits, permission policy and
normal turn-boundary prompt hooks remain in force.

### Same-session context handoff

`Session.handoff()` is an idle, nonblocking exclusive-lease operation; the
assembler's private automatic path already owns the turn lease. The summary,
marker, head/count and optional `input.queued`/submission index are one bounded
batch. A failed transaction publishes nothing. Retrying the same request ID
returns the original queued/turn IDs; conflicting payloads fail. A post-COMMIT
lost response is durable and does not cause a second cutover on retry.
Attachments, pending approvals, existing queue and child owners are not moved or
reassigned. No external tool effects are replayed. A continuation left queued
by a daemon crash remains durable; this does not introduce background workers.

### Immutable session names

Saved sessions receive an immutable ASCII adjective-color-animal name, globally unique across projects and root and child namespaces. Names survive title changes, archive, and trash; purged names are never reused. The name lives in the `sessions` row and the `session_names` table, not in the session log: no record is appended, so seqs stay stable. Exports carry it (JSON/Markdown via the summary metadata; JSONL as a synthesized leading `session.named` line with seq 0), and import (`create_from_records`) keeps a carried name when it is valid and free, otherwise allocates a new one. Drafts remain unnamed until persistence. Exact ids take precedence over exact, case-sensitive names; prefixes and Unicode lookalikes are not normalized. `nexus sessions show <name-or-id>` resolves without opening a handle. The host `SessionResolve` command accepts `ref` and optional `namespace`, returning canonical identity and lifecycle metadata; resolution does not grant access or revive deleted sessions. There is no rename operation.

### Context read authority

Context access uses host-owned allocation ancestry, never model/provider spawn
events. Child allocations deny by default and require explicit `context_read`
opt-in; external/unknown workers deny. Completion preserves access; missing,
trashed, purged or cross-project ancestors revoke it. See [context](context.md).

Name version 2 uses 229 adjectives x 56 colors x 188 animals = 2,410,912 base
names (about 21.2 bits); every word is 3-9 lowercase ASCII letters so a `-99`
suffix still fits 32 characters. These are display identities, not secrets or
access tokens. Global uniqueness is enforced by SQLite (`session_names.name` is
the primary key), with 16 random attempts then suffixes 2-99; exhaustion fails
explicitly rather than reusing a tombstone. Allocation happens in the same
transaction that creates the row. Existing sessions are named by a backfill in
deterministic batches of at most 500 per transaction (idempotent, resumes after a
crash). The legacy `session.named` event, if an old log contains it, is still
folded by the reducer; new sessions never get one.

Schema migration 9 is forward-only. Back up the state database before upgrade;
old binaries cannot safely open the upgraded schema. Do not downgrade in place.
`SessionResolve(include_retired=True)` explicitly permits tombstone lookup and
returns `retired: true`; default resolution refuses purged names. Retired results
are metadata only and cannot be opened.
