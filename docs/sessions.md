# Sessions and durable state

Opening a new chat reserves an **in-memory draft**, not a saved session. Setup
and presence events remain in memory and snapshots are not written. The first
user message atomically saves the draft's buffered records and the session row.
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
- `records.body` is exactly `msgspec.json.encode(record)`: the bytes a JSONL
  export line carries, so the reducer, export, fork and replay read the same data.
- **Durability:** WAL, `synchronous=FULL`, `busy_timeout=5000`, every write in
  `BEGIN IMMEDIATE` so `seq` assignment is race-free across threads, processes
  and the several daemons sharing the file. Reads always see a committed prefix.
- **Secrecy:** file `0600`, parent dir `0700`, created before the first connection.
- `namespace = "main"` for user sessions, `"agents"` for subagent child sessions
  (their own real, replayable logs; logical id `<parent>/sub/<n>`, stored under a
  sanitised id with a hash suffix).
- Schema is versioned by `PRAGMA user_version` (`SCHEMA_VERSION` 3); the record
  envelope by `SESSION_LOG_VERSION` (1). A future runtime refuses old logs
  explicitly rather than misreading them.

`StateDatabase.project_sessions` supplies a bounded cross-project sidebar index
by joining session metadata to recorded project roots; it excludes archive,
trash and child namespaces and orders by newest activity.

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
  boundary ([loop.md](loop.md#steering-queue-interrupt)).
- **Crash recovery.** `recover_dangling_tool_uses()` appends an error
  `ToolResult` for every unresolved `ToolUse`, executing nothing.
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
  output ([context.md](context.md#compaction)).

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
| `list` / `summary` | `SessionSummary` (state `idle`/`running`/`awaiting_permission`/`awaiting_input`, viewers, title, activity) in one indexed query |
| `archive` / `unarchive` / `archive_stale` | hide without touching the log; the sweep archives up to 500 idle, unopened sessions older than `sessions.auto_archive_days` |
| `delete` / `restore` | move to trash in one transaction, never cancelling a turn; `purge_expired` removes rows past retention (default 7 days) |
| `export(id, format=)` | a consistent prefix as `json`, `markdown` or `jsonl` |
| `replay` | yield persisted events in order, read-only |

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

## Automatic titles

A session's title starts as the first line of the first user message
(`export.derive_title`, instant, `title_source = first_message`). When
`[sessions] auto_title` is on, `AutoTitler` (`host_support/auto_title.py`) then
makes **one small side call** to `[sessions] title_model` (the `low` tier by
default) and `SessionManager.set_auto_title` stores the result (`auto`).

- It is not a session: no loop, no tools, no memory, no events, and no cost in the
  session's usage. The turn never waits for it.
- Root sessions only (the check runs in `SessionStart` before the first message is
  logged); forks and subagents never start it. One task per session, two at once,
  cancelled when the session is deleted or the host stops.
- Input is the first message (≤ 2,000 characters, `…` marks a cut, attachments as
  `@name`) in `<message>` tags, treated as data. Output is capped (256 tokens; a
  reasoning model spends hidden tokens against it), cleaned to one line of ≤ 50
  characters (`clean_title`), with a 15 s timeout. The prompt is in `session/title.py`.
- A failure, timeout, refusal or empty reply keeps the first-message title. One
  daemon-log line records model, latency, tokens and the outcome; the title text
  is debug-only.
- `set_auto_title` is one conditional `UPDATE ... WHERE title_source =
  'first_message'`, so a title the user sets (reserved source `user`) always wins.
  Clients see the new title on their normal session-list refresh.
- Settings → Session titles switches it off and shows which model titles go to.

