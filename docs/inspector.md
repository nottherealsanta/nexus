# Session inspector

## Inside NSH and the TUI

**Open inspector** (palette or Settings → About), **Open session in inspector**,
and a tool's inspector action now open a native, read-only modal, not a browser.
It uses the clients' existing authenticated Core transport, including remote NSH
connections. Close it with Esc to return to the same session and draft.

The six views are **Timing**, **Conversation**, **Steps**, **Requests**,
**Raw** and **Questions**. Conversation contains canonical saved message JSON;
Steps contains durable turns, model requests, usage, HTTP/first-byte
measurements, attempts and tool results; Requests includes the snapshot, next
request and retained redacted wire requests/responses; Raw contains the session
record, event counts and retained event JSON; Questions lists pending and replied
`ask` requests with links to their turn and tool call. Kernels and shells
remain in their existing resources modal. The standalone browser inspector below
remains available, including its richer kernel detail views.

Client rules (both clients, same behavior):

- **Select a session** from `tree` (`session=<id>` on every request) without
  switching the chat session. Filters `q`, `kind`, `status`, `type` are sent to Core.
- **Page with cursors**, never offsets: pass `next_before` as `before` for the
  older page. Keep a stack of cursors to go newer. Records within a page are
  oldest first; pages go newest to oldest.
- **Render `summary`** as the row and show `detail` (exact JSON) on expand. If
  `detail_truncated`, offer "load full" which calls `/inspect/record`.
- **Refresh only on change.** While open, react to the existing session event stream, coalesce durable changes,
  call `/inspect/head`, and refetch only when `head` or `generation` differ from
  the last response. Coalesce bursts into one refetch, tag each request with a
  sequence number and ignore a response that is not the latest (navigation,
  view switch, filter change, close). Do not fetch or poll while the inspector
  is closed. A `generation` change or a `floor` above the held cursor means the
  history changed: drop held cursors and reload the newest page. Show
  live/paused/disconnected state and keep manual Refresh; an offline view
  must not look current.

State kept per (inspected session, view): filters, cursor stack, selected row id,
expanded row ids, loaded full records, scroll, zoom and pan. Selection is by row id, so
rows that arrive do not move it. Switching views or sessions and back restores it; Refresh
keeps it. Long lists draw at most 60 rows around the selection ("... n rows above/below").

Layout (identical in both clients): title and inspected session (`session → inspecting
Scout`), the six views, the live state line, the filters line and inputs, the tree selector,
the Timing ruler and zoom controls, then the body (cache panel with the session-lifetime and
displayed-page totals, coverage, per-model rows and "Writes unknown"; `omitted` and notes;
the rows) and the page controls. Live states: `Live` (connected, newest page), `Paused`
(user paused, or an older page is shown; "newer records available" when the head moved),
`Disconnected` (a request failed; data is labelled "not current"). Keys are the `inspector.*`
commands in `interface/manifest.toml` (bare keys, only while the inspector is open): `1`-`6`
views, `r` refresh, `w` pause/resume, `[`/`]` session, `/` search, `f` kind, `s` status,
`t` event types, `x` clear, `j`/`k` select, Enter expand, `e` full record, `n`/`p`
older/newer, `c` copy, `i` cache notes, `=`/`-` zoom, `h`/`l` pan, `z` fit. NSH also has
buttons for each. Rows and the Sessions tree (a wrapping one-line list in both) are clickable, and `j`/`k` keep the selected row in view in both. Known gap: the standalone browser inspector remains for kernels and jobs.

### Inspector API v2

`GET /api/session/{id}/inspect?view=&before=&limit=&session=&q=&kind=&status=&type=`

