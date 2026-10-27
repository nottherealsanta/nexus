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
| GET | `/api/info` | | `{name, version, pid, home, url, log: {generation, head, head_cursor, floor}}` |
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
| GET | `/api/session` | `?project=`, `?directory=` or `?parent=`, plus `?archived=exclude\|only\|include` | `[Session]` |
| POST | `/api/session` | `{project` or `directory` or `parent, agent?, title?, model?, permission?, effort?, disabled_tools?}` | `Session`; with `parent`, a child in the parent's directory (`launched_by: "user"`) |
| GET | `/api/session/{id}` | | `Session` |
| PATCH | `/api/session/{id}` | `{agent?, title?, model?, permission?, effort?, disabled_tools?}` (`""` clears model or effort) | `Session` |
| DELETE | `/api/session/{id}` | | 204; interrupts a running turn first |
| POST | `/api/session/{id}/archive` | | `Session` for the whole root tree; idempotent |
| POST | `/api/session/{id}/unarchive` | | `Session` for the whole root tree; idempotent |
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
| GET | `/api/session/{id}/export` | `?format=markdown\|json&scope=session\|tree` | a read-only Markdown transcript or JSON document; see [Session export](#session-export) |
| GET | `/api/session/{id}/permission` | | pending approvals |
| POST | `/api/session/{id}/permission/{pid}` | `{reply: "allow" or "deny"}` | 204 |
| GET | `/api/question` | | every pending `ask` question request |
| GET | `/api/session/{id}/question` | | pending questions for that session (empty list when none) |
| POST | `/api/session/{id}/question/{qid}` | `{answers: [{question_id, selected?, text?}, …]}` | 204; 400 invalid answer, 404 unknown question or session, 409 already resolved |
| GET | `/api/event` | `?session=` (that session and its descendants), `?cursor=<generation>:<id>` (or `Last-Event-ID`) | server-sent events (below) |
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
or `allow` (nothing asks). Session-directory edits are approval-free by fixed
policy; see [tools.md](tools.md#read-observations-and-freshness). `status` is
`busy` while a turn runs.

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
`event:<event id>;seq:<bus seq>` for the read boundary and
`x-nexus-log-generation` names the log those ids belong to. A missing or unknown
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
parameter. Both formats stream. A large log is complete, and the export does not
silently omit records.

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
text exactly). A user message that starts with `[Nexus notice:` or
`[Notification:` is labelled a context notice. Child conversations stay in
their own session sections, never inside the parent's messages.

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
  "boundary": {"log_generation": "log_…", "event_id": 4200, "seq": 9000, "session_updated": 1793…,
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
and complete for the boundary. `tree.children` lists every direct child;
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

Reload state from `GET /api/project` and `GET /api/session` (and the session's
events if needed), then resume from `head_cursor`; omitting the cursor means
"start live at the boundary". A cursor error is never silently turned into
"start now". This increment does not expire history, so `floor` is 0 and
`history_unavailable` is already defined for retention that is not implemented
yet.

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
| `project.created` / `project.updated` | `Project`; global, stored |
| `project.deleted` | `{id}`; global, stored |
| `session.created` / `session.updated` | `Session`, stored in that session's log |
| `session.snapshot` | a snapshot summary, emitted once when an old session is lazily reconstructed |
| `session.deleted` | `{id}`: a global tombstone stored without the session's history |
| `session.archived` / `session.unarchived` | `{root, sessions: [ids], archived}`: one transition for a whole tree, stored in the root's event log |
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
| `bus.overflow` | `{dropped_after, generation, head}`: stream control, never stored; resume from your cursor or resync |
| loop and stream events | unchanged from [stream-events.md](stream-events.md): `wire_request`, `wire_response`, `wire_event`, `text_delta`, `reasoning_delta`, `tool_call_started`, `tool_args_delta`, `part_done`, `usage`, `done`, `step_start`, `tool_start`, `tool_end`, `turn_end` |

`wire_request` carries the exact request body and headers, with credentials
redacted. `wire_event` carries every provider SSE event as received. Nothing the
model sees is hidden from clients.

## A turn, end to end

1. Subscribe: `GET /api/event?session=ses_…` (with `?cursor=` / `Last-Event-ID`
   when reconnecting) and wait for `server.connected`.
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

Login over the API (sign-in is the local `nexus login`), files and diffs.

Event retention and log compaction: the `event` table still grows without
limit and the log floor stays 0. The cursor protocol already reports
`history_unavailable` with the current head and floor, so retention can arrive
without a client change. Turn recovery after a crash, automatic retries,
idempotent mutations and `/continue` are also still open (Step 11); a reconnect
resumes the **stream**, not an interrupted turn or in-memory approvals and
questions.

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
from the current files and appends `[Nexus notice: agent changed]`. An already-running call keeps its configuration; explicit changes apply to
subsequent model/tool calls, including within the same turn.

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
