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
| POST | `/api/session/{id}/prompt` | `{text}` | 202 `{status: "busy"}`; 409 while a turn runs |
| POST | `/api/session/{id}/interrupt` | | 204 |
| GET | `/api/session/{id}/message` | | `[Message]` in order |
| GET | `/api/session/{id}/event` | `?after=<event id>&limit=` (≤ 5000) `&type=a,b` | `{events, counts, more}`: the stored event log |
| GET | `/api/session/{id}/children` | | `[Session]`, direct children |
| GET | `/api/session/{id}/tree` | | the whole tree containing it, from its root: `Session` + nested `children` |
| GET | `/api/session/{id}/context` | | the provider request body the next turn would send |
| GET | `/api/session/{id}/permission` | | pending approvals |
| POST | `/api/session/{id}/permission/{pid}` | `{reply: "allow" or "deny"}` | 204 |
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
 "tools": ["read", "edit", "write", "code"], "disabled_tools": ["bash"],
 "permission": "ask", "status": "idle", "created": 1793…, "updated": 1793…}

// Agent (see agents.md)
{"name": "plan", "description": "…", "source": "builtin", "path": "…/plan.md",
 "model": null, "effort": null, "tools": ["read", "bash"], "steps": null}

// Message (parts encoded as in stream-events.md, including raw wire items)
{"id": "msg_…", "session_id": "ses_…", "seq": 2, "created": 1793…, "role": "assistant",
 "parts": [{"type": "tool_call", "id": "call_…", "name": "bash", "arguments": "{…}", "origin": "codex", "raw": {…}}]}

// Pending approval (also the data of permission.asked)
{"id": "per_…", "session_id": "ses_…", "tool": "bash", "call_id": "call_…", "args": {"command": "ls"}}
```

`permission` is `ask` (bash, edit and write wait for a reply; read, `bash_output`
and `bash_kill` never ask) or `allow` (nothing asks). `status` is `busy` while a
turn runs.

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
| `session.deleted` | `{id}` |
| `session.status` | `{status: "busy" or "idle"}`, before and after each turn |
| `message.added` | `Message`, once it is saved |
| `permission.asked` | pending approval |
| `permission.replied` | `{id, allow}` |
| `session.notified` | `{from, text}`: a `wait: false` child finished, and its answer goes to this (parent) session |
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
   gets an error result ("interrupted by the user"), so the next request stays
   valid.

```sh
TOKEN=$(jq -r .token ~/.nexus10/daemon.json)
curl -N "http://127.0.0.1:4747/api/event?token=$TOKEN"           # watch everything
curl -H "authorization: Bearer $TOKEN" -X POST -d '{"path":"."}' http://127.0.0.1:4747/api/project
```

## Not yet

Login over the API (sign-in is the local `nexus login`), files and diffs,
forks, compaction, worktrees (a session's `directory` is the project path),
resuming `/api/event` from a sequence number (use the stored log instead),
pairing for remote clients.

Kernel execution and metadata limits: [tools.md](tools.md#code). `code.call` and
`code.cell` are stored session events; `kernel.deleted` announces kernel removal.

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