| Field | Meaning |
| --- | --- |
| `version` | `2` |
| `session_id` / `session` | the requested session / the one inspected (`session` param or the same) |
| `before`, `next_before`, `more`, `limit` | cursor paging; `next_before` is null on the last page |
| `head`, `session_head`, `log_head`, `generation`, `floor` | `head` = highest event id in the inspected session and its descendants, `session_head` = the session alone; `log_head`/`generation`/`floor` are the global log watermark |
| `filters` | the filters applied |
| `tree` | `[{id, title, parent, depth, status (running/idle/archived), agent, model}]`, root first, at most 200 |
| `cache` / `cache_range` | see "Cache and Gantt timing" |
| `records` | rows of Conversation/Steps/Requests/Raw/Questions: `{kind, id, summary, detail, detail_bytes, detail_truncated, ...}` plus `seq`, `role`, `type`, `turn_id`, `session_id`, `status`, `links` where they apply |
| `lanes` | Timing/Steps lanes (below) |
| `text` | the same page as one text blob (summaries + clipped details) for simple renderers |
| `notes`, `omitted` | human notes and machine-readable omissions `[{kind, count?, bytes?, reason}]`; kinds `tree`, `sessions`, `lanes`, `detail`, `section` |

Cursors are opaque base64url JSON bound to a view family and a stable
boundary: Conversation = message `seq`; Requests/Raw = event id; Timing/Steps =
`(turn started, turn id)` over the whole selected subtree (Steps: the selected
session); Questions = `(asked time, question id)`. Rows written after page 1
land newer than every boundary, so the union of older pages is exact and never
duplicates or skips. A cursor from another view family is 400.

Filters (applied in SQL): `q` is a substring match (Conversation: message parts;
Steps/Timing: turn id/error/data, tool name/arguments/output, request model;
Raw/Requests: event type and inline payload; Questions: asked/replied JSON).
`kind`: Timing/Steps lane kind `turn|model|tool|attempt`; Conversation role.
`status`: Timing/Steps turn status; Questions `pending|replied`. `type`: comma
list of event types for Raw (Requests intersects with `wire_request,wire_response`).

Example (abridged, `view=Steps&limit=1`):

```json
{"version": 2, "session_id": "ses_1", "session": "ses_1", "view": "Steps", "limit": 1,
 "before": null, "next_before": "WyJ0IiwxNzMxLjUsInR1cm5fOWYiXQ", "more": true, "generated": 1760000000.1,
 "head": 412, "session_head": 380, "log_head": 9120, "generation": "g_ab12", "floor": 0,
 "filters": {},
 "tree": [{"id": "ses_1", "title": "Review", "parent": null, "depth": 0, "status": "idle",
           "agent": "build", "model": "openai/gpt-5"}],
 "cache": {"scope": "session lifetime", "input": 12000, "cached": 9000, "uncached": 3000,
           "hit_percent": 75.0, "denominator": "input tokens reported by provider",
           "coverage": {"requests_with_usage": 11, "requests_total": 12, "complete": false},
           "models": {"openai/gpt-5": {"requests": 11, "input": 12000, "cached": 9000, "output": 800,
                      "reasoning": 100, "uncached": 3000, "hit_percent": 75.0}},
           "write": null, "write_known": false},
 "cache_range": {"scope": "displayed page", "input": 1000, "cached": 750, "uncached": 250,
                 "hit_percent": 75.0, "denominator": "input tokens reported by provider",
                 "coverage": {"requests_with_usage": 1, "requests_total": 1}, "models": {}},
 "omitted": [{"kind": "detail", "count": 1, "bytes": 31200, "reason": "detail text clipped ..."}],
 "records": [{"kind": "turn", "id": "turn_9f", "session_id": "ses_1", "turn_id": "turn_9f",
              "started": 1731.5, "status": "completed",
              "summary": "turn completed · 2 requests · 1 tools · 4200 ms",
              "detail": "{...first 8,000 bytes...", "detail_bytes": 39200, "detail_truncated": true}],
 "lanes": [{"id": "ses_1:tool:call_1", "kind": "tool", "label": "bash", "turn_id": "turn_9f",
            "start": 1731.6, "end": 1731.94, "running": false, "end_unknown": false, "status": "finished",
            "ref": {"kind": "tool", "id": "turn_9f:call_1"}, "summary": "tool bash finished 340 ms",
            "detail": "{...}", "detail_bytes": 410, "detail_truncated": false}],
 "notes": ["..."], "text": "..."}
```

