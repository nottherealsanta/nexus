# Daemon, projects and sessions

Nexus Core runs as one background daemon per Nexus home (`~/.nexus10`). The
daemon owns projects, sessions, turns, Monty kernels, background bash jobs,
tool approvals, pending questions, workspace control operations, session
archiving, pending directory moves and startup recovery, and exposes them over
the [HTTP API](api.md). Clients never read Nexus state files.

## Lifecycle (`nexus/daemon.py`)

| Command | What it does |
| --- | --- |
| `nexus serve [--port N]` | Runs the daemon in the foreground. |
| `nexus daemon start` | Starts it in the background if none answers, then waits until it does (15 s at most). |
| `nexus daemon stop` | Sends SIGTERM. Running turns are interrupted. |
| `nexus daemon status` | Prints the URL, pid and log path. |

`nexus run`, `chat`, `project` and `session` start the daemon automatically.

- **Discovery.** While it runs, the daemon writes `~/.nexus10/daemon.json` (0600)
  with `{url, pid, token, started}` and removes it on exit. A client trusts the
  file only after `GET /api/info` answers with the token.
- **Token.** A new random token is made on each start. It keeps other local
  users and web pages off the loopback port.
- **Port.** `$NEXUS_PORT`, default 4747, bound to 127.0.0.1 only.
- **Logs.** `~/.nexus10/logs/daemon.log`.
- **Same code.** The background daemon runs `python -m nexus serve` with the
  same interpreter as the client that started it.

## Projects and sessions (`nexus/store.py`, `nexus/runtime.py`)

