# HTTP API

Nexus Core runs as a daemon (see [daemon.md](daemon.md)). Every surface talks
to it over this API: the `nexus` CLI (`nexus-core/nexus/client.py`), and later
the TUI (`nexus-tui/`) and NSH (`nexus-nsh/`). The shape follows OpenCode's
`/api/*` (opencode.ai/v2/docs/api), cut down to what exists. Code:
`nexus-core/nexus/server.py`.

## Basics

- **Base URL.** `http://127.0.0.1:4747` (`$NEXUS_PORT`). Loopback only.
- **Auth.** `Authorization: Bearer <token>`, with the token read from
  `~/.nexus10/daemon.json` (mode 0600). `GET /api/event` also accepts
  `?token=` for browser `EventSource`. A missing or wrong token gets 401.
- **Bodies.** JSON objects, at most 1 MiB.
- **Errors.** `{"error": "..."}` with 400 (bad input), 401, 404 (unknown id)
  or 409 (a turn is already running).

## Endpoints

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| GET | `/api/bash` | | Background bash jobs (metadata: status, command, exit code, counts) |
| GET | `/api/bash/{id}` | | One job with its retained output (404 if missing) |
| DELETE | `/api/bash/{id}` | | 204; kills it (a finished job is unchanged) |
| GET | `/api/kernel` | | Live kernels, metadata, retained cells and variable previews |
| GET | `/api/kernel/{id}` | | One kernel (404 if missing) |
| DELETE | `/api/kernel/{id}` | | 204; 409 while busy |
| GET | `/api/info` | | `{name, version, pid, home, url}` |
| GET | `/api/provider` | | `[{id, label, dialect, base_url, catalog, default_model, connected, verified}]` |
| GET | `/api/model` | `?provider=` | `{source, models: [ModelInfo + ref]}` for connected providers |
| GET | `/api/model/default` | | `{model}` |
| GET | `/api/agent` | `?project=` or `?directory=` | `{agents: [Agent], problems: [{path, error}]}` |
| GET | `/api/agent/{name}` | `?project=` or `?directory=` | `Agent` with `prompt` |
| GET | `/api/project` | | `[Project]`, most recently used first |
| POST | `/api/project` | `{path, name?}` | the project at `path`, created if new |
| GET | `/api/project/{id}` | | `Project` |
| PATCH | `/api/project/{id}` | `{name}` | `Project` |
| DELETE | `/api/project/{id}` | | 204; deletes its sessions too |
| GET | `/api/session` | `?project=`, `?directory=` or `?parent=` | `[Session]` |
| POST | `/api/session` | `{project` or `directory` or `parent, agent?, title?, model?, permission?, effort?, disabled_tools?}` | `Session`; with `parent`, a child in the parent's directory (`launched_by: "user"`) |
| GET | `/api/session/{id}` | | `Session` |
| PATCH | `/api/session/{id}` | `{agent?, title?, model?, permission?, effort?, disabled_tools?}` (`""` clears model or effort) | `Session` |
| DELETE | `/api/session/{id}` | | 204; interrupts a running turn first |
| POST | `/api/session/{id}/model` | `{model}`; `""` returns to the agent's or default model | `Session` |
| POST | `/api/session/{id}/agent` | `{agent}` | `Session` |
| POST | `/api/session/{id}/workspace-tools` | | `Session`; activates the two on-demand workspace tools (idempotent) |
| POST | `/api/session/{id}/directory` | `{directory}` | `Session` + `change`; 200 applied/unchanged, 202 pending while a turn runs |
| GET | `/api/session/{id}/worktrees` | | `{repository, main, session_directory, worktrees: [...]}` |
| POST | `/api/session/{id}/worktrees` | `{action: "list" or "create" or "delete", path?, branch?, base?}` | the list, the created/reused worktree, or the deleted worktree |
| POST | `/api/session/{id}/prompt` | `{text}` | 202 `{status: "busy"}`; 409 while a turn runs |
| POST | `/api/session/{id}/interrupt` | | 204 |
| GET | `/api/session/{id}/message` | | `[Message]` in order |
| GET | `/api/session/{id}/event` | `?after=<event id>&limit=` (≤ 5000) `&type=a,b` | `{events, counts, more}`: the stored event log |
| GET | `/api/session/{id}/children` | | `[Session]`, direct children |
| GET | `/api/session/{id}/tree` | | the whole tree containing it, from its root: `Session` + nested `children` |
| GET | `/api/session/{id}/context` | | the provider request body the next turn would send |
| GET | `/api/session/{id}/snapshot` | | the full frozen session snapshot (agent, prompt, tools, instruction file, child catalog) |
| GET | `/api/session/{id}/permission` | | pending approvals |
| POST | `/api/session/{id}/permission/{pid}` | `{reply: "allow" or "deny"}` | 204 |
| GET | `/api/question` | | every pending `ask` question request |
| GET | `/api/session/{id}/question` | | pending questions for that session (empty list when none) |
| POST | `/api/session/{id}/question/{qid}` | `{answers: [{question_id, selected?, text?}, …]}` | 204; 400 invalid answer, 404 unknown question or session, 409 already resolved |
| GET | `/api/event` | `?session=` (that session and its descendants) | server-sent events (below) |
| GET | `/inspector` | | the session inspector page ([inspector.md](inspector.md)); static, no token needed to load it |