Bounds: details are clipped at 8,000 bytes each and, because `text` repeats them,
at about 2 MB per response in total (`omitted` says how many details and bytes
were left out); at most 2,000 lanes (the oldest on the page are dropped and
counted); at most 200 tree entries and 200 sessions in a Timing subtree. The
page's children are fetched with one `in (...)` query per table, not one query
per row.

### Cache and Gantt timing

- `cache` is the **selected session's lifetime** ledger (`session_usage`), never a
  sum of visible rows, repeated cumulative reports or child sessions. Fields:
  `scope: "session lifetime"`, `input`, `cached`, `uncached = input - cached`,
  `hit_percent = cached / input`, `denominator: "input tokens reported by
  provider"`, `coverage {requests_with_usage, requests_total, complete}`, and
  `models` with the same numbers per model. `cache_range` is separately labelled
  (`scope: "displayed page"`) and sums only the requests on the returned
  Timing/Steps page (null for other views); each request counts once (its
  attempts are already summed). A selected call's own usage is in its lane detail.
  No provider reports means **unknown** (null), not zero.
- **What providers report.** The Responses and chat dialects report
  `cached_tokens` (cache read) inside input tokens. The Claude dialect reports
  `cache_read_input_tokens` and `cache_creation_input_tokens`, but Nexus folds
  both into `input_tokens` and stores only the read count as
  `cached_input_tokens`. **Cache writes are therefore unknown**
  (`write: null, write_known: false`); keeping them needs a versioned
  storage/codec change (tracked in PLAN.md 14.1). TTL/expiry and cost savings are
  never shown, and cache validity is never inferred from timestamps. These are
  observed token counts, not a promise about the next call.
- Timing is a Gantt chart on one absolute time axis, grouped by session,
  including descendants. Turns, model calls, tools and retry attempts have separate
  lanes with stable ids `session:kind:id`; `ref` names the record for
  `/inspect/record`. Model lanes carry `ttfb` (seconds from the wire request to
  its first event) only when it was recorded. Unfinished running operations
  extend to the snapshot time; historical missing ends say **end unknown**
  (`end_unknown`). Nested calls inside `code` have no independent timings.
- Both clients support zoom (1-64x), horizontal pan, fit, details, copying, refresh,
  and older/newer paging (a stack of cursors) with the keys above. NSH adds buttons
  and clickable rows; the TUI also scrolls with arrows/Page Up/Page Down.
- A page holds up to `limit` (default 50, max 200) messages, events, questions or
  turns; Timing's turns are counted across the whole subtree. Compaction and
  missing diagnostics are announced; canonical messages are not reconstructed
  from diagnostic partials. Pages are cursors, not a pinned snapshot.

Implementation: `nexus-core/nexus/inspector_data.py`,
`nexus-nsh/src/ui/inspector.tsx`, `nexus-tui/src/inspector.rs`.

## Standalone browser inspector

A web page served by the daemon that shows every session at four levels of
detail, from the conversation down to the raw wire traffic. Code:
`nexus-core/nexus/inspector/index.html` (one static file, no build step).

```sh
nexus sessions inspector             # opens the browser
nexus sessions inspector ses_…       # opens one session
nexus sessions inspector --no-open   # prints the URL
```

The URL is `http://127.0.0.1:4747/inspector#token=…&session=…&level=…`. The
token is in the fragment, which browsers never send to the server. The page
itself holds no data and loads without a token. Every API call it makes sends
the token as a bearer header. The page is served with a strict
content-security policy: no external scripts, styles or connections.

## Layout

- **Sidebar.** Projects with their path, and each project's sessions as a tree
  (who launched whom). Each row shows the title, agent, model and whether the
  session was launched by an agent. A green dot means busy; an `archived` pill
  marks an archived tree, separately from busy/idle. The sidebar always loads
  archived sessions (`?archived=include`), so their history stays reachable. It
  updates live from `/api/event`, including `session.archived` and
  `session.unarchived`.
