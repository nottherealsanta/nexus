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
- **Bodies.** JSON objects, at most 1 MiB; prompt requests allow 12 MiB for inline image data.
- **Errors.** `{"error": "..."}` with 400 (bad input), 401, 404 (unknown id),
  409 (a turn is already running) or 410 (history no longer retained; see
  [Event stream](#event-stream)).

## Endpoints

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| GET | `/api/bash` | | Background bash jobs (metadata: status, command, exit code, counts) |
| GET | `/api/bash/{id}` | | One job with its retained output (404 if missing) |
| DELETE | `/api/bash/{id}` | | 204; kills it (a finished job is unchanged) |
| GET | `/api/kernel` | | Live kernels, metadata, retained cells and variable previews |
| GET | `/api/kernel/{id}` | | One kernel (404 if missing) |
| DELETE | `/api/kernel/{id}` | | 204; 409 while busy |
| GET | `/api/config` | | `{revision, values, defaults, schema, errors}` for the initial settings |
| PATCH | `/api/config` | `{expected_revision, set, unset}` | updated config; stale revision is 409 |
| GET | `/api/info` | | `{name, version, api_version: {major, minor}, pid, home, url, log: {generation, head, head_cursor, floor, floor_cursor}, retention, retry, idempotency}` |
| GET | `/api/snapshot` | `?session=`, `?message_limit=` | projects, sessions, running turns, pending waits, optional bounded session view and an event watermark |
| GET | `/api/provider` | | `[{id, label, dialect, base_url, catalog, default_model, connected, verified}]` |
| GET | `/api/model` | `?provider=` | `{source, models: [ModelInfo + ref]}` for connected providers |
| GET | `/api/model/default` | | `{model}` |
| GET | `/api/agent` | `?project=` or `?directory=` | `{agents: [Agent], problems: [{path, error}]}` |
| GET | `/api/agent/{name}` | `?project=` or `?directory=` | `Agent` with `prompt` |
| GET | `/api/project` | | `[Project]`, most recently used first |
| POST | `/api/project` | `{path, name?}` | the project at `path`, created if new |
| GET | `/api/project/{id}` | | `Project` |
| PATCH | `/api/project/{id}` | `{name, expected_revision?}` or `If-Match` | `Project`; a stale revision is 409 |
| DELETE | `/api/project/{id}` | | 204; deletes its sessions too |
| GET | `/api/project/{id}/worktrees` | | the project checkout's worktrees (`repository`, `main`, `worktrees[]`, `project_directory`); 400 outside Git |
| GET | `/api/session` | `?project=`, `?directory=` or `?parent=`, plus `?archived=exclude\|only\|include` | `[Session]` |
| POST | `/api/session` | `{project` or `directory` or `parent, agent?, title?, model?, permission?, effort?, disabled_tools?}` | `Session`; with `parent`, a child in the parent's directory (`launched_by: "user"`) |
| GET | `/api/session/{id}` | | `Session` |
| PATCH | `/api/session/{id}` | `{agent?, title?, model?, permission?, effort?, disabled_tools?, expected_revision?}` (`""` clears model or effort; `If-Match` also accepted) | `Session`; a stale revision is 409 |
| DELETE | `/api/session/{id}` | | 204; interrupts a running turn first |
| POST | `/api/session/{id}/archive` | | `Session` for the whole root tree; idempotent |
| POST | `/api/session/{id}/unarchive` | | `Session` for the whole root tree; idempotent |
| POST | `/api/session/{id}/model` | `{model}`; `""` returns to the agent's or default model | `Session` |
| POST | `/api/session/{id}/agent` | `{agent}` | `Session` |
| POST | `/api/session/{id}/workspace-tools` | | `Session`; activates the two on-demand workspace tools (idempotent) |
| POST | `/api/session/{id}/directory` | `{directory}` | `Session` + `change`; 200 applied/unchanged, 202 pending while a turn runs |
| GET | `/api/session/{id}/worktrees` | | `{repository, main, session_directory, worktrees: [...]}` |
| POST | `/api/session/{id}/worktrees` | `{action: "list" or "create" or "delete", path?, branch?, base?}` | the list, the created/reused worktree, or the deleted worktree |
| POST | `/api/session/{id}/prompt` | `{text}` | 202 `{status: "busy", turn_id, origin, idempotency_key?}`; 409 while a turn runs |
| POST | `/api/session/{id}/continue` | `{}` | 202 with turn provenance; starts a turn from the stored context and adds no user message. 409 busy/archived/unreconciled, 400 empty, 404 unknown |
| POST | `/api/session/{id}/interrupt` | | 204 |
| GET | `/api/session/{id}/message` | | `[Message]` in order |
| GET | `/api/session/{id}/event` | `?after=<event id>&limit=` (≤ 5000) `&type=a,b` | `{events, counts, more, floor, head, generation, history_truncated}`: the retained stored event log |
| GET | `/api/session/{id}/turn` | `?after=<turn id>` or `?before=<turn id>`, `&limit=` (default 50, ≤ 200) | `{turns, more_before, more_after, floor, usage}`: turn summaries, oldest first, and the spend ledger; see [Turn summaries](#turn-summaries) |
| GET | `/api/session/{id}/children` | | `[Session]`, direct children |
| GET | `/api/session/{id}/tree` | | the whole tree containing it, from its root: `Session` + nested `children` |
| GET | `/api/session/{id}/context` | | the provider request body plus `used`, `window`, `exact` context budget |
| GET | `/api/session/{id}/snapshot` | | the full frozen session snapshot (agent, prompt, tools, instruction file, child catalog) |
| GET | `/api/session/{id}/export` | `?format=markdown\|json&scope=session\|tree` | a read-only Markdown transcript or JSON document; see [Session export](#session-export) |
| GET | `/api/session/{id}/permission` | | pending approvals |
| POST | `/api/session/{id}/permission/{pid}` | `{reply: "allow" or "deny"}` | 204 |
| GET | `/api/question` | | every pending `ask` question request |
| GET | `/api/session/{id}/question` | | pending questions for that session (empty list when none) |
| POST | `/api/session/{id}/question/{qid}` | `{answers: [{question_id, selected?, text?}, …]}` | 204; 400 invalid answer, 404 unknown question or session, 409 already resolved |
| GET | `/api/event` | `?session=` (that session and its descendants), `?cursor=<generation>:<id>` (or `Last-Event-ID`) | server-sent events (below) |
| GET | `/api/info` (handshake) | | `api_version` is `{major, minor}`. A client sends nothing past `/api/info` when the major differs; a newer minor only adds fields, so older clients keep working and say what they lack |
| GET | `/inspector` | | the session inspector page ([inspector.md](inspector.md)); static, no token needed to load it |

Configuration patch paths and polling behavior are described in [config.md](config.md).

### Objects

```jsonc
// Project
{"id": "prj_…", "path": "/abs/path", "name": "repo", "created": 1793…, "updated": 1793…,
 "revision": 1}

// Session
{"id": "ses_…", "project_id": "prj_…", "directory": "/abs/path", "title": "…", "agent": "build",
 "parent_id": null, "parent_call_id": null, "launched_by": "user", "children": ["ses_…"],  // who launched whom
 "model": "codex/gpt-6.1-terra", "model_source": "default",   // session | agent | default
 "effort": "high", "effort_source": "agent",                    // session | agent | provider
 "tools": ["read", "edit", "write", "ask", "code", "expose_workspace_tools"], "disabled_tools": ["bash"],
 "workspace": {"entry": true, "active": false},   // on-demand control state
 "starting_directory": "/abs/path",               // frozen into the system prompt
 "directory_changed": false, "directory_change": null,  // or {from, to, status: "pending", source}
 "permission": "ask", "status": "idle", "revision": 1,  // revision increments on mutation
 "created": 1793…, "updated": 1793…,
 "archived": false,                                // archive is separate from busy/idle status
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
or `allow` (the default for new sessions; nothing asks). Session-directory edits are approval-free by fixed
policy; see [tools.md](tools.md#read-observations-and-freshness). `status` is
`busy` while a turn runs.

### Turn summaries

`GET /api/session/{id}/turn` (since `api_version` 1.2) returns what a client
shows about each turn without reading the event log: status, steps, stop,
error, recovery, every provider request with its `provider/model` ref, effort,
token usage, reasoning time and attempts (retries), and each tool call's timing
and error flag. Tool arguments and output stay out; they are in the messages.

- No cursor: the newest `limit` turns. `after=<turn id>`: the oldest `limit`
  turns started after that turn (reconnect). `before=<turn id>`: the newest
  `limit` turns before it (load earlier). Turns are always oldest first.
- `more_before` / `more_after`: whether older / newer turns exist beyond the page.
- `floor.seq`: messages with a lower `seq` have no summary (their turns were
  pruned, or they predate turn records). `null` when every message is covered.
- `usage`: the session's spend ledger per model, every request and attempt
  counted. It outlives pruned turns. `since` is when its first row counted
  (`null` when nothing was counted); for an older session totals start there.
- `usage` on a request or attempt is `null` when unknown (no report, or a crash
  before the provider reported it), never zeros. A request's usage is the sum of
  its attempts'.
- `stop` is the loop's `TurnEnd.stop` (`ended`), else the last request's stop.
  `ended` is `{stop, steps}` once the loop ended the turn, else `null`.
- `message_id` ties a request to its assistant message; `start_seq` is the last
  message seq before the turn.
- Requests that predate this version were backfilled once from the events that
  were still retained: `model` is then the bare provider model id (no
  `provider/` prefix), and usage is filled only where exactly one `usage` event
  fell in the request's range.
- 404 for an unknown session; 400 for an `after`/`before` turn that is not in
  this session, both cursors at once, or a `limit` below 1.

```json
{
  "turns": [{
    "id": "trn_…", "status": "completed", "started": 1793241740.77, "finished": 1793241775.34,
    "start_seq": 150, "steps": 2, "stop": "end_turn", "ended": {"stop": "end_turn", "steps": 2},
    "error": null, "recovery": "none",
    "requests": [{
      "id": "req_…", "step": 1, "status": "completed", "model": "openai/gpt-6.1-terra", "effort": "low",
      "started": 1793241740.78, "finished": 1793241747.71, "stop": "tool_calls", "error": null,
      "message_id": "msg_…", "usage": {"input": 48880, "cached": 48384, "output": 89, "reasoning": 0},
      "reasoning_seconds": 2.1,
      "attempts": [{"n": 1, "status": "completed", "delay": null, "started": 1793241740.78,
                    "finished": 1793241747.71, "error": null,
                    "usage": {"input": 48880, "cached": 48384, "output": 89, "reasoning": 0}}]
    }],
    "tools": [{"call_id": "call_…", "name": "bash", "state": "inserted",
               "started": 1793241747.72, "finished": 1793241748.01, "is_error": false}]
  }],
  "more_before": false, "more_after": false, "floor": null,
  "usage": {"since": 1793241740.79,
            "models": {"openai/gpt-6.1-terra": {"requests": 1, "input": 48880, "cached": 48384,
                                                "output": 89, "reasoning": 0}}}
}
```

After every turn closes (completed, failed or interrupted) the daemon publishes
a durable `turn.summary` event whose `data` is that turn's entry, before the
`session.status` idle event. Startup recovery publishes one for each turn it
closes. A client replaces its copy of that turn with it.

### Archive and session lists

Archive state is per root tree: `POST …/archive` and `POST …/unarchive` apply
to the named root and every descendant in one transaction and are idempotent.
A child-only request gets 400 naming its root; the operation must start from
the root. The response is the root `Session` with `archived` set. Repeating an
operation that changes nothing returns 200 with no transition event.

`GET /api/session` filters by `archived`:

| Value | Returns |
| --- | --- |
| `exclude` (default, or omitted) | unarchived sessions only; archived trees are hidden |
| `only` | archived sessions only |
| `include` | every archived and unarchived session |

The filter applies to `?project=`, `?directory=` and `?parent=` lists; for
`?parent=` it filters the direct children. Any other value is 400. Explicit
`GET …/session/{id}`, `…/tree`, `…/children`, `…/message`, `…/event` and
`…/snapshot` ignore archive state, so an archived session stays readable and
inspectable.

Archiving is refused with 409 while the root or any descendant is running a
turn or waiting for an approval or question; nothing is interrupted. Archived
sessions refuse a prompt (409, naming the unarchive command) and refuse a new
child in the tree (409) until unarchived. Unarchive restores availability but
does not start a turn. Archive state never deletes messages, events, snapshots,
file observations, directories, worktrees or branches, and it does not stop a
background bash job. A background job's finish notice is stored as context but
does not wake an archived session; it is present when the user later unarchives
and resumes.

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

### Manual continuation

`POST /api/session/{id}/continue` starts one normal turn from the session's
stored context. It is an explicit client action: reconnecting, reading a
session, restarting the daemon and startup recovery never continue a session by
themselves.

The request contains no user text. The daemon sends the frozen system prompt and
the saved messages exactly as they are, in order; it appends no synthetic user
message such as "continue" or an empty message, and it rewrites no history. It
uses the session's current effective model, effort, tools, permission and
directory, records the new provider request, response and any subsequent tool
calls like an ordinary prompt, and supports interruption, approvals and
questions.

```jsonc
// 202 from POST /api/session/ses_…/continue
{
  "status": "busy",
  "turn_id": "trn_…",
  "origin": "continuation",
  "continuation": {
    "predecessor": {
      "turn_id": "trn_…", "request_id": "req_…", "status": "interrupted",
      "request_status": "interrupted", "step": 2, "stop": null,
      "link": "request", "legacy": false, "note": null
    },
    "reconciliation": null          // or {calls, tool_message, notice}
  },
  "reconciled": null
}
```

| Situation | Status |
| --- | --- |
| A turn is already running (`session.status` `busy`) | 409; nothing is interrupted |
| The root tree is archived | 409 with the unarchive command |
| The session has no stored messages | 400, saying to send a prompt first and naming the prompt endpoint/CLI |
| The session has an unfinished durable turn | 409; restart the daemon so startup recovery reconciles it |
| Unknown session | 404 |
| Missing or wrong token | 401 |

`predecessor` names the preceding turn and its last provider request when they
exist. `link` is `request`, `turn` (a turn with no request), or `none`; a legacy
conversation without execution records says so in `note` rather than inventing a
link. A crash during a continuation keeps this object in `turn.data`, and
startup recovery reports it in `turn.recovered`.

Before a provider request is made, every saved tool call must have a result. If
a crash left calls dangling, the continuation closes them from durable evidence
and appends one labelled `[Nexus recovery: continuation reconciled interrupted
tool calls]` user notice. A completed result is preserved; a started call says
its outcome is unknown and is never re-run; a saved call that never started says
so; a legacy call with no execution record is labelled unknown. The tool message
and notice are committed in one transaction, so repeating the continuation
sends the reconciled context unchanged and appends nothing again. Partial
provider output remains diagnostic history in the event log and is never
reconstructed into arguments or reasoning.

Continuation announces itself in the durable `session.status` `busy` event:
`{status: "busy", turn_id, origin: "prompt" | "continuation", continuation?,
reconciled?}`. The matching idle event carries the same `turn_id` and `origin`,
and startup recovery's `turn.recovered` carries `origin` and `continuation` too.
This is execution/API metadata; it is not an extra model-visible user message.
After an ambiguous network failure, retry only with the same retained
`Idempotency-Key` (see below). Without a key, inspect the session or event log
before taking another action: the turn may already have started.

### Mutations, retries, continuation and reconnect

These four actions are separate and never substitute for one another:

| Action | Owner | What is retried/replayed | Durable evidence | It never… |
| --- | --- | --- | --- | --- |
| Client retry of the same mutation | client + `mutation` journal | One keyed HTTP request, replayed with the original status/body | `mutation` row: key, fingerprint, operation, target, accepted, turn/resource, status/body, expiry | Creates a second turn or repeats an event |
| Provider request retry inside a turn | agent loop + `turn_attempt` | Only a pre-stream transport/429/502/503/504 failure, bounded by policy | `turn_request`, `turn_attempt`, `retry_scheduled/started/exhausted` events | Runs a tool again, appends the user message, or continues after the first stream event |
| Event-stream reconnect | client cursor + `Stream` | Replay of durable events after the last applied cursor | Event row id, log generation, floor/head, per-subscriber overflow | Resubmits a prompt or answers a wait |
| Manual continuation | `POST …/continue` | Nothing automatically; one explicit new turn | `turn.data.origin/continuation`, `session.status` busy/idle provenance | Retries a failed request, answers a question/approval, or resumes a recovered turn |

Timeouts and budgets belong to these owners: the client owns its own transport
timeout (and keyed transport retries), the retry policy owns pre-stream provider
attempts, the event stream owns queue/backpressure, and manual continuation has
no automatic retry. A question or approval is resolved only by its explicit
reply (or an explicit cancellation); a reconnect or continuation never answers
one.

#### Idempotency-Key

Every mutating request may carry `Idempotency-Key`. The key is 1–128 characters
from `A-Z a-z 0-9 . _ : -`. It is scoped to this daemon's database and log
generation. The fingerprint is `sha256` of the method, normalized target and
canonical JSON body plus the normalized `If-Match` revision precondition;
credentials are not included.

| Situation | Result |
| --- | --- |
| New key | Prompt/continue atomically journal acceptance with the turn; other mutations journal intent before running and the returned result afterwards |
| Same key, same fingerprint, after acceptance | The original status and body are replayed with `Idempotent-Replayed: true`; no work runs again |
| Same key, different payload/target | **409** `{"code": "idempotency_key_reused", …}`; nothing changes |
| Concurrent identical requests | They share one acceptance and one result; only one turn/resource is created |
| Key for a target deleted after acceptance | The stored result is replayed before the target is looked up; the target is not recreated |
| Rejected prompt/continue (4xx) | Not journaled; the key may be retried after the conflict clears (for example, prompt while busy) |
| Other keyed mutation returns 4xx | The returned failure is journaled; use a new action/key after correcting the request |
| Crash before an ordinary mutation's response was journaled | **409** `mutation_outcome_unknown`; inspect the target, never blindly repeat the effect |
| Unknown/absent key | Current single-attempt behavior; a lost response is ambiguous |

The deduplication lifetime is **seven days after terminal resolution**
(`NEXUS10_MUTATION_RETENTION_DAYS`). Prompt and continue journal their
acceptance **in the same SQLite transaction as the turn**, so a crash cannot
leave a turn without a replay record or a record without a turn. Unresolved
entries never expire. When a keyed turn finishes (normally or through startup
recovery), its entry becomes resolved and starts the lifetime. After expiry the
key must not be retried automatically. Retained response bodies are bounded at
64 KiB; a larger accepted response replays as a bounded accepted record that
names the operation, target, resource and turn, and says to inspect the
resource. Other mutation helpers commit their changes independently of the response
journal. Their durable intent prevents automatic repetition in that crash window;
startup resolves it to a stable, expiring 409 `mutation_outcome_unknown`. This is
not an exactly-once guarantee for external side effects.

Prompt/continue acceptance responses include `idempotency_key`. A replayed
prompt/continue returns the original `turn_id` even if that turn has since
finished or been interrupted; it never schedules work.

#### Revisions

`Project` and `Session` carry a monotonic `revision`. Reads report it.
`PATCH /api/project/{id}` and `PATCH /api/session/{id}` (and
`POST …/model`, `POST …/agent`) accept `If-Match: <revision>` or
`expected_revision` in the JSON body. A stale value is **409**
`{"code": "revision_conflict", "revision": <current>, "current_revision": <current>, "resource": …}`
before any change. A keyed update is replayed from its journal before the
revision is rechecked, so a retry returns the original accepted result; fetch the resource separately
for its current state. It does not apply the old update again.
Archive/unarchive, directory moves, title/history, agent/tool switches and
model/effort changes increment the session revision. The workspace snapshot and
message history remain the authoritative context.

#### Mutation route matrix

`Keyed` means the route accepts `Idempotency-Key` and replays an accepted
response. `Revision` means it accepts an expected revision. `Crash` is the
durable reconciliation for a lost HTTP response.

| Route | Keyed | Revision | Crash / replay behavior |
| --- | --- | --- | --- |
| `POST /api/project` | yes | — | A replayed create returns the same project id; deletion later does not recreate it |
| `PATCH /api/project/{id}` | yes | `If-Match` / `expected_revision` | Replay returns the original project; stale revision 409 |
| `DELETE /api/project/{id}` | yes | — | Replay is 204 even though the project and sessions are gone |
| `POST /api/session` | yes | — | Replay returns the same session; a child is not created twice |
| `PATCH /api/session/{id}` | yes | `If-Match` / `expected_revision` | Replay before recheck; stale revision 409 |
| `DELETE /api/session/{id}` | yes | — | Replay is 204; tombstones already recorded |
| `POST …/archive`, `…/unarchive` | yes | — | Replay is the original transition; repeating an unchanged state publishes nothing |
| `POST …/model`, `…/agent` | yes | yes | Replay is the original session; the new snapshot/notice is not rebuilt |
| `POST …/workspace-tools` | yes | — | Activation is idempotent; replay returns the activated session |
| `POST …/directory` | yes | — | Replay returns the original applied/pending result and does not move twice |
| `POST …/worktrees` | yes (journal only) | — | SQLite intent/evidence only. Git create/delete is an external effect: a crash leaves a pending intent that becomes **409** `mutation_outcome_unknown` (and expires after the retention lifetime). Inspect the repository; never blindly repeat a failed create/delete |
| `POST …/prompt` | yes | — | Acceptance + turn journal are atomic. A crash before the task starts is recovered as an interrupted turn, and replay returns the original `turn_id` |
| `POST …/continue` | yes | — | Same as prompt; replay returns the original continuation provenance |
| `POST …/interrupt` | yes | — | Replay is 204; it never resumes or answers a wait |
| `POST …/permission/{pid}` | yes | — | A resolved approval replays its 204; distinct contradictory replies still conflict |
| `POST …/question/{qid}` | yes | — | A resolved question replays its 204; a different answer without the same key is 409/404 |
| `DELETE /api/kernel/{id}`, `/api/bash/{id}` | yes | — | A replayed delete is 204; runtime resources are not required to still exist |

Except for atomic prompt/continue acceptance, all keyed routes persist intent
before execution and their response afterwards. The ordinary replay outcomes in
the table require a recorded response; a crash in between produces
`mutation_outcome_unknown`, including for creates, deletes and wait replies.
Git operations are also external effects and cannot be made transactional with
SQLite. An unknown outcome is never rerun blindly. Mutations without a key keep the old single-attempt semantics; clients
must not assume an unkeyed POST is safe to retry.

**Ordering.** Durable acceptance order is the order of the `mutation` rows and
events; a keyed replay never reorders work. Synchronous resource updates
(project/session settings, model/agent switches, tool switches, archive) apply
immediately and serialize in the daemon's event loop; a stale expected revision
is rejected before anything changes. A directory move requested while a call is
running is queued and applied at the next model/tool dispatch boundary (or turn
end), never inside the running call. An interrupt cancels the active turn and
its descendants; it never answers a pending question or approval. New prompts
while busy are **409** until Step 12a adds steer/queue, whose insertion point is
after outstanding tool results and before the next model call (steer) or the
next turn (queue).

#### Bounded provider retries

A provider request is retried within its agent step only when the failure
happens **before any response stream event** and is a transport error, `429`,
`502`, `503` or `504`. Authentication, validation, cancellation, persistence
and unknown errors are terminal. An HTTP `Retry-After` is honored when it fits
the remaining budget; otherwise the retry stops and reports it.

Defaults (`NEXUS_RETRY_MAX_ATTEMPTS`, `NEXUS_RETRY_BASE_DELAY`,
`NEXUS_RETRY_MAX_WAIT`, `NEXUS_RETRY_MAX_ELAPSED`): 3 attempts total,
exponential backoff with jitter from 1 s, at most 30 s per wait and 60 s of
retry time per step. Each attempt is recorded in `turn_attempt` and visible as
`retry_scheduled` / `retry_started` / `retry_exhausted` events with the attempt
number, delay, status, provider message and `Retry-After`. Once a stream event
arrives, failure is terminal; its partial output stays diagnostic. No retry
ever re-runs a tool, an outer `code` cell, a background command, approval or
question, and a daemon restart never resumes a pending retry. A settings change
waits for the next logical request; `self.request()` is unchanged across a
retry, so a retried attempt cannot silently change contents.

#### Consistent snapshot

`GET /api/snapshot?session=<id>&message_limit=<1..1000>` returns state at one
watermark:

```jsonc
{
  "watermark": {"generation": "log_…", "head": 4200, "floor": 0, "head_cursor": "log_…:4200"},
  "projects": [Project], "sessions": [raw session rows],
  "running": [turn], "approvals": [pending approval], "questions": [pending question],
  "selected": {"session_id": "ses_…", "messages": [Message], "messages_limit": 200,
               "messages_more": false, "oldest_seq": 1}
}
```

The watermark is read in the same SQLite read transaction as the lists and the
bounded selected-session view. Apply events strictly after `head`; do not infer
loss from gaps in a filtered stream. If a cursor returns `history_unavailable`,
`cursor_generation`, `cursor_ahead` or overflow, fetch this snapshot, discard
stale pending waits, and resubscribe after `watermark.head_cursor`. Pending
questions/approvals are current in-memory waits only; the durable cancellation
events from a restart dismiss old waits. If the selected session was deleted
while a client was away, `GET /api/snapshot?session=` is 404; the Python client
retries the snapshot globally and emits `client.resync` with
`data.snapshot.deleted_session`, so the session view can close. A snapshot read
never mutates state, starts a turn or answers a wait.

### Session export

`GET /api/session/{id}/export` returns a read-only export of one session or its
descendants. It is an ordinary authenticated API call: the CLI uses it, and no
client reads SQLite.

| Query | Values | Meaning |
| --- | --- | --- |
| `format` | `markdown`, `json` | Required. |
| `scope` | `session` (default), `tree` | `session` exports the selected session only. `tree` exports the selected session and all of its descendants (never its ancestors or siblings), so exporting a child includes that child's own subtree. |

Responses:

| Format | `content-type` | Filename |
| --- | --- | --- |
| `markdown` | `text/markdown; charset=utf-8` | `nexus-<session id>-<scope>-<UTC timestamp>.md` |
| `json` | `application/json; charset=utf-8` | `nexus-<session id>-<scope>-<UTC timestamp>.json` |

The filename is sent as `content-disposition: attachment; filename="…"`.
`x-nexus-export-format-version` is `1`; `x-nexus-export-boundary` reports
`event:<event id>;seq:<bus seq>` for the read boundary,
`x-nexus-log-generation` names the log those ids belong to, and
`x-nexus-log-floor` is the retention floor at the boundary. A missing or unknown
`format` and an unknown `scope` are 400, an unknown session is 404, and a
missing or wrong token is 401.

Archived sessions export normally and are not unarchived; an archive state is
the same before and after the export. There is no export without a session.

#### Read-only contract and boundary

One export opens its own SQLite connection in read-only mode and reads
everything inside one transaction. Session rows, snapshots, messages and events
therefore come from one coherent database view even while turns keep writing;
the watermark (`boundary.log_generation`, `boundary.event_id`, `boundary.seq`,
per-session counts and the newest `session.updated`) names that view. `event_id`
is the durable boundary (a resume cursor is `<log_generation>:<event_id>`);
`seq` is the publishing daemon's counter and is informational only. The read
transaction is held while
the response streams. Writes after the boundary are not in the export, which is
why a running session's export is labelled durable partial state.

An export never calls a provider, starts or interrupts a turn, activates a tool,
writes a notice or event, reconstructs a missing snapshot, or reads a workspace
file or a credential. A running session exports what is durably stored at the
boundary; it is not interrupted or waited for. The JSON event log may contain
stream deltas that had not yet been saved as a message.

Events are read in pages of 1,000 by event row id until a short page; this is
not the `/event` endpoint's 5,000-event page cap, and there is no `limit` query
parameter. Both formats stream. The export includes every event **retained at
the boundary**; when retention has moved the floor, `boundary.history_truncated`
is true, `boundary.floor` names the expired boundary, and the Markdown header
says so. Messages and snapshots are not affected by event expiry, and the export
never claims an expired wire event was present.

Only top-level transport `headers` on `wire_request` and `wire_response` events
are passed through provider header redaction (`authorization`,
`proxy-authorization`, `cookie`, `set-cookie`, `chatgpt-account-id`,
`x-openai-internal-codex-residency`, `x-api-key` become `***`). This applies to
transport metadata only; message text and tool arguments/results are preserved
as stored, with no text-pattern redaction.

#### Markdown

A readable transcript. It starts with the export metadata and a tree outline,
then one section per session, in order: the session's fields (agent, model,
effort, directory, parent/child relationships, archive state, snapshot origin
and instruction-file status), a snapshot note, the stored snapshot's full
system prompt and tool definitions, and the stored messages in order. Snapshot
instructions are labelled as the current stored snapshot, not a reconstruction
of every historical request. Each message shows its role, id and stored sequence; parts are labelled
`Text`, `Reasoning`, `Tool call` and `Tool result`. Reasoning shows the readable
summary; encrypted reasoning is labelled as present but unreadable and says it
is replayed unchanged. Tool arguments and results are fenced; arguments are
pretty-printed only when they are valid JSON (the JSON export keeps the stored
text exactly). A user message that starts with `[Nexus notice:`,
`[Nexus recovery:` or `[Notification:` is labelled a context notice. Child
conversations stay in their own session sections, never inside the parent's
messages.

No additional clipping is applied: existing clip notices are reproduced as
stored. Fences are one backtick longer than any backtick run in the content, so
content containing code fences stays valid. The Markdown export does not include
the wire event log; use JSON for that. An empty session says no messages were
stored. A session without a stored snapshot says so and does not reconstruct
one.

#### JSON (format version 1)

```jsonc
{
  "format": "nexus.session-export",
  "format_version": 1,
  "generated": 1793…, "generated_at": "2026-10-27T12:34:56+00:00",
  "scope": "tree", "root": "ses_…", "session_count": 2,
  "state": {"running": false, "running_sessions": [], "durable_partial": false,
            "note": "…"},                       // running/partial explanation
  "boundary": {"log_generation": "log_…", "event_id": 4200, "seq": 9000, "floor": 0,
               "history_truncated": false, "session_updated": 1793…,
               "counts": {"ses_…": {"messages": 4, "events": 88}}, "note": "…"},
  "sessions": [{
    "session": {"id": "ses_…", "project_id": "prj_…", "directory": "/abs/path",
                "title": "…", "model": null, "permission": "ask", "effort": null,
                "agent": "build", "parent_id": null, "parent_call_id": null,
                "launched_by": "user", "disabled_tools": [], "archived": false,
                "created": 1793…, "updated": 1793…},   // model/effort null = not chosen
    "tree": {"depth": 0, "in_export": true, "parent_in_export": false,
             "children": ["ses_…"], "children_in_export": ["ses_…"]},
    "snapshot": { …full stored session_snapshot, or null… },
    "snapshot_origin": "created", "snapshot_created": 1793…,
    "snapshot_notice": "Recorded when the session was created…",
    "state": {"running": false, "durable_partial": false},
    "counts": {"messages": 4, "events": 88},
    "messages": [{"id": "msg_…", "session_id": "ses_…", "seq": 1, "created": 1793…,
                  "role": "user", "parts": [{"type": "text", "text": "…",
                                             "origin": null, "raw": null}]}],
    "events": [{"id": 42, "seq": 42, "time": 1793…, "type": "text_delta",
                "session_id": "ses_…", "data": {…}}]
  }]
}
```

Records keep the API shapes: `messages` is the `/message` list (ids, per-session
`seq`, parts with `origin` and `raw`), and `events` is the stored log (durable
event row `id`, publishing daemon's `seq`, time, type, session id, data) in order
and retained at the boundary. `tree.children` lists every direct child;
`children_in_export` lists the ones included by the scope. `snapshot` is the full
stored snapshot or `null`.

The snapshot provenance is labelled, never inferred. `origin: "created"` was
recorded when the session was created: its frozen agent prompt is the starting
one, later explicit changes are recorded forward in the messages, and an agent
switch replaces the snapshot. `origin: "agent-switch"` is the configuration
rebuilt when the agent was switched, and its notice says it is not the session's
original historical prompt. `origin: "reconstructed"` carries the reconstruction
note. A session with no stored snapshot gets `snapshot: null` and a notice that
the original prompt is unknown and was not reconstructed for the export.

CLI:

```sh
nexus session export ID --format markdown --output transcript.md
nexus session export ID --format json --scope tree --output session.json
nexus session export ID --format markdown                 # stdout
nexus session export ID --format json --scope tree -o -  # stdout ('-' too)
```

Omitting `--output` (or `-`) writes to stdout. An existing file is refused
unless `--force` is passed. The CLI downloads through the API to a temporary
`.part` file in the destination directory and publishes it only after the whole
response succeeded (exclusive hard link, or atomic replacement with `--force`); a failed download removes the temporary file and leaves an
earlier destination untouched. See [daemon.md](daemon.md#session-export).

`GET /api/session/{id}/event` reads one session's stored log by durable row id
(`?after=<event id>`); global events are not part of a session's log.

`GET /api/session/{id}/export` adds `x-nexus-log-generation` next to the boundary
headers, and the JSON `boundary` carries `log_generation` beside `event_id` and
`seq` (see [Session export](#session-export)).

## Event stream

`GET /api/event` is `text/event-stream`. The first frame is `server.connected`;
it has no `id:` line:

```
event: server.connected
data: {"version": "0.10.0.dev0", "generation": "log_1a2b3c4d5e6f7a8b", "head": 4200,
       "head_cursor": "log_1a2b3c4d5e6f7a8b:4200", "resumed_from": null,
       "session": null, "floor": 0}
```

Every later frame is one durable log row (the one exception, `bus.overflow`, is
stream control and described below), and the SSE `id:` line is its cursor:

```
id: log_1a2b3c4d5e6f7a8b:4201
event: text_delta
data: {"id": 4201, "cursor": "log_1a2b3c4d5e6f7a8b:4201", "seq": 77, "time": 1793…,
       "type": "text_delta", "session_id": "ses_…", "data": {"type": "text_delta", "text": "Hel"}}
```

| Field | Meaning |
| --- | --- |
| `id` | The durable row id in this database's event log. Stable across daemon restarts. |
| `cursor` | `<log generation>:<event id>`. Ordering is ascending event id. |
| `generation` | Identifies this database's log. In `/api/info` as `log.generation`. A new or replaced database has a new generation; a copied database keeps the same one. |
| `seq` | The publishing daemon's own counter. It restarts at 1 on every daemon start. **Not** a cursor; after a replayed range it can jump backwards. |
| `head`, `head_cursor`, `floor` | The durable head and retention floor at the moment of the request (`/api/info` reports the same). |

`generation`, `head`, `head_cursor` and `floor` are stream/info metadata, not
fields of every event; each event carries `id`, `cursor`, `seq`, `time`, `type`,
`session_id` and `data`.

Events are committed before subscribers receive them; a storage failure raises
instead of publishing an event that could not be replayed. A session event whose
session row is already gone is not published (the deletion tombstone covers it).

### Resume, replay and cursors

Pass the cursor of the last event applied as `?cursor=<generation>:<id>`, or as
the `Last-Event-ID` header (a browser `EventSource` sends it automatically after
the `id:` lines above). If both are present the **query parameter wins**. The
stream resumes strictly after that id.

The cursor is validated before the stream opens:

| Situation | Status | `code` |
| --- | --- | --- |
| Malformed cursor text | 400 | `invalid_cursor` |
| Cursor from another log generation | 409 | `cursor_generation` |
| Cursor ahead of the log head | 409 | `cursor_ahead` |
| Cursor older than the retained floor | 410 | `history_unavailable` |
| `?session=` names an unknown session | 404 | — |

Those responses are ordinary JSON (not a stream) and carry an actionable resync:

```json
{"error": "…", "code": "cursor_generation", "resync": true,
 "generation": "log_…", "head": 4200, "head_cursor": "log_…:4200",
 "floor": 0, "floor_cursor": "log_…:0"}
```

Reload state from `GET /api/snapshot` (or `GET /api/project` and
`GET /api/session`), then resume from `head_cursor`; omitting the cursor means
"start live at the boundary". A cursor error is never silently turned into
"start now".

Retention removes only a contiguous prefix of the log and moves `floor` to the
last removed event id. A cursor **at** the floor can resume (the next event is
`floor + 1`); a cursor below it gets 410 `history_unavailable` with the current
head and floor. The durable head high-water mark survives even when every
retained row was removed, so ids are never reused. A session's messages,
snapshots and conversation stay independent of diagnostic event expiry.

The replay-to-live boundary is atomic. The server registers the live
subscription, reads the durable head as the same step, and then pages the stored
log up to that head (500 rows per page). Events published during catch-up are
already queued for the live phase; events at or below the head are skipped on the
live side. So a publication during replay is delivered exactly once, at the
handoff, in order. No `limit` is applied to catch-up: the whole history from the
cursor is streamed.

`bus.overflow` is stream control, not a log event: it has no `id:` line and is
not stored.

```
event: bus.overflow
data: {"type": "bus.overflow", "time": …, "session_id": null, "id": null,
       "data": {"dropped_after": 4210, "generation": "log_…", "head": 4300}}
```

A subscriber more than 10 000 events behind is cut off after this frame.
`dropped_after` is the last live event enqueued for that subscriber (not
necessarily the last one the client applied); reconnect from your own last
applied cursor, or resync.

### Filtering and gaps

With `?session=`, only events of that session, its descendants, and global events
(`session_id: null`) are sent. Ids are global, so a filtered stream naturally
skips unrelated numbers: **a gap alone is not evidence of loss**. Only
`bus.overflow`, a cursor/resync response, or a subscriber ending without either
means continuity was broken.

Membership is maintained from the log itself. A `session.created` replayed or
received during the stream adds a descendant whose `parent_id` is already in the
tree; a `session.deleted` tombstone removes that session and its cached
descendants. Filtering therefore never depends on a relationship that a deletion
removed. Deleting the selected session leaves the stream open for global events;
a client should reload that tree when it sees the tombstone.

Global events are stored with `session_id: null`: `project.created`,
`project.updated`, `project.deleted`, and the minimal `session.deleted`
tombstone (`{"id": "ses_…"}`) so a reconnect cannot miss them for lack of a
session row. Deleting a session still deletes its messages, events, snapshots and
observations; the tombstone contains only the id needed for synchronization.
Deleting a project publishes one tombstone per deleted session, then
`project.deleted`.

| Type | Data |
| --- | --- |
| `config.changed` | `{revision}`: global, stored; clients reload `/api/config` |
| `project.created` / `project.updated` | `Project`; global, stored |
| `project.deleted` | `{id}`; global, stored |
| `session.created` / `session.updated` | `Session`, stored in that session's log |
| `session.snapshot` | a snapshot summary, emitted once when an old session is lazily reconstructed |
| `session.deleted` | `{id}`: a global tombstone stored without the session's history |
| `session.archived` / `session.unarchived` | `{root, sessions: [ids], archived}`: one transition for a whole tree, stored in the root's event log |
| `session.status` | `{status: "busy" or "idle", turn_id?, origin?: "prompt" or "continuation", continuation?, reconciled?}` before and after each turn; a recovered turn's idle also carries `reason` and `turn_id` |
| `message.added` | `Message`, once it is saved |
| `permission.asked` | pending approval |
| `permission.replied` | `{id, session_id?, allow, outcome: "allowed" or "denied" or "cancelled", reason?}`; a cancellation has `allow: false` |
| `question.asked` | pending question request (`id`, `session_id`, `call_id`, `questions`, `created`) |
| `question.replied` | `{id, session_id, call_id, outcome: "answered" or "cancelled", answers, reason?}` |
| `turn.recovered` | a turn a daemon restart interrupted: its outcome, restored messages, tool classifications, cancelled waits and lost runtime state (see [Crash recovery](#crash-recovery)) |
| `retry_scheduled` / `retry_started` / `retry_exhausted` | one provider retry transition: `{attempt, next_attempt?, attempt_id, delay?, reason, status?, retry_after?, max_attempts}` |
| `log.retention` | a global retention pass: `{at, events: {removed, floor, head, protected, pinned}, turns, mutations, settings}` |
| `session.notified` | `{from, text}`: a `wait: false` child finished, and its answer goes to this (parent) session |
| `session.workspace_tools` | `{active, entry, tools}`: the on-demand workspace controls were activated |
| `session.message_tool` | `{active, tools}`: `message_session` was exposed by the session's first `session` call (once) |
| `session.directory_changed` | `{from, to, branch, repository, worktree, source, project_id, frozen_directory, instructions}` |
| `worktree.listed` | `{repository, main, session_directory, worktrees: [...]}` |
| `worktree.created` / `worktree.deleted` | `{path, branch, head, repository, main, session_directory}` |
| `session.error` | `{kind: "provider" or "auth" or "request" or "interrupted", message, status?, body?}` |
| `bus.overflow` | `{dropped_after, generation, head}`: stream control, never stored; resume from your cursor or resync |
| loop and stream events | unchanged from [stream-events.md](stream-events.md): `wire_request`, `wire_response`, `wire_event`, `text_delta`, `reasoning_delta`, `tool_call_started`, `tool_args_delta`, `part_done`, `usage`, `done`, `step_start`, `tool_start`, `tool_end`, `turn_end` |

`wire_request` carries the exact request body and headers, with credentials
redacted. `wire_event` carries every provider SSE event as received. Nothing the
model sees is hidden from clients.

## Crash recovery

If the daemon crashes or is restarted while turns are running, the next startup
reconciles them before it accepts a request ([daemon.md](daemon.md#crash-recovery)).
Clients do not call a recovery endpoint; they resync and see the outcome in the
durable log:

- `session.status` `{"status": "idle", "reason": "the daemon restarted while the turn was running", "turn_id": "trn_…"}`
  for each recovered session.
- Ordinary `message.added` events for a restored assistant message (a response
  that had completed), for the one tool message closing its calls, and for the
  new `[Nexus recovery: interrupted turn]` user notice. The notice is new
  context; earlier messages are unchanged.
- `question.replied` with `outcome: "cancelled"` and `permission.replied` with
  `outcome: "cancelled"` for every wait the restart lost. The old ids cannot
  resume the turn: a question reply gets 409, an approval 404.
- One `turn.recovered` per turn with `{turn_id, session_id, outcome,
  reason, legacy, origin, continuation, request, assistant_message, tool_message,
  tools, waits, losses, partial_output, notice}`. `outcome` is `interrupted`, or
  `completed` when the turn's `ended` record (or, for a turn left open by an older
  daemon, a durable `turn_end` event) proved the loop finished before the crash
  (then `notice` is null). `legacy` is true for a pre-execution-record session.
  `origin` is `prompt` or `continuation` when recorded (null for an older turn),
  and `continuation` is the predecessor link a continuation crashed with.
  `partial_output` is `{"diagnostic_only": true, "counts": …, "parts": …,
  "tool_calls": …}` for an interrupted stream, or null. `tools[].outcome`
  is `restored`, `not_run`, `approval_pending`,
  `approval_denied`, `approval_cancelled`, `question_answered`,
  `question_cancelled`, `unknown`,
  `unknown_code` or `legacy_unknown`; `losses` reports `{kernels, bash_jobs}`.
- One `turn.summary` per closed turn, after `turn.recovered`, equal to its
  `GET /api/session/{id}/turn` entry.

Partial provider output (deltas and completed parts of an interrupted stream)
stays in the event log but is **not** inserted into the conversation, because
an interrupted response cannot be replayed without pretending it completed.
A restored response comes from parts that were durable before the crash. A
completed tool result is preserved and inserted; a started call with no result
is labelled unknown and is never re-run. A session saved by an older version
with a trailing tool call and no execution records gets a legacy
`[Nexus recovery: legacy interrupted turn]` notice and unknown results; it is
never claimed to have not run. Recovery itself is repeatable: a crash
during recovery does not duplicate a result, notice, cancellation or
`turn.recovered` event.

## A turn, end to end

1. Subscribe: `GET /api/event?session=ses_…` (with `?cursor=` / `Last-Event-ID`
   when reconnecting) and wait for `server.connected`.
2. `POST /api/session/ses_…/prompt {"text": "…"}` → 202. To resume the stored
   context without a new user message, `POST …/continue` instead and follow the
   same events; a continuation has no user `message.added` before `step_start`.
3. Events: `session.status busy` → `message.added` (user) → `step_start` →
   `wire_request` → `wire_event`… with `text_delta`/`tool_call_started`… →
   `part_done`… → `usage` → `done` → `message.added` (assistant) → `tool_start`
   → (`permission.asked` → client replies) → `tool_end` → `message.added`
   (tool) → `step_start` … → `turn_end` → `turn.summary` → `session.status idle`.
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

Login over the API (sign-in is the local `nexus login`), files and diffs.

Retention, idempotent mutation keys, bounded provider retries, the consistent
snapshot and client reconnect/resync are implemented ([Mutations, retries,
continuation and reconnect](#mutations-retries-continuation-and-reconnect)).
Steer and queued prompts remain Step 12a; a prompt while busy is still 409.
Automatic continuation after a crash is still not performed: recovery closes the
turn and manual continuation is an explicit request. External Git effects are
not transactional; an ambiguous worktree operation must be inspected rather
than repeated.

Worktree listing, creation and deletion exist, but merge/integration and branch
cleanup are separate future operations. A session's directory can move anywhere
the user can reach: it is not confined to its project.

Kernel execution and metadata limits: [tools.md](tools.md#code). `code.call` and
`code.cell` are stored session events; `kernel.deleted` announces kernel removal.

Question requests are in memory like approvals, but their durable
`question.asked` records let startup recovery close a lost wait with an explicit
`question.replied`/`outcome: "cancelled"`. A turn does not survive a crash
itself; recovery marks it interrupted and never resumes it (Step 11). Their
schema, limits and cancellation behavior are in
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
from the current files and appends `[Nexus notice: agent changed]`. An already-running call keeps its configuration; explicit changes apply to
subsequent model/tool calls, including within the same turn.

`disabled_tools` accepts task tools, `session`, `message_session` and the three
workspace control names. New sessions offer `expose_workspace_tools`; activating it adds
`change_session_directory` and `worktree` to later request schemas and to the
model-visible tool result, never to the frozen prompt. Activation persists with
the snapshot, and a pending directory move is visible in `directory_change`.

A session created before snapshots existed returns `snapshot.origin:
"reconstructed"` after its first use and carries a visible snapshot note. It is
not presented as the exact historical prompt; the reconstruction is stored once
and stays stable. It also starts with `workspace: {entry: false, active: false}`;
activate it explicitly if needed.

### Project file browsing (initial read-only API)

`GET /api/project/{id}/worktrees` lists the worktrees of the project's own
checkout (`repository`, `main`, `worktrees[]`, `project_directory`) when there
is no session to ask. It is the same shape as the session endpoint and returns
400 outside Git.

`GET /api/project/{id}/files?path=&session=` lists one directory level under
the project directory or the named session's directory. Entries report name,
kind, byte size, modification time, Git status and ignored status. Symlinks are
listed as links and are not followed when opening files or directories. `GET /api/project/{id}/file?path=&session=` returns UTF-8 text,
a SHA-256 content revision and a MIME-based language hint. Reads reject binary
or non-UTF-8 data and files over 1 MiB. Paths are relative to the selected root;
the session must belong to the project. File edits and change notifications are not yet provided.

`GET /api/project/{id}/files/search?q=&limit=&directories=&session=` returns
`{"query", "paths"}`: visible file paths under the selected root (relative,
POSIX, with a trailing `/` for directories when `directories=1` or `true`) whose
path contains `q`, case-insensitively. `q` defaults to `""`, which lists the first
visible paths. Results rank basename prefix matches, then basename matches, then
path matches; shallower paths come first, and directories rank ahead of files of
equal rank. `limit` defaults to 30 and is capped at 100. The walk never opens
files, skips hidden entries and all symlinks, and stops after 50,000 entries.
Inside a Git work tree only paths Git would not ignore are listed, and the list is
cached for 3 seconds. Invalid input returns 400: `q` must be at most 256
characters, contain no NUL or backslash, and not start with `/` or contain a `..`
segment; `limit` must be a non-negative integer.

`GET /api/session/{id}/git` returns branch, ahead/behind counts and changed
paths. Each change has `staged` (index column set), `unstaged` (worktree
column set, also true for untracked files), the porcelain v1 `status` code
(`"MM"`, `"A "`, `"??"`, with X as the index and Y as the worktree state) and
tracked line counts. Clients treat `"??"` as untracked.
`GET /api/session/{id}/git/diff?path=` returns separate staged and unstaged
diffs for a relative path. Untracked file contents are not included in diffs.


### Image prompts

`POST /api/session/{id}/prompt` accepts `{text?: string, images?: [{name?: string, data_url: string}]}`.
At least text or one image is required. Up to eight PNG, JPEG, GIF or WebP
images are allowed, at most 8 MiB of decoded bytes combined. Data URLs must use
base64; remote URLs and SVG are rejected. Validation runs before creating the
turn. Images become ordered `image` parts alongside the user text, stored in
SQLite with their bytes for reload, continuation, export and provider replay.
They are not workspace files or ephemeral browser URLs.

On the first startup after the allow-by-default upgrade, previously stored
`ask` permissions are changed to `allow` for existing sessions and children.
This one-time migration increments their revisions; an explicit `ask` set after
the migration remains in effect across restarts.

## Configuration, provider login and context budget

See [Configuration](config.md) for the config revision/patch contract and the
provider login lifecycle. `POST /api/provider/{id}/login` returns 202 for a
device flow, or 200 for an accepted OpenCode key. `GET` reports progress;
`DELETE` signs out (204). Global `provider.login` events report progress.

`GET /api/session/{id}/context` retains the provider request fields and adds
`used`, `window` and `exact`. The newest request's measured input is used when
its model matches the current model; otherwise `used` is a character-based
estimate and `exact` is false. A missing catalog window is null. Measured usage
refers to that request, rather than tokenizing newly appended context. Clients
label estimates with `~`.

## Pending inputs: steer and queue

`POST /api/session/{id}/prompt` accepts `mode: "send" | "steer" | "queue"`.
The default `send` retains its busy-session 409 behavior. During a running turn,
`steer` and `queue` return an `input_id` and remain outside model context until
delivery. Steers are inserted before the next model request, after all results
from the current tool step. A steer arriving during a final answer causes
another model step. Queued inputs start subsequent turns in order.

`GET /api/session/{id}/input` returns `{inputs, held}`. `PATCH
/api/session/{id}/input/{iid}` edits `text` or promotes `mode` from queue to
steer; `DELETE` removes the input and returns its parts for draft restoration.
`POST .../input/{iid}/move` with `{before: iid}` reorders pending input.
`POST /api/session/{id}/input/next` releases a held queue and sends its next item.
Pending inputs and held sessions are included in `/api/snapshot`.

Interruption holds the queue. Restart preserves pending inputs, converts
undelivered steers into queued inputs, and holds them until explicit release.
Archive and deletion refuse pending inputs unless `drop_inputs: true` is given.
`input.added`, `input.updated`, `input.removed`, `input.delivered` and
`input.held` events update client activity. Delivery includes the stored
`message_id` and `turn_id`; the corresponding user message has `delivery:
"steer"` or `"queued"`. Pending items themselves are not timeline messages.