### Objects

```jsonc
// Project
{"id": "prj_…", "path": "/abs/path", "name": "repo", "created": 1793…, "updated": 1793…}

// Session
{"id": "ses_…", "project_id": "prj_…", "directory": "/abs/path", "title": "…", "agent": "build",
 "parent_id": null, "parent_call_id": null, "launched_by": "user", "children": ["ses_…"],  // who launched whom
 "model": "codex/gpt-6.1-terra", "model_source": "default",   // session | agent | default
 "effort": "high", "effort_source": "agent",                    // session | agent | provider
 "tools": ["read", "edit", "write", "ask", "code", "expose_workspace_tools"], "disabled_tools": ["bash"],
 "workspace": {"entry": true, "active": false},   // on-demand control state
 "starting_directory": "/abs/path",               // frozen into the system prompt
 "directory_changed": false, "directory_change": null,  // or {from, to, status: "pending", source}
 "permission": "ask", "status": "idle", "created": 1793…, "updated": 1793…,
 "snapshot": {"version": 2, "origin": "created", "created": 1793…, "directory": "/abs/path",
              "agent": {"name": "build", "source": "project", "path": "…/build.md", "fingerprint": "sha256:…"},
              "instructions": {"path": "…/AGENTS.md", "status": "loaded", "bytes": 123, "sha256": "sha256:…"},
              "current_instructions": {"path": "…/AGENTS.md", "status": "loaded", "bytes": 123, "sha256": "sha256:…"},
              "workspace": {"entry": true, "active": false}, "directory_changes": 0,
              "granted_tools": ["read", "bash", "edit", "write", "ask", "code"],
              "child_agents": [{"name": "scout", "description": "…", "mode": "subagent", "source": "builtin",
                                "path": "…/scout.md", "fingerprint": "sha256:…"}]}}

// Worktree entry (in the worktree list)
{"path": "/abs/wt", "head": "…", "branch": "feature", "bare": false, "detached": false,
 "locked": null, "prunable": null, "main": false, "exists": true, "dirty": false, "changes": 0}

// Agent (see agents.md)
{"name": "plan", "description": "…", "source": "builtin", "path": "…/plan.md",
 "model": null, "effort": null, "tools": ["read", "bash"], "steps": null}

// Message (parts encoded as in stream-events.md, including raw wire items)
{"id": "msg_…", "session_id": "ses_…", "seq": 2, "created": 1793…, "role": "assistant",
 "parts": [{"type": "tool_call", "id": "call_…", "name": "bash", "arguments": "{…}", "origin": "codex", "raw": {…}}]}

// Pending approval (also the data of permission.asked)
{"id": "per_…", "session_id": "ses_…", "tool": "bash", "call_id": "call_…", "args": {"command": "ls"}}

// Pending question (also the data of question.asked)
{"id": "qst_…", "session_id": "ses_…", "call_id": "call_…", "created": 1793…,
 "questions": [{"id": "db", "question": "Which database?",
                "options": [{"id": "pg", "label": "PostgreSQL", "preview": "…"}],
                "multiple": false, "allow_free_text": false}]}

// Answer item (POST …/question/{qid})
{"question_id": "db", "selected": ["pg"], "text": null}
```