- **Session header.** Every session field: id, agent, model and where it came
  from, effort, permission, archive state, directory, starting directory (the
  one frozen into the system prompt), workspace-control state (entry available,
  activation) and any pending directory change, who launched it (with a link to
  the parent and the launching tool call), children (links), and how many
  messages and events are stored. It also shows the snapshot origin and creation
  time, the agent's source path and fingerprint, and the `AGENTS.md` path and
  status.

## Levels (keys 1–4)

| Level | Shows | Source |
| --- | --- | --- |
| 1 Conversation | Saved messages: user and assistant text, reasoning summaries (with the size of the encrypted state), tool calls with each argument labelled, tool results with line and character counts. A `session` tool call links to the child it started. A Questions card shows each `ask` request (including nested in `code`), its options, answer or cancellation, and live pending status. Every part has its JSON, including the provider's raw wire item. A `[Nexus recovery: interrupted turn]` notice appears as an ordinary user message. | `/api/session/{id}/message`, `/api/session/{id}/question`, stored `question.asked` / `question.replied` |
| 2 Steps | One row per model call, grouped by turn: model, input item count, tools offered, HTTP status, time to first byte, total time, tokens in (cached) and out (reasoning), stop reason, and each tool run with its result and duration. Provider retry attempts appear in the turn's events (`retry_scheduled`/`retry_started`/`retry_exhausted`). | stored events |
| 3 Requests | A snapshot card (origin, agent source and fingerprint, instruction-file status, current instruction file after a move, workspace-control state, move count, child catalog), the next request (what the model would see now), then every sent request as labelled sections: redacted headers, parameters, instructions (including the `# Instructions file` section), each input item (preview, expandable to JSON), and tools (name, description, schema). Response headers too. | `/api/session/{id}/context`, the session's snapshot summary, stored `wire_request` / `wire_response` |
| 4 Raw | Every retained stored event in order, with the time offset, type and a clipped preview; click a row for its full JSON. You can filter by type or content, and token deltas and provider SSE events are hidden by default (one checkbox). Also the messages and session record as stored. When retention moved the floor, the view says earlier events expired and names the retained boundary; messages are not affected. When a diagnostic wire payload expired without moving the floor, the row says `payload expired`, its JSON carries the explicit expired marker, and a note explains that the event id/type remain while the body is not recoverable and `/api/snapshot` resyncs canonical state. | `/api/session/{id}/event` |

Clipping is always announced. Long tool output shows its first 60 lines, with
the rest one click away. Previews say how many characters were cut. The Raw
table shows 1500 rows at a time ("show more"). Up to 50 000 events are loaded
per session.

## Connection and resync

The page holds one fetch-based `/api/event` stream (not `EventSource`, so it can
read explicit continuity failures). It starts live at the server's head and
keeps each event's durable cursor. After a dropped connection it reconnects with
`?cursor=`, so the missed range is replayed from the stored log with stable
ordering. Events are deduplicated by their durable id; an event that arrives
while a session is loading is buffered until loading finishes. Loaded event rows
and messages are deduplicated independently by their own ids: the separate HTTP
reads can have different boundaries. Only stream delivery advances the cursor.

An explicit cursor failure (400/409/410: malformed, another log generation,
ahead of the log, or history below the floor) and `bus.overflow` trigger a full
resync instead of a silent fresh start: the sidebar, kernels, background jobs
and the open session are reloaded, and the stream restarts at the new head. The
sidebar line shows live / catching up / reconnecting / resyncing. If the open
session is deleted while the page is live, its view closes with an explicit
note. Reconnecting never resubmits a prompt or re-runs a turn. The browser uses
the same continuity rule as the Python client: cursor failures and overflow
require a state reload, not a guess; numeric gaps within a filtered stream are
normal.

## Limits

- Sessions from before the event log have messages only. Levels 2 and 3 show a
  note, and Conversation and Raw still work.
- Sessions from before stored snapshots show a `reconstructed` snapshot origin
  and the visible snapshot note in their instructions; the note says the prompt
  is not an exact historical record.
