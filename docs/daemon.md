# Daemon, projects and sessions

Nexus Core runs as one background daemon per Nexus home (`~/.nexus10`). The
daemon owns projects, sessions, turns, Monty kernels, background bash jobs,
tool approvals and pending questions, and exposes them over
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
  - `directory`: where tools run. It is the project path today, and will be a
    worktree later. A worktree is a session property set from the control
    plane, never a tool.
  - `agent`: default `build`.
  - `model`: `provider/model`, if the user chose one. Otherwise the agent's
    preference applies, then the daemon default.
  - `permission`: `ask` or `allow`.
  - `effort`: reasoning effort, or none for the provider's default.
  - `title`: set from the first prompt.
  - `snapshot`: the frozen agent, prompt, tool definitions, child catalog and
    instruction file resolved at creation ([agents.md](agents.md#session-snapshots)).
- **Messages.** Append-only rows in session order, saved as each one is
  produced (user, assistant, tool). Parts keep their `raw` wire items, so a
  resumed session replays to its provider without loss.
- **Storage.** SQLite at `~/.nexus10/nexus.db` (WAL). Tables: `project`,
  `session`, `session_snapshot`, `message` and `event`. `session_snapshot` is
  the JSON configuration frozen for one session; `event` holds every event of a
  session, in order: the durable log behind the [inspector](inspector.md).
  Deleting a project or session deletes everything under it, including its child
  sessions and snapshots.

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
- **Approval.** read, `bash_output`, `bash_kill`, `ask` and the outer code call never ask; code delegates
  approval to its nested task calls. bash (including background), edit and write ask unless the session
  is `allow`. The turn waits on `permission.asked` until a client replies, or
  the turn is interrupted.
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
nexus project list | open [PATH]
```

The CLI subscribes to `/api/event?session=…` before it posts the prompt, and
renders events (a child's under a `── child … ──` header) until every session in
the tree is idle. `nexus session list` shows the tree. It answers `permission.asked`
with a y/N prompt. For `question.asked` it shows each question and its options,
collects the answers and submits them; Ctrl-C, EOF or an interrupted turn
abandons the prompt without submitting, and the daemon closes the question.
Ctrl-C during a turn sends `/interrupt`. `--yes` sets the session's permission
to `allow`. In chat, `/model provider/model` changes the session's model and
`/context` prints the next request body.

## Not verified / not yet

- **Crashes.** A daemon crash mid-turn loses the in-flight assistant message;
  messages saved before it stay.
- **Turns after restart.** Turns are not resumed after a restart.
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
jobs; daemon shutdown kills every job. Limits, output retention and the finish
notice are in [tools.md](tools.md#background-bash-jobs).

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
