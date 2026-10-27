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
  - `permission`: `ask` or `allow`.
  - `effort`: reasoning effort, or none for the provider's default.
  - `title`: set from the first prompt.
  - `archived`: whether this session's whole tree is archived. Archive state is
    separate from busy/idle `status`; see [Session archiving](#session-archiving).
  - `snapshot`: the frozen agent, prompt, tool definitions, child catalog and
    instruction file resolved at creation ([agents.md](agents.md#session-snapshots)).
- **Messages.** Append-only rows in session order, saved as each one is
  produced (user, assistant, tool). Parts keep their `raw` wire items, so a
  resumed session replays to its provider without loss.
- **Storage.** SQLite at `~/.nexus10/nexus.db` (WAL). Tables: `project`,
  `session`, `session_snapshot`, `message`, `event`, `file_observation`, `turn`,
  `turn_request`, `turn_tool` and `log`. `session_snapshot` is
  the JSON configuration frozen for one session; `event` is the durable event
  log, in publish order (see [Durable event log](#durable-event-log)); `log`
  holds that database's log generation and retention floor;
  `file_observation` holds each session's read observations (see below);
  `turn`, `turn_request` and `turn_tool` are the durable execution lifecycle
  ([Crash recovery](#crash-recovery)). A `message` row also records the id of the
  `message.added` event that announced it.
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
- **Failures.** A storage error from the log propagates out of `publish`; the
  event is not delivered and does not get an `id`. An event naming a session
  whose row is already gone is not delivered either; the tombstone covers it.
- **Not yet.** No retention or compaction runs: the log grows without limit and
  the floor stays 0. Retries, idempotent mutation requests and manual
  continuation (`/continue`) are separate (Step 11); a reconnect resumes the
  stream, not a turn. Startup recovery reconciles interrupted turns from the
  durable log and execution records ([Crash recovery](#crash-recovery)).

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
  outer code call and the workspace control tools never ask; `edit` and `write`
  are approval-free inside the session directory as fixed policy, and code
  delegates approval to its nested task calls. bash (including background) still
  asks unless the session is `allow`. The turn waits on `permission.asked` until
  a client replies, or the turn is interrupted.
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
- **Sign-ins.** One sign-in object per provider for the daemon's lifetime, so
  OAuth access tokens are reused instead of refreshed every turn.
  `nexus login` writes `auth.json` and the daemon reads it on the next refresh.

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
- If the loop's `turn_end` event is durable but the turn's closing record was
  lost, it finishes the turn as `completed` instead: nothing was interrupted
  from the model's point of view. It publishes the idle status and a
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
  restart does not lose why the turn started.

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

**Not done here.** No automatic retries, no `/continue`, no idempotent client
mutations and no retention. A background job that outlives an orderly turn and
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
every session in the tree is idle. The Python client (`Client.events`) understands durable ids
and can resume from a cursor; the CLI's turn view does not reconnect by itself. `nexus session list` shows the tree. It answers `permission.asked`
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
  answered later. Manual continuation after recovery is explicit.
- **Retention.** The event log has no expiry or compaction yet; every cursor
  stays resumable and the floor is 0. Turn, request and tool lifecycle rows also
  have no retention; they are small but accumulate one turn at a time.
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
is saved as context without starting a turn. Limits, output retention and the
finish notice are in [tools.md](tools.md#background-bash-jobs). Jobs do not
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