- Text is shown as plain text. Markdown is not rendered.
- Native inspector API v2 limits: 8,000 bytes per detail, about 2 MB per response,
  2,000 lanes, 200 tree entries, `limit` at most 200. Anything omitted appears in
  `omitted` and `notes`, and clipped details load in full from `/inspect/record`.
  Cache writes are unknown. `q` over Raw/Requests matches inline payloads only.
- Live updates re-render the open session when its events arrive. Events of a
  child show in the child's own page.
- The Questions card (Step 10) was added after the Playwright checks above and
  was script-syntax-checked, not re-run in a browser.- The workspace-control header fields, snapshot fields and live events were
  added later and script-syntax-checked (`node --check`), not re-run visually.
- Archive state is shown as a separate `archived` field and sidebar pill; it
  does not replace busy/idle. The archive view was script-syntax-checked, not
  re-run visually.
- Checked with Playwright (Chromium) in light and dark mode and at 390 px
  width. Not checked in Safari or Firefox.

## Code and kernels

The sidebar also lists live kernels. Opening one shows its creator, users,
cell count, assigned-variable previews and retained cells (last 100; omissions
announced). Conversation nests expandable `code.call` records beneath their
parent `code` call, including intermediate output the model did not request.
Code events also appear in Raw. The list updates after cells and deletions.

Verified with Playwright (Chromium, headless) against a scripted daemon: a
parallel `read` + `bash` cell with three nested calls (one caught error), a
finished background bash job with retained output, the kernel and job lists and
detail views, light and dark mode, and 390 px width. No console errors and no
document-level horizontal overflow. Screenshots:
`artifacts/inspector-kernels-light.png`,
`artifacts/inspector-kernels-dark.png`,
`artifacts/inspector-kernel-light.png`,
`artifacts/inspector-kernel-dark.png`,
`artifacts/inspector-kernels-narrow.png`,
`artifacts/inspector-kernel-narrow.png`,
`artifacts/inspector-job-light.png`,
`artifacts/inspector-job-dark.png` and `artifacts/inspector-job-narrow.png`.

The session header lists the next turn's `tools` and saved `disabled_tools`,
using “none” for an empty list.

## Workspace controls

The header and Requests card show the live directory, the starting directory
frozen into the system prompt, whether the on-demand entry point is available and
whether the two controls are active, and a pending API move. A move emits
`session.directory_changed` and the announcement appears as an ordinary user
message in Conversation; the destination `AGENTS.md` can be read there. Worktree
operations appear in the event log (`worktree.listed`, `worktree.created`,
`worktree.deleted`) with the repository, main checkout, paths and branches;
they do not change the session directory. After activation, Requests shows the
two new tool schemas while Instructions stays as it was frozen.

## Bash jobs

The sidebar lists background bash jobs, running and finished, with their status
and command. Opening one shows the command, directory, session, status, exit
code, duration, output counts and the retained output. The list refreshes on
`bash.started` and `bash.finished`. Jobs are retained until the job limit
evicts the oldest finished one, so a job's output stays inspectable after it
finishes; the durable event log still has the finish tail after eviction.

## Questions

The Questions card in Conversation lists each `ask` request from the stored
event log: every question text and option (with its label, id and preview), the
validated answer or the cancellation reason, and the asking tool call id (a
nested `code` question shows the synthetic nested id). Pending status comes from
`GET /api/session/{id}/question` and refreshes on `question.asked` and
`question.replied`. Questions asked by a child appear on the child's page, as
the inspector shows a session's own events. Schema: [tools.md](tools.md#ask).

Event storage (2026-11-04): Questions reads durable wait records, and Steps reads
paged `/turn` records (model, usage, attempts, tools, HTTP status and first-byte
time). Old rows without these measurements say unknown. Interrupted request
partials are labelled diagnostic-only and are not part of model context.
Streaming detail remains available in retained `wire_event` records; token and
argument deltas are live-only. Raw shows an explicit earlier-events-compacted
notice from cumulative session metadata. Compacted sessions retain Conversation
and state-based Steps/Questions; Requests/Raw only show retained diagnostics.