- **Project.** A directory, unique by its resolved absolute path.
  `POST /api/project {path}` returns the existing project or creates one, named
  after the folder by default.
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
  - `model`: `provider/model`, if the user chose one. Otherwise the agent's
    preference applies, then the daemon default.
  - `permission`: `ask` or `allow` (default). An older database is upgraded once
    on open (migration `default_tool_allow_v1`): every stored `ask`, children
    included, becomes `allow` and its `revision` increases. An `ask` set after
    that is kept across restarts.
  - `effort`: reasoning effort, or none for the provider's default.
  - `title`: set from the first prompt (`Image` for an image-only prompt).
  - `archived`: whether this session's whole tree is archived. Archive state is
    separate from busy/idle `status`; see [Session archiving](#session-archiving).
  - `snapshot`: the frozen agent, prompt, tool definitions, child catalog and
    instruction file resolved at creation ([agents.md](agents.md#session-snapshots)).
- **Messages.** Append-only rows in session order, saved as each one is
  produced (user, assistant, tool). Parts keep their `raw` wire items, so a
  resumed session replays to its provider without loss.
- **Images.** A prompt may carry images (`POST …/prompt {text?, images?}`;
  [api.md](api.md#image-prompts)). `nexus/images.py` validates them before the
  turn is created: at most eight base64 PNG, JPEG, GIF or WebP data URLs whose
  bytes match their media type, 8 MiB decoded in total, a 1–255 character name.
  The user message is the text, then a `[Image A]`, `[Image B]`, … label and an
  `Image` part per attachment ([llm-providers.md](llm-providers.md#user-images)).
  The image bytes are stored in the message row, so they survive restarts,
  continuation, export and replay to any provider. The prompt route accepts
  bodies up to 12 MiB; every other route keeps 1 MiB.
- **Storage.** SQLite at `~/.nexus10/nexus.db` (WAL). Tables: `project`,
  `session`, `session_snapshot`, `message`, `event`, `file_observation`, `turn`,
  `turn_request`, `turn_tool`, `turn_attempt`, `session_usage`, `mutation`,
  `preference`, `migration` and `log`.
  `session_snapshot` is
  the JSON configuration frozen for one session; `event` is the durable event
  log, in publish order (see [Durable event log](#durable-event-log)); `log`
  holds that database's log generation, retention floor and head high-water
  mark; `mutation` is the idempotency journal; `turn_attempt` records provider
  retry attempts; `file_observation` holds each session's read observations (see
  below); `turn`, `turn_request` and `turn_tool` are the durable execution
  lifecycle ([Crash recovery](#crash-recovery)) and, with `session_usage`, the
  turn summaries ([Turn summaries](#turn-summaries-and-the-spend-ledger));
  `preference` holds machine-wide name/value settings (`Store.preference`,
  `set_preference`); `migration` names one-time data upgrades already applied.
  A `message` row also records the id of the `message.added` event that announced it.
  Deleting a project or session deletes everything under it, including its child
  sessions, snapshots, observations, execution records and session events.
  Archiving only changes one `archived` column on every session in the tree; it
  deletes nothing.

## Durable event log

`nexus/bus.py` commits every published event to `event` before any subscriber
receives it, and gives it its durable row `id`. `nexus/eventlog.py` turns that
id into a cursor, `<generation>:<id>`, and serves the reconnect stream.

- **Generation.** `log.generation` (`log_<hex>`) identifies this database's log.
  It is created on the first open of a database and kept across restarts. A new,
  deleted or replaced database gets a new generation; a copy keeps it. `/api/info`
  reports it with the current head and the retention floor.
- **Ordering and ids.** `event.id` is a never-reused autoincrement row id, so
  ordering is publish order and ids survive restarts. The bus `seq` is only the
  current daemon's counter and is not a cursor. An old database is migrated in
  place on open: the event table is rebuilt to allow `session_id: null` while
  every row id is preserved (and the autoincrement sequence continues).
- **Globals and deletion.** Session events are deleted with their session. Global
  events are stored with `session_id: null`: project create/update/delete and a
  minimal `session.deleted` tombstone (`{"id": "ses_…"}`). Deleting a session or
  project publishes one tombstone per deleted session, so a reconnecting client
  cannot miss a deletion that no longer has a session row. Statistics, messages,
  snapshots and observations still disappear with the session.
- **Reconnect.** `GET /api/event` accepts `?cursor=` or `Last-Event-ID`, registers
  the live subscription and reads the head atomically, replays the stored log up
  to that head in pages, then continues live while skipping the overlap. Cursor
  errors (wrong generation, ahead, malformed, below the floor) are 400/409/410
  JSON responses with the current head, not a silent "start now". The full
  contract, filtering and overflow are in [api.md](api.md#event-stream).
- **Retention.** `nexus/retention.py` removes only a contiguous prefix of old
  diagnostic events and advances `log.floor`; the head high-water mark remains.
  Events are pinned by an in-flight replay and protected by an unfinished
  turn/request, an open wait or a live background job. Messages, snapshots and
  model context never expire with the wire log. Defaults are 14 days and 256 MiB
  of retained payload, checked hourly (`NEXUS10_EVENT_RETENTION_DAYS`,
  `NEXUS10_EVENT_RETENTION_BYTES`, `NEXUS10_RETENTION_INTERVAL_SECONDS`).
  A `log.retention` global event reports pruning or storage pressure. Each pass
  removes at most 5,000 events in one transaction; later passes continue cleanup.
  A replay releases its pin when catch-up reaches the live queue. Terminal turn/request/tool
  rows are pruned after 30 days while keeping 200 turns per session (their
  summaries go with them; the `session_usage` ledger stays); mutation
  entries expire seven days after terminal resolution. Pressure is reported, not
  silently resolved by dropping incoming events: `log.retention` carries the
  removed count, batches, retained payload bytes and `over_budget` when the
  protected evidence itself exceeds the budget.
- **Reconnect.** `GET /api/event` accepts `?cursor=` or `Last-Event-ID`, registers
  the live subscription and reads the head atomically, replays the stored log up
  to that head in pages, then continues live while skipping the overlap. Cursor
  errors (wrong generation, ahead, malformed, below the floor) are 400/409/410
  JSON responses with the current head, not a silent "start now". The full
  contract, filtering and overflow are in [api.md](api.md#event-stream). The
  Python client reconnects with bounded backoff, deduplicates by durable id and
  resyncs from `/api/snapshot` on a cursor failure or overflow. Locally discovered
  clients refresh the daemon token on a restart; explicitly configured remote
  clients keep their supplied credentials.
- **Failures.** A storage error from the log propagates out of `publish`; the
  event is not delivered and does not get an `id`. An event naming a session
  whose row is already gone is not delivered either; the tombstone covers it.

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
  approval or question, gives 409 and is left alone. Archiving never interrupts
  or stops anything, and never removes a worktree, branch or background job.
- **While archived.** Messages, events, snapshots, file observations,
  directories and tree relationships stay readable. A prompt gets 409 with the
  unarchive command, and a new child in the tree gets 409, until the root is
  unarchived. Unarchive does not start a turn.
- **Notifications.** A background job may still finish. Its durable event is
  published as usual, and its model-visible notice is saved as a context
  message without calling the provider. The notice is there when the user
  unarchives and resumes.
- **Lists.** `GET /api/session` excludes archived trees by default;
  `?archived=only` returns archived trees and `?archived=include` returns both.
  Explicit session, tree, message, event and snapshot reads ignore the filter.

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
  deep. The tool offers only agents whose `mode` allows a child session
  (built-ins: `build`, `quick`, `scout`, `plan`).
- **Modes.** A root session may run only `primary`/`all` agents (`orchestrate`,
  `build`), and a child only `subagent`/`all` agents. Creating a session or
  switching its agent with the wrong mode returns 400.
- **The child.** It runs in the parent's directory with the parent's
  permission and `disabled_tools`, and its own agent, model and messages.
  User-created children may explicitly override the inherited switches.
- **`wait: true` (default).** The tool returns the child's final answer, or its
  error.
- **`wait: false`.** The tool returns at once. When the child finishes, its
  answer goes to the parent (`session.notified`): it is added before the
  parent's next model call if the parent is busy, or it starts a new parent turn
  if the parent is idle.
- **`message_session`.** The first successful `session` call also exposes
  `message_session {session, message}` to the calling session, once, recorded
  in its snapshot as `messaging: {activated: true}` and announced with
  `session.message_tool`. That call's result carries the new tool's description
  and guidance; later requests include its schema. It messages only a direct
  child of the calling session (any other id is a tool error). A working child gets the message before its next model call
  (a steer); an idle child starts a new turn on its existing conversation. The
  call returns at once and the child's answer comes back as a `session.notified`
  notification, as for `wait: false`. It can be switched off in
  `disabled_tools`, and it is never bound inside `code`.
- **Users.** A user can start a child too: `POST /api/session {parent}`.
- **Interrupt and delete.** Interrupting a session interrupts its descendants'
  turns, and an interrupted parent is not woken by them. Deleting cascades.

## Turns

- One turn per session at a time; a second prompt gets 409. A `turn` row is
  written before its task starts, and each provider request and tool call
  records its own durable lifecycle state ([Crash recovery](#crash-recovery)).
- Each turn builds the loop's `Agent` from the session's stored snapshot
  (prompt, tools, steps), the live session settings (model, effort, permission,
  tool switches) and the saved messages. It does not reload agent files or
  `AGENTS.md`, so edits affect new sessions only. It runs in
  `session.directory` with the session's model, using the session id as the
  prompt-cache key.
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
- **Interrupt.** Cancels the turn task and its descendants. A tool call left
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
- **Summary.** Every closed turn (completed, failed or interrupted) is followed
  by a durable `turn.summary` event before the idle `session.status`
  ([Turn summaries](#turn-summaries-and-the-spend-ledger)).
- **Sign-ins.** One sign-in object per provider for the daemon's lifetime, so
  OAuth access tokens are reused instead of refreshed every turn.
  `nexus login` writes `auth.json` and the daemon reads it on the next refresh.

## Turn summaries and the spend ledger

`nexus/store.py` keeps what clients show about a turn in the state tables, so
it never depends on the event log ([TURN_SUMMARY_PLAN.md](../TURN_SUMMARY_PLAN.md)):

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

**Upgrade.** The migration that adds these columns backfills once, in the same
transaction, from the events still retained: a request whose range (after its
`event_after`, up to the next request's) holds exactly one `usage` event gets
that usage on the request and its last attempt; the model is the bare id from
its first `wire_request` body and effort its reasoning effort. Anything else
stays unknown. The ledger is seeded from the filled requests.

**Recovery.** A running turn with `ended` set had finished from the loop's
point of view, so startup recovery closes it as `completed` without reading the
event log; only a turn left open by a daemon from before the column falls back
to the log's `turn_end` event ([Crash recovery](#crash-recovery)).

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
- If the loop ended the turn before the crash (the turn's `ended` record, or
  for a turn left open by an older daemon a durable `turn_end` event) but the
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

**Partial output.** An interrupted stream is not a completed response. Its
deltas, completed `part_done` items, `raw` data and `origin` stay in the event
log for inspection, but they do not enter conversation history: doing so would
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
- **Daemon version.** Nothing checks that a running daemon runs the same code
  as the client. Run `nexus daemon stop` after changing core.

## Kernels

`nexus/kernels.py` owns a lazy Monty pool and up to eight persistent kernels,
shared by session tree or explicit id. They outlive sessions. Deleting a kernel
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
survive a daemon restart. An unfinished `bash.started` is reported by startup
recovery when it reconciles that session's turn: the retained output is lost and
the command may still be running. Recovery does not kill it from a pid.

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

## File observations

The runtime gives each session a `FileGuard` (`nexus/observations.py`) backed by
the `file_observation` table. A direct `read` records the resolved path, content
hash, line ranges, clipped lines and read call id; a read inside `code` records
nothing because its output is retained by the kernel, and reads by another
session belong to that session. Observations persist, so a restart does not
forget what the model saw and never rebuilds an old observation from current
file contents.

`edit` and `write` (direct and nested) require a same-revision observation before
changing an existing file. Local changes need the affected lines, while
overwrite/delete/move need complete coverage; details and the clipping rules are
in [tools.md](tools.md#read-observations-and-freshness). A successful change
drops the observations it invalidated. All Nexus file mutations in one daemon
share an in-process asyncio lock (`Runtime.file_lock`), and the revision is
checked again inside it before writing; a multi-file patch validates every file
before writing any.
Startup recovery restores an observation only for a completed `read` whose saved
result it puts back into conversation history (and records its revision when the
result was saved). A read whose result is unknown grants nothing, so recovery
never authorizes an edit beyond content the model actually received.
This orders Nexus writers only: unrestricted `bash` and
external processes can still write without these checks, and an external writer
can race the moment between the final check and the write.

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
failure leaves the tool waiting. The message-link migration adds its column and
marks legacy rows in one explicit transaction.


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
starts a queued turn by itself. Pending inputs block session archive/deletion
unless explicitly dropped. See [Pending inputs](api.md#pending-inputs-steer-and-queue).
