# Daemon, projects and sessions

Nexus Core runs as one background daemon per Nexus home (`~/.nexus10`). The
daemon owns projects, sessions, turns, Monty kernels, background bash jobs,
tool approvals, pending questions, workspace control operations, session
archiving and pending directory moves, and exposes them over
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
  `session`, `session_snapshot`, `message`, `event`, `file_observation` and
  `log`. `session_snapshot` is
  the JSON configuration frozen for one session; `event` is the durable event
  log, in publish order (see [Durable event log](#durable-event-log)); `log`
  holds that database's log generation and retention floor;
  `file_observation` holds each session's read observations (see below).
  Deleting a project or session deletes everything under it, including its child
  sessions, snapshots, observations and session events. Archiving only changes
  one `archived` column on every session in the tree; it deletes nothing.

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
  the floor stays 0. Turn recovery, retries and resuming in-memory approvals or
  questions after a crash are separate (Step 11); a reconnect resumes the stream,
  not interrupted work.

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

- One turn per session at a time; a second prompt gets 409.
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
  this tool was running; its result is unknown" when it started, or "before this
  tool ran" otherwise. `session.error` is published with `kind: "interrupted"`.
- **Failures.** Provider, sign-in and request errors end the turn with
  `session.error`. Saved messages stay as they are.
- **Sign-ins.** One sign-in object per provider for the daemon's lifetime, so
  OAuth access tokens are reused instead of refreshed every turn.
  `nexus login` writes `auth.json` and the daemon reads it on the next refresh.

## The CLI as a client (`nexus/cli.py`, `nexus/client.py`)

```sh
nexus run "fix the test"            # new session in the current directory's project
nexus run -c "and now the docs"     # continue the project's latest session
nexus chat --session ses_…          # continue a given session
nexus chat --wire                   # also print every request and provider event
nexus session list [--all] | show ID | rm ID
nexus session archive ID | unarchive ID
nexus session list --archived-only | --include-archived
nexus session export ID --format markdown --output transcript.md
nexus session export ID --format json --scope tree --output session.json
nexus project list | open [PATH]
```

The CLI subscribes to `/api/event?session=…` before it posts the prompt, and
renders events (a child's under a `── child … ──` header) until every session in
the tree is idle. The Python client (`Client.events`) understands durable ids
and can resume from a cursor; the CLI's turn view does not reconnect by itself. `nexus session list` shows the tree. It answers `permission.asked`
with a y/N prompt. For `question.asked` it shows each question and its options,
collects the answers and submits them; Ctrl-C, EOF or an interrupted turn
abandons the prompt without submitting, and the daemon closes the question.
Ctrl-C during a turn sends `/interrupt`. `--yes` sets the session's permission
to `allow`. In chat, `/model provider/model` changes the session's model and
`/context` prints the next request body. `-c`/`--continue` skips archived
sessions when choosing the project's latest; `--session` with an archived id
stops with an unarchive command.

## Not verified / not yet

- **Crashes.** A daemon crash mid-turn loses the in-flight assistant message;
  messages saved before it stay. A clean shutdown writes the closing events; a
  crash can leave the last stored `session.status` as `busy` until a client
  reloads `/api/session` (turn recovery is Step 11).
- **Turns after restart.** Turns are not resumed after a restart. Event streams
  resume; in-memory approvals and questions do not.
- **Retention.** The event log has no expiry or compaction yet; every cursor
  stays resumable and the floor is 0.
- **Daemon version.** Nothing checks that a running daemon runs the same code
  as the client. Run `nexus daemon stop` after changing core.

## Kernels

`nexus/kernels.py` owns a lazy Monty pool and up to eight persistent kernels,
shared by session tree or explicit id. They outlive sessions. Deleting a kernel
stops it; daemon shutdown stops any running cell and closes every worker and the
pool. See [tools.md](tools.md#code) for limits and cancellation behavior.

## Bash jobs

`nexus/bash_jobs.py` owns background commands started with `bash background=true`.
They run in the starting session's directory, are scoped to its tree for the
tools, and outlive turns and code cells. Deleting a session or project kills its
jobs; daemon shutdown kills every job. Archiving does not; if the session is
archived when the job finishes, `bash.finished` is still stored and its notice
is saved as context without starting a turn. Limits, output retention and the
finish notice are in [tools.md](tools.md#background-bash-jobs).

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
before writing any. This orders Nexus writers only: unrestricted `bash` and
external processes can still write without these checks, and an external writer
can race the moment between the final check and the write.

## Questions

The runtime keeps pending `ask` requests in memory, keyed by `qst_…`, with the
session id, the tool call id (including a nested `code` call's synthetic id) and
the validated questions. They are not approvals: answers never allow a tool call.
Interrupt, session/project deletion, daemon shutdown and a cancelled code cell
close them with an explicit cancellation event. The tool waits with no timeout
of its own. A daemon crash loses pending requests along with the running turn.
Contract, limits and endpoints: [tools.md](tools.md#ask),
[api.md](api.md#endpoints).


CLI input is serialized across questions and approvals while the SSE consumer
keeps running. A question resolved or cancelled by another client dismisses its
input reader. Cancellable stdin reads use file descriptors without abandoned
input threads, including piped input; later lines are not prefetched.