Questions are separate from approvals: an answer is data for the model, never a
permission grant. Every question in a request must be answered exactly once.
Unknown question or option IDs, duplicate or missing answers, invalid
selection counts and text where the question does not allow it get 400 and leave
the request pending. A second reply to a resolved request (including after
cancellation) gets 409, with no second event or tool result; a bounded set of
recent resolutions is remembered, so a much older id gets 404 instead. Questions
live in memory and have no daemon-side timeout; they end with an answer or with
cancellation. Shapes and limits: [tools.md](tools.md#ask).

`permission` is `ask` (bash waits for a reply; read, `bash_output`, `bash_kill`,
`ask`, `edit`, `write`, the outer `code` call and workspace controls never ask)
or `allow` (nothing asks). Session-directory edits are approval-free by fixed
policy; see [tools.md](tools.md#read-observations-and-freshness). `status` is
`busy` while a turn runs.

### Workspace controls

`POST /api/session/{id}/workspace-tools` activates `change_session_directory` and
`worktree` (idempotent; also for sessions whose stored snapshot predates them)
and returns the session. The activation is stored with the snapshot. A new
session starts with only `expose_workspace_tools`, so `tools` reports it; older
sessions do not gain it until this call.

`POST /api/session/{id}/directory {directory}` validates an existing directory
(absolute, or relative to the current session directory) and moves the session.
Idle: **200** with `change.status: "applied"` (or
`"unchanged"`), the same forward announcement a tool call produces, and a
`session.directory_changed` event. Busy: **202** with
`change.status: "pending"`; the move is applied at the next tool/model dispatch
or when the turn ends. Session JSON shows the target in `directory_change` until
then. A second pending request gets 409. Errors: 400 for a missing/empty path or
a path that is not an existing directory, 404 for an unknown session.

`GET /api/session/{id}/worktrees` and
`POST /api/session/{id}/worktrees {action}` list, create/reuse or delete Git
worktrees for the session directory's repository. `path` is absolute or relative
to the main checkout. Create returns `path`, `branch`, `created`/`reused` and
does not change `directory`. Delete refuses the main checkout, dirty, locked or
missing worktrees, and paths used by a session, a project or a pending move; it
never deletes a branch. 400 carries Git's or the validation message; 404 is an
unknown session.
Limits and intent: [tools.md](tools.md#workspace-control-tools-on-demand).

## Event stream

`GET /api/event` is `text/event-stream`. The first event is
`server.connected`. Each later event is:

```
id: 42
event: text_delta
data: {"seq": 42, "time": 1793…, "type": "text_delta", "session_id": "ses_…", "data": {"type": "text_delta", "text": "Hel"}}
```

`seq` increases by one per event across the whole daemon, so a gap means
something was missed. With `?session=`, only events of that session and its
descendants, plus global events (`session_id: null`), are sent. A subscriber
more than 10 000 events behind gets `bus.overflow` and is disconnected.

Every event that belongs to a session is also stored (`GET
/api/session/{id}/event`). A client that reconnects catches up from the stored
log with `?after=<last event id>`.

| Type | Data |
| --- | --- |
| `project.created` / `project.updated` | `Project` |
| `project.deleted` | `{id}` |
| `session.created` / `session.updated` | `Session` |
| `session.snapshot` | a snapshot summary, emitted once when an old session is lazily reconstructed |
| `session.deleted` | `{id}` |
| `session.status` | `{status: "busy" or "idle"}`, before and after each turn |
| `message.added` | `Message`, once it is saved |
| `permission.asked` | pending approval |
| `permission.replied` | `{id, allow}` |
| `question.asked` | pending question request (`id`, `session_id`, `call_id`, `questions`, `created`) |
| `question.replied` | `{id, session_id, call_id, outcome: "answered" or "cancelled", answers, reason?}` |
| `session.notified` | `{from, text}`: a `wait: false` child finished, and its answer goes to this (parent) session |
| `session.workspace_tools` | `{active, entry, tools}`: the on-demand workspace controls were activated |
| `session.directory_changed` | `{from, to, branch, repository, worktree, source, project_id, frozen_directory, instructions}` |
| `worktree.listed` | `{repository, main, session_directory, worktrees: [...]}` |
| `worktree.created` / `worktree.deleted` | `{path, branch, head, repository, main, session_directory}` |
| `session.error` | `{kind: "provider" or "auth" or "request" or "interrupted", message, status?, body?}` |
| `bus.overflow` | `{dropped_after}` |
| loop and stream events | unchanged from [stream-events.md](stream-events.md): `wire_request`, `wire_response`, `wire_event`, `text_delta`, `reasoning_delta`, `tool_call_started`, `tool_args_delta`, `part_done`, `usage`, `done`, `step_start`, `tool_start`, `tool_end`, `turn_end` |

`wire_request` carries the exact request body and headers, with credentials
redacted. `wire_event` carries every provider SSE event as received. Nothing the
model sees is hidden from clients.

## A turn, end to end

1. Subscribe: `GET /api/event?session=ses_…` and wait for `server.connected`.
2. `POST /api/session/ses_…/prompt {"text": "…"}` → 202.
3. Events: `session.status busy` → `message.added` (user) → `step_start` →
   `wire_request` → `wire_event`… with `text_delta`/`tool_call_started`… →
   `part_done`… → `usage` → `done` → `message.added` (assistant) → `tool_start`
   → (`permission.asked` → client replies) → `tool_end` → `message.added`
   (tool) → `step_start` … → `turn_end` → `session.status idle`.
4. `POST …/interrupt` at any point cancels. A tool call left without a result
   gets an error result ("interrupted while running" or "before it ran"), so the
   next request stays valid. A pending question is closed with
   `question.replied`/`outcome: "cancelled"`; its answer can no longer arrive.

```sh
TOKEN=$(jq -r .token ~/.nexus10/daemon.json)
curl -N "http://127.0.0.1:4747/api/event?token=$TOKEN"           # watch everything
curl -H "authorization: Bearer $TOKEN" -X POST -d '{"path":"."}' http://127.0.0.1:4747/api/project
```

## Not yet

Login over the API (sign-in is the local `nexus login`), files and diffs,
forks, compaction, resuming `/api/event` from a sequence number (use the stored
log instead), pairing for remote clients.

Worktree listing, creation and deletion exist, but merge/integration and branch
cleanup are separate future operations. A session's directory can move anywhere
the user can reach: it is not confined to its project.

Kernel execution and metadata limits: [tools.md](tools.md#code). `code.call` and
`code.cell` are stored session events; `kernel.deleted` announces kernel removal.

Question requests are in memory like approvals. They are not restored after a
daemon crash or restart; an active turn does not survive either (Step 11 defines
reconnect and resync). Their schema, limits and cancellation behavior are in
[tools.md](tools.md#ask).

Background bash jobs: `bash.started` and `bash.finished` are stored session
events (the finish event carries a clipped output tail). A finish also adds a
notice to the starting session's context. Jobs are killed when their session or
project is deleted, and on daemon shutdown. Limits and lifecycle:
[tools.md](tools.md#background-bash-jobs).

`disabled_tools` on session create or patch is a list of task/control tool names.
Unknown names return 400 with the known names. An empty list re-enables all
agent-granted tools. `tools` reports what the next turn offers after agent
restrictions and switches. Children inherit switches when the field is omitted;
an explicit list on user-created children overrides that inherited setting.

A session freezes its agent file, rendered prompt, tool definitions and
`AGENTS.md` at creation ([agents.md](agents.md#session-snapshots)). Agent-file
and `AGENTS.md` edits therefore affect new sessions only.
`GET /api/session/{id}/context`, `GET /api/session/{id}/snapshot` and the actual
outgoing request all use the stored snapshot; they agree on `model`,
`instructions` and `tools` at any point where the message history is unchanged. A tool switch changes later request schemas
and appends a `[Nexus notice: tool availability changed]` user message; the
frozen system prompt is not rewritten. An agent switch rebuilds the snapshot
from the current files and appends `[Nexus notice: agent changed]`. A turn that
is already running keeps the model, effort, agent, prompt and tools it started
with.

`disabled_tools` accepts task tools, `session`, and the three workspace control
names. New sessions offer `expose_workspace_tools`; activating it adds
`change_session_directory` and `worktree` to later request schemas and to the
model-visible tool result, never to the frozen prompt. Activation persists with
the snapshot, and a pending directory move is visible in `directory_change`.

A session created before snapshots existed returns `snapshot.origin:
"reconstructed"` after its first use and carries a visible snapshot note. It is
not presented as the exact historical prompt; the reconstruction is stored once
and stays stable. It also starts with `workspace: {entry: false, active: false}`;
activate it explicitly if needed.
