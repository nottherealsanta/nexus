# Capability matrix (Step 12.1, item 1)

Walks every feature and flow in [INTERFACE_PLAN.md](../INTERFACE_PLAN.md) and
classifies it against what Core does today. Sources: `docs/api.md`,
`docs/stream-events.md`, `docs/daemon.md`, `docs/tools.md`, `docs/inspector.md`,
`docs/agents.md`, `docs/models.md`, checked against the route table and handlers in
`nexus-core/nexus/server.py` and `runtime.py` (the docs in the working tree were
being edited by Step 11 while this was written; routes were re-read in code).

## Classes

| Class | Meaning |
| --- | --- |
| **API ready** | Every datum and operation the feature needs exists in Core now. The client only renders, derives or combines. The endpoint or event is cited. Residual limits are in the Notes column. |
| **Core work** | A required piece is missing. The step that owns it is cited (`11a`, `12a`). `12a?` means no step lists it yet and it needs adding to the plan. The client must not invent state to cover it. |
| **Client only** | No Core involvement: view state, rendering, local input, per-client settings. |
| **Deferred** | Not in the first release, with the reason. |

Implementation status per client is in PLAN.md Step 12; this file stays the
classification of what Core can support. A feature that is mostly API ready but has one missing detail is classified by
what blocks the first-release flow. Enhancements that are not blocking stay
**API ready** with a `Gap:` note.

Conventions used below: `E:` names a stream or stored event, `GET /api/...` an
endpoint. "Stream" is `GET /api/event`; "snapshot" is `GET /api/snapshot`.
Durations come from event `time`; Core sends no duration fields.

## 1. Shell and navigation anchors

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| S1 | Left sidebar toggle (leading), breadcrumbs (centre), right sidebar toggle (trailing) | Client only | | Both toggles persist when panels close |
| S2 | Narrow layouts: sidebars as drawers | Client only | | Old TUI hid the left bar under 56 conversation columns |
| S3 | Sidebar tab and focus restoration | Client only | | |
| S4 | Keyboard-only navigation, visible focus, focus restore after drawers/dialogs | Client only | | Release-gate item |
| S5 | Follow-latest only at bottom, scroll anchoring, jump to latest | Client only | | Release-gate item |
| S6 | Shortcut precedence (Esc: popup, search, inner, overlay, sidebar, composer, interrupt) | Client only | 12.3 item 4 | Old `focus.rs::escape` is the model |
| S7 | Terminal restoration, bracketed paste, Unicode widths, ASCII fallback, NO_COLOR, reduced motion | Client only | 12.3 item 4 | TUI only |

## 2. Left sidebar

### Sessions tab

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| L1 | Scope toggle: current project / all projects | API ready | `GET /api/session` with or without `?project=`; `?archived=` | No filter returns every session of every project |
| L2 | Group by project when scope is all | API ready | `GET /api/project`, `Session.project_id` | |
| L3 | Group by worktree (main checkout first, then one group per worktree, branch and path) | API ready | `GET /api/session/{id}/worktrees` (`worktrees[]`: path, branch, main, dirty), `Session.directory` | Gap: the list is keyed by a session, so a project with no session has no worktree list. Group by matching `Session.directory` to a worktree path |
| L4 | Row: title, "Untitled" until title | API ready | `Session.title` (placeholder `New session`; filled from the first prompt, truncated) | Real titles are L21. Treat `New session` as untitled |
| L5 | Row: status idle / busy | API ready | `Session.status`; E: `session.status` | |
| L6 | Row: status waiting for input | API ready | snapshot `approvals`, `questions`; `GET /api/question`; E: `permission.asked`, `question.asked` and their `*.replied` | Derived by joining pending waits to sessions |
| L7 | Row: status error | API ready | E: `session.error` (`kind`, `message`, `status`, `body`) | Gap: not a Session field; derive from the stream, or from stored events within retention (default 14 days) |
| L8 | Row: relative time of last activity | API ready | `Session.updated` (bumped on every saved message) | |
| L9 | Row: number of child sessions; children not listed as rows | API ready | `Session.children`, `parent_id` (filter `parent_id == null` for rows) | |
| L10 | Handoff chain ("continued from") | Core work | 12a Compaction as handoff | No `continued_from` field exists |
| L11 | One-turn sessions hidden from the list | Core work | 12a One-turn sessions | No session kind exists |
| L12 | Archived filter ("Archived" view) | API ready | `GET /api/session?archived=only\|include` | |
| L13 | Search over titles and user messages (indexed) | Core work | 12a Archive, search (FTS5) | No search endpoint |
| L14 | Row action: open | Client only | | |
| L15 | Row action: rename | API ready | `PATCH /api/session/{id}` `{title, expected_revision?}` | |
| L16 | Row action: archive / unarchive | API ready | `POST …/archive`, `…/unarchive` | Root only (child gives 400); busy or waiting tree gives 409 |
| L17 | Row action: delete (confirm, names child count) | API ready | `DELETE /api/session/{id}`; child count from `GET …/tree` | Interrupts a running turn first. Emits `session.deleted` tombstones |
| L18 | Row action: copy id | Client only | | |
| L19 | Row action: open in inspector | API ready | `/inspector#token=…&session=…&level=…` ([inspector.md](../docs/inspector.md)) | Token travels in the fragment |
| L20 | Attention marks on rows and projects (waiting, unread finish, error) | Client only | | Derived from L6/L7 plus a local "seen" cursor |
| L21 | Auto title (cheap model, from first message) | Core work | 12a One-turn sessions | Until then the title is the truncated first prompt |

### Session actions (palette and row menu)

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| A1 | Rename | API ready | see L15 | Header click to rename is client only |
| A2 | Archive / unarchive | API ready | see L16 | |
| A3 | Delete | API ready | see L17 | |
| A4 | Export (Markdown transcript or JSON) | API ready | `GET /api/session/{id}/export?format=markdown\|json&scope=session\|tree` | Download via the API; headers carry boundary and filename. Markdown omits wire events; JSON includes them |
| A5 | Handoff / `/compact` | Core work | 12a Compaction as handoff | |
| A6 | Change session directory | API ready | `POST …/directory {directory}` (200 applied/unchanged, 202 pending while busy); E: `session.directory_changed` | A pending move shows in `Session.directory_change` |
| A7 | Worktrees: list, create, delete | API ready | `GET/POST …/worktrees {action,path?,branch?,base?}`; E: `worktree.listed/created/deleted` | Create does not move the session. An ambiguous crash outcome must be inspected, not repeated |
| A8 | Activate workspace controls (prerequisite for the model to use them) | API ready | `POST …/workspace-tools` | UI commands A6/A7 do not need activation |
| A9 | Integrate / merge a worktree | Deferred | INTERFACE_PLAN "TBD"; PLAN Known gaps | Core has no merge/integrate or branch cleanup |
| A10 | Interrupt (Esc) | API ready | `POST …/interrupt` | Cancels descendants too. A pending approval vanishes without `permission.replied` on a normal interrupt (Known gap); treat disappearance as cancelled |
| A11 | Continue | API ready | `POST …/continue` | See section 10 |
| A12 | No fork, no revert | Deferred | INTERFACE_PLAN | By design |

### Files tab

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| F1 | Project tree, lazy one level, gitignore aware | Core work | 12a Files and git | `api.md` "Not yet": files |
| F2 | Project/worktree selector for the tree | API ready | worktrees list (A7) | Data for choosing the root; the tree itself is F1 |
| F3 | Git status markers per file; ignored dimmed or hidden | Core work | 12a Files and git | |
| F4 | Open file in built-in editor | Core work | 12a Files and git (read) | |
| F5 | Open in `$EDITOR` / OS default | Client only | | Local clients only. Needs F1 for the path |
| F6 | Copy path | Client only | | |
| F7 | Insert `@path` into composer | Core work | 12a Files and git | Completion needs the F1 listing |

### Projects tab

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| P1 | List projects: name, path | API ready | `GET /api/project` | |
| P2 | Session count, last activity | API ready | derive from `GET /api/session`; `Project.updated` | No aggregate field |
| P3 | Select project (Sessions/Files follow) | Client only | | View state |
| P4 | Add project by path | API ready | `POST /api/project {path,name?}` | No directory browser endpoint; path entry only |
| P5 | Remove project (confirm; sessions go with it) | API ready | `DELETE /api/project/{id}` | Emits a tombstone per session then `project.deleted` |
| P6 | Rename project | API ready | `PATCH /api/project/{id}` | Not in INTERFACE_PLAN; free |

### Built-in editor

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| E1 | Open, edit, line numbers, find, syntax highlight, dirty marker | Client only | | TUI: ratatui-code-editor. NSH: editor element |
| E2 | Read and write files confined to the project | Core work | 12a Files and git; Enforced read-before-edit | `GET`/`PUT` file endpoints do not exist |
| E3 | Stale-save refusal with content revision; reload preserving unsaved text | Core work | 12a Enforced read-before-edit ("editor/API writes carry their own observed revision") | Revision is a content hash, not mtime. Unsaved-text recovery is client only |

## 3. Right sidebar

### Session info

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| R1 | Title, id (copyable), agent, model and source, effort and source, directory | API ready | `GET /api/session/{id}` (`model_source`, `effort_source`) | |
| R2 | Worktree branch for the directory | API ready | `GET …/worktrees` entry matching `session_directory`; E: `session.directory_changed.branch` | Non-git directory has none |
| R3 | Started at, last activity | API ready | `Session.created`, `Session.updated` | |
| R4 | Total duration of turns | API ready | E: `session.status` busy/idle pairs (`turn_id`) | Event retention (14 days default) can drop old pairs. Gap: no stored aggregate |
| R5 | Turns, model calls | API ready | E: `session.status`, `step_start`; or messages | Same retention caveat |
| R6 | Tool calls by tool (`bash 12 · edit 4`) | API ready | `GET …/message` tool_call parts | From messages, so no retention limit |
| R7 | Tokens: input, cached, output, reasoning | API ready | E: `usage` (per response) | Usage exists only in the event log. Gap: after retention, totals are incomplete. Core aggregate would fix it (12a?) |
| R8 | Context used vs window (bar) | API ready | last `usage.input_tokens` + `GET /api/model` (`context`) | Estimate. Handoff threshold needs 12a |
| R9 | Cost when price known | API ready | `usage` x `ModelInfo.input_cost/output_cost/cache_read_cost` (USD per million) | Client arithmetic; only connected providers' models listed |
| R10 | Child sessions: count, running, failed | API ready | `GET …/children`, `GET …/tree`, child `status` | "Failed" derives from child `session.error`/result |
| R11 | Enabled and disabled tools | API ready | `Session.tools`, `Session.disabled_tools` | |
| R12 | Kernels this tree uses | API ready | `GET /api/kernel` (creator, users) | Match by session/tree |
| R13 | btw list in session info | Core work | 12a One-turn sessions | |
| R14 | Changes, Jobs, Logs, cross-session Cost | Deferred | INTERFACE_PLAN "Not in the right sidebar for now" | Background bash jobs are available (`GET /api/bash`) when revisited |

### Git status

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| G1 | Branch, ahead/behind, changed files with `+N −M`, staged vs unstaged | Core work | 12a Files and git | `worktrees[].dirty/changes` is only a count |
| G2 | Click a file to open its diff | Core work | 12a Files and git (per-file diff) | |

## 4. New-session view

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| N1 | Show starting context before first message | Core work | 12a Session drafts | `POST /api/session` creates and freezes immediately |
| N2 | Agent selector | API ready | `GET /api/agent?project=` (name, description, source, path, model, effort, tools, steps) | Gap: `mode` (primary vs subagent) is not in the JSON; a wrong-mode choice returns 400 |
| N3 | Model and effort selectors | API ready | `GET /api/model` (`efforts`, `context`, costs), `GET /api/model/default` | |
| N4 | Edit agent/model/effort/tools before first message without side effects | Core work | 12a Session drafts | Today `PATCH` of agent or `disabled_tools` on an empty session appends a `[Nexus notice: …]` user message, polluting context |
| N5 | Directory or worktree choice | API ready | `POST /api/session {directory}` or `{project}`; worktrees A7 | |
| N6 | System prompt as the model will get it | Core work | 12a Session drafts | Available only after creation: `GET …/snapshot`, `GET …/context` |
| N7 | `AGENTS.md` with path and status | API ready | `Session.snapshot.instructions` (path, status, bytes, sha256); full text in `GET …/snapshot` | Only after creation (see N1) |
| N8 | Tools with guidance, schema and a switch each | Core work | 12a Session drafts | Per-session switches exist (`disabled_tools`; `GET …/snapshot.tool_definitions`), but not before creation |
| N9 | Skills and MCP servers with switches and status | Core work | 11a Skills, MCP | Nothing implemented |
| N10 | Token count of the whole starting context | Core work | 12a Session drafts | No token counter |
| N11 | Prompt override | Core work | 12a Session drafts | |
| N12 | After first send: frozen context as the first timeline item, collapsed | API ready | `GET …/snapshot`, `GET …/context` | Rendered from the snapshot; label as starting context |
| N13 | Empty project opens directly on this view | Client only | | |

## 5. Timeline

### Turns

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| T1 | Turn grouping and numbering `#1…`; "go to turn" | API ready | `GET …/message`; E: `session.status` (`turn_id`, `origin`) | Client numbers user messages. A continuation has no user message, so number by `turn_id` |
| T2 | Order: assistant text, reasoning, tool calls/results, child cards, notices | API ready | `GET …/message` parts in order; live: `text_delta`, `reasoning_delta`, `tool_call_started`, `tool_args_delta`, `part_done`, `message.added` | |
| T3 | Fold a turn to its header (number, first line, tools used, duration, status); fold all but last | Client only | | View state; folding never removes context |
| T4 | Turn footer: model, effort | API ready | E: `wire_request` body (`model`, reasoning effort) | Every model used in the turn comes from its requests |
| T5 | Turn footer: duration, model calls, tool calls | API ready | E: `step_start`, `tool_start`, `tool_end`, `turn_end`, event `time` | |
| T6 | Turn footer: tokens | API ready | E: `usage` | Retention caveat as R7 |
| T7 | Turn footer: cost | API ready | `usage` x `ModelInfo` costs | |
| T8 | Turn footer: how it ended (done, interrupted, error, step limit) | API ready | E: `turn_end.stop` (`end_turn`, `max_tokens`, `max_steps`, …), `session.error.kind`, `turn.recovered.outcome` | |
| T9 | Reasoning collapsed "Thought for N s", expandable | API ready | `Reasoning.summary`; duration from `reasoning_delta` event times | |
| T10 | Encrypted reasoning labelled | API ready | `Reasoning.raw` has encrypted content; export labels it the same way | |
| T11 | Notices and insertions (child finished, directory changed, clipping, steering, settings changes) | API ready | User messages starting `[Nexus notice:`, `[Nexus recovery:`, `[Notification:`; E: `session.notified` | Classify by prefix, as the export does. Steering notices wait on 12a |
| T12 | Errors: provider/auth/request/interrupted with status and body | API ready | E: `session.error` | |
| T13 | Retry rows with attempt number and wait | API ready | E: `retry_scheduled`, `retry_started`, `retry_exhausted` | Execution diagnostics, not context; label separately |
| T14 | Startup-recovery rows | API ready | E: `turn.recovered`, `[Nexus recovery: …]` notice message | |
| T15 | Handoff markers | Core work | 12a Compaction as handoff | |
| T16 | Directory-change rows (old/new directory, branch) | API ready | E: `session.directory_changed`; forward announcement is a message | |
| T17 | Model/agent switch row at the point it took effect | API ready | agent: `[Nexus notice: agent changed]` message; model: E: `session.updated` (diff of `model`/`effort`) | Gap: a model switch adds no model-visible message, so the row is an execution marker; label it as such |
| T18 | Hook rows, diagnostic rows | Deferred | INTERFACE_PLAN "when Core has hooks" | Core has no hooks |
| T19 | Session too large: load by turn newest first, older on scroll with a count | Core work | 12a? | `GET /api/snapshot?message_limit=` returns the newest N (`messages_more`, `oldest_seq`), but older history is only the whole `GET …/message` list, with no paging |

### User messages

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| U1 | Markdown as typed | Client only | | |
| U2 | Attachments as chips | Core work | 12a Attachments and images | Prompt body is `{text}` only |
| U3 | Large text collapsed with "N more lines", sent whole | Client only | | Composer paste collapse is also client |
| U4 | Images as thumbnails (NSH inline; TUI kitty/iTerm2/sixel, else chip) | Core work | 12a Attachments and images | Rendering fallback is client |
| U5 | Queued and steering messages marked | Core work | 12a Steer and queue | |

### Markdown and copy

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| M1 | CommonMark + GFM, fenced code with language label and highlighting | Client only | | |
| M2 | Streaming: unfinished fence or table stays plain until it closes | Client only | | |
| M3 | TUI tables fit width, OSC 8 links, headings by weight | Client only | | |
| M4 | `path:line` links open the editor at the line | Core work | 12a Files and git | Link detection is client; opening needs F4 |
| M5 | Copy: message, final answer, text block, code block, tool in/out, session id, path | Client only | | OSC 52 in the TUI, clipboard in NSH |
| M6 | Copy confirmation in the alert area | Client only | | |

## 6. Tool rendering

Common: collapsed line is status icon, tool name, key argument, summary, duration.
Arguments come from `tool_call.arguments` or `tool_start.args`; results from the
`tool` message or `tool_end.result`; duration from the `tool_start`/`tool_end`
event times. Status: running (`tool_start` without `tool_end`), done, error
(`result.is_error`), denied (`permission.replied.outcome: denied`), waiting
(`permission.asked`/`question.asked` pending).

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| X1 | Collapsed line updates live while arguments stream | API ready | E: `tool_call_started`, `tool_args_delta`, `tool_start`, `tool_end` | |
| X2 | `read`: path and range; lines read; expanded content with line numbers and clip notice | API ready | args `path`, `offset`, `limit`; result lines `{n}<TAB>{text}`, footer `[lines A–B of N]`, clip lines | Parse the result text; no structured result |
| X3 | `bash`: command first line; exit code, output lines; expanded: full command, directory, output box, exit, duration | API ready | args `command`; result starts `[exit code N]`; E: `tool_start/end` times; working dir from `Session.directory` | Output is clipped at 30,000 chars in the result. A background run returns `started background job job_…`; see X13 |
| X4 | `edit`: path or file count; `+N −M` | API ready | args `old_string`/`new_string`/`replace_all` or `patch` (apply_patch text); result `edited <path> (N replacement[s])` | Counts derive from the arguments, not the result |
| X5 | `edit` expanded: unified diff with line numbers, highlighting, per-file patch folds, failed edit shows error and attempted change | API ready | arguments + error result | Gap: results carry no line numbers or surrounding context, so diff line numbers are relative to the hunk unless Core adds them (12a Files and git diff) |
| X6 | `write`: path; `+N` new or `+N −M` overwrite | Core work | 12a Files and git (diff) or tool-result metadata | Result says `created`/`overwrote … (N lines, B bytes)`; the previous content is not stored, so `−M` and the overwrite diff are unavailable |
| X7 | `code`: first line of cell; nested calls, `Out` type, kernel id; expanded: source, stdout, `Out [n]`, nested lines | API ready | args `code`, `kernel_id`; E: `code.call` (nested, parent call id), `code.cell`; `GET /api/kernel/{id}` | Result labels kernel id, cell number, duration, call count |
| X8 | `session`: agent and title; child status | API ready | args `agent`, `prompt`, `wait`, `title`; child via `parent_call_id` (`GET …/children`) | See section 7 |
| X9 | `ask`: first question; answer once given; expanded questions, options, answers | API ready | args `questions`; E: `question.asked`, `question.replied`; `GET …/question` | Result is JSON keyed by question id |
| X10 | `expose_workspace_tools` | API ready | tool call/result; E: `session.workspace_tools` | |
| X11 | `change_session_directory` | API ready | tool call/result; E: `session.directory_changed` | |
| X12 | `worktree` | API ready | tool call/result; E: `worktree.listed/created/deleted` | |
| X13 | Background bash: job state and output | API ready | `GET /api/bash`, `GET /api/bash/{id}`, `DELETE /api/bash/{id}`; E: `bash.started`, `bash.finished` | Jobs live in memory; output lost on daemon restart (Known gap) |
| X14 | `web_search`, `web_fetch` | Core work | 11a Web tools | Not implemented |
| X15 | MCP tools (`server` + tool name) | Core work | 11a MCP | Not implemented |
| X16 | Skill loading | Core work | 11a Skills | Not implemented |
| X17 | Errors and denials: error colour; denial says who denied | API ready | `result.is_error` text; E: `permission.replied` (`outcome`, `reason`) | |
| X18 | Large output: capped UI box, first/last N lines, "N hidden · show all" | Client only | | Display cap only |
| X19 | "Show all" fetches the full result from Core | Core work | 12a Attachments and images (durable artifacts for full tool outputs) | Today Core keeps only the clipped model-visible text; only a background bash job keeps retained output (`GET /api/bash/{id}`) |
| X20 | Separate notice: UI hides vs model never saw | API ready | model-side clip is in the result text (`[… clipped N of TOTAL characters …]`, `[… line clipped at 2000 …]`) | UI preview cap is X18; keep the two labels distinct |
| X21 | Raw toggle: exact arguments JSON and result text as sent | API ready | `ToolCall.arguments` (exact text), `ToolResult.output`; `wire_request` for the full body | |
| X22 | "Open in inspector" from a tool line | API ready | `/inspector#…&session=…&level=` | Gap: no anchor for a single call; link opens the session at a level |

## 7. Child sessions

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| C1 | Card at the `session` call: agent, title, status, waits-or-not, elapsed | API ready | `Session` of the child (`parent_call_id`, `launched_by`, `status`); args `wait` | Statuses: running/done via `status` plus last result; failed and interrupted derive from events |
| C2 | Live activity: last tool line or streaming text, tool count, tokens | API ready | Stream `?session=<parent>` includes descendants | Aggregation client side; retention caveat for old children |
| C3 | Done: first lines of answer, expandable to whole answer | API ready | tool result of the `session` call | |
| C4 | Non-waiting child's later "finished" notice linked to the card | API ready | E: `session.notified {from, text}`; notice message | |
| C5 | Waiting-for-input bubbles to the parent's waiting area | API ready | `GET /api/question`, snapshot `approvals`/`questions` (all sessions); descendant stream | |
| C6 | Open child: same view, breadcrumbs, back, Esc/Backspace | API ready | `GET …/tree`, child messages and stream | Back-to-card scroll position is client state |
| C7 | Prompt the child directly | API ready | `POST /api/session/{child}/prompt` | A child is a normal session |
| C8 | Create a child as a user | API ready | `POST /api/session {parent, agent?}` | Not in INTERFACE_PLAN; exists |
| C9 | Steer a child from the parent's card | Deferred | INTERFACE_PLAN open question | |

## 8. Waiting for user input

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| W1 | One card above the composer; marker on the tool line; sidebar "waiting" | API ready | E: `permission.asked`, `question.asked`; snapshot | Client unifies the two |
| W2 | Count and cycle across sessions and children | API ready | `GET /api/question`, snapshot `approvals`, `questions` | |
| W3 | Approval: full parameters, bash command and directory | API ready | `permission.asked.args`; `Session.directory` | In practice only `bash` asks. `edit`, `write`, `read` and workspace controls are approval-free by fixed policy |
| W4 | Approval: diff preview for edit/patch/write | Deferred | tools.md "Approval" | Those tools never ask, so there is nothing to preview. Revisit if policy changes |
| W5 | Choice: allow once / deny | API ready | `POST …/permission/{pid} {reply: "allow"\|"deny"}` | |
| W6 | Choice: allow for this session | Core work | 12a? | Only `allow`/`deny`. `PATCH permission: "allow"` turns off every ask for the session, which is broader than "this call" |
| W7 | Choice: deny with a note to the agent | Core work | 12a? | Reply has no `reason` field |
| W8 | Disabled choice with reason | Client only | | Depends on W6/W7 capability data; Core would need to report which choices exist |
| W9 | Question: options (single/multi), free text, optional preview, answer all then submit | API ready | `question.asked.questions[]` (`options[].preview`, `multiple`, `allow_free_text`); `POST …/question/{qid} {answers}` | 400 leaves it pending; 409 once resolved |
| W10 | No timeout | API ready | tools.md ask | |
| W11 | Cancelled: card closes, timeline keeps it as "cancelled: interrupted" | API ready | E: `question.replied` (`outcome: cancelled`, `reason`), `permission.replied`; tool result | Gap: a normal-interrupt approval cancel has no event (Known gap) |
| W12 | Draft answers kept for this view | Client only | | |
| W13 | Attention from elsewhere: marks, banner "go to", drawer, OS notification | Client only | | Data from snapshot and stream |

## 9. Composer

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| K1 | Multi-line Markdown input, Enter sends, Shift+Enter newline | Client only | | |
| K2 | Send a prompt | API ready | `POST …/prompt {text}` with `Idempotency-Key` | 202 `{turn_id, origin}` |
| K3 | Steer while busy | Core work | 12a Steer and queue | A busy prompt is 409 |
| K4 | Queue for next turn; list, edit, remove queued | Core work | 12a Steer and queue | |
| K5 | Agent selector, mid-turn | API ready | `POST …/agent {agent}`; E: `session.updated`; notice message | Applies to the next model call. Rebuilds the snapshot; confirm first (cache) |
| K6 | Model selector, mid-turn | API ready | `POST …/model {model}` | Confirm first (cache). Applies to subsequent calls |
| K7 | Effort selector, mid-turn | API ready | `PATCH /api/session/{id} {effort}` | |
| K8 | Show pending change and when it takes effect | Client only | | Derive from busy state and `session.updated` |
| K9 | Tools/MCP changes mid-turn | API ready | `PATCH {disabled_tools}`; notice message | MCP part is 11a |
| K10 | Attachments: durable storage, size limits, chips, drag-drop, paste image | Core work | 12a Attachments and images | |
| K11 | `@path` mentions with project-file completion | Core work | 12a Files and git | |
| K12 | Large paste becomes a collapsed chip | Client only | | Old `editor.rs` has this |
| K13 | Voice dictation into the composer | Client only | 12a "Transcription (maybe)" | Platform recognizer needs no Core. A provider endpoint would need Core |
| K14 | Interrupt (Esc / stop button) | API ready | `POST …/interrupt` | |
| K15 | Drafts per session | Client only | | |
| K16 | `/` command completion | Client only | 12.1 item 3 manifest | |
| K17 | Retried sends do not duplicate turns | API ready | `Idempotency-Key` on prompt/continue (replays original `turn_id`) | One key per user action |
| K18 | Competing clients: revision/conflict | API ready | `expected_revision`/`If-Match` on `PATCH`, `POST …/model`, `…/agent`; 409 `revision_conflict` | |

## 10. Continue, one-turn sessions, handoff, directory

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| O1 | `/continue` after interrupted or failed turn | API ready | `POST …/continue`; E: `session.status` (`origin: continuation`, `continuation.predecessor`, `reconciled`) | Client enables it when idle, messages exist, last turn interrupted/failed. Errors: 409 busy/archived/unreconciled, 400 empty |
| O2 | Link continuation to the interrupted attempt; label unknown outcomes | API ready | `continuation.predecessor`, `reconciled`; `[Nexus recovery: …]` notice | |
| O3 | Do not repeat an ambiguous POST | API ready | api.md | Use an `Idempotency-Key`; inspect before retrying without one |
| O4 | One-turn: title | Core work | 12a One-turn sessions | |
| O5 | `/btw` overlay and list | Core work | 12a One-turn sessions | |
| O6 | Handoff at threshold (default 80%) or on request | Core work | 12a Compaction as handoff | No context limit exists (Known gaps) |
| O7 | New root session starts from the summary; "continued from" item; chain in sidebar | Core work | 12a Compaction as handoff | |
| O8 | Directory change: old/new paths and branch, `AGENTS.md` loaded as forward context, Files/git/header update, group move | API ready | `POST …/directory`; E: `session.directory_changed` (`from`, `to`, `branch`, `instructions`); `Session.directory` | Files/git refresh depends on F1/G1 |
| O9 | Running children keep their paths | API ready | daemon.md | |
| O10 | Worktree delete refuses dirty/in-use | API ready | `POST …/worktrees {action: delete}` returns 400 with the reason | |
| O11 | Leaving a worktree means changing directory first | API ready | O8 | |
| O12 | Edits in the session directory need no approval; refusal explains the re-read | API ready | tool error text | |

## 11. Commands and palette

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| Q1 | Palette (Cmd/Ctrl+P), commands with keybinding, availability | Client only | 12.1 item 3 | One shared manifest. Availability rules read Core state |
| Q2 | Slash commands from the same list, with completion | Client only | 12.1 item 3 | |
| Q3 | New session, switch session/project | API ready | `POST /api/session`, `GET /api/session`, `GET /api/project` | |
| Q4 | Search sessions | Core work | 12a Archive, search | See L13 |
| Q5 | Archive, delete | API ready | L16, L17 | |
| Q5b | Fold all, go to turn, toggle sidebars | Client only | T3, T1, S1 | |
| Q6 | Change model / effort / agent / tools | API ready | K5-K9 | |
| Q7 | Open settings, theme, notifications, copy actions | Client only | | |
| Q8 | Sign in | Core work | 12a Settings over the API | Sign-in is `nexus login` locally |
| Q9 | Open inspector | API ready | L19 | |
| Q10 | `btw`, compact/handoff | Core work | 12a | A5, O5 |
| Q11 | Interrupt | API ready | K14 | |
| Q12 | Rebind keys | Client only | | Settings Keybindings |

## 12. Alerts and notifications

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| Z1 | Toasts (copied, saved, title set) | Client only | | |
| Z2 | Banners: daemon disconnected/reconnecting | Client only | | Driven by stream state |
| Z3 | Banner: provider needs sign-in | API ready | `GET /api/provider` (`connected`); E: `session.error` `kind: auth` | |
| Z4 | Banner: version warning | Core work | 12a Version handshake | |
| Z5 | Confirm dialogs: delete, model/agent switch (cache), remove project | Client only | | |
| Z6 | Blocking alert: major version mismatch | Core work | 12a Version handshake | `/api/info.version` is the Core version string, not an API major.minor |
| Z7 | Notifications drawer with unread count and session links | Client only | | History is view state; old TUI keeps 50 |
| Z8 | Attention marks (row, project, window title) | Client only | | See L20 |
| Z9 | Completion sound; OS/terminal notification (bell, OSC 9/777) | Client only | | Per-kind switches live in Settings Notifications |

## 13. Connection to Core

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| H1 | Read `GET /api/info` on connect | API ready | `{name, version, pid, home, url, log, retention, retry, idempotency}` | |
| H2 | API version compare (major halt, minor warn) | Core work | 12a Version handshake | Not in `/api/info` |
| H3 | Find or start the daemon | API ready | `~/.nexus10/daemon.json` (`url`, `pid`, `token`, `started`) trusted after `GET /api/info`; `nexus daemon start`; log `~/.nexus10/logs/daemon.log` | Local bootstrap only. A browser cannot read `daemon.json` or spawn (12.5 item 5) |
| H4 | Authenticated SSE | API ready | `GET /api/event` with `Authorization: Bearer`; `?token=` for `EventSource` | |
| H5 | Reconnect with cursor, atomic replay-to-live, dedupe by `id` | API ready | `?cursor=<generation>:<id>` or `Last-Event-ID`; first frame `server.connected` | |
| H6 | Resync on `history_unavailable`, `cursor_generation`, `cursor_ahead`, `bus.overflow` | API ready | `GET /api/snapshot` then resubscribe from `watermark.head_cursor` | Filtered-stream gaps are normal |
| H7 | Restore pending waits after reconnect | API ready | snapshot `approvals`, `questions` | In-memory waits; a restart cancels them with events |
| H8 | Restore queued messages | Core work | 12a Steer and queue | |
| H9 | Daemon restart detection: new pid/token | API ready | `/api/info.pid`; `server.connected.generation`; re-read `daemon.json` | |
| H10 | Interrupted turns show partial state and "interrupted by daemon restart" | API ready | E: `turn.recovered`, `session.status` idle with `reason`; recovery notice | |
| H11 | Keyed mutations, revision conflicts | API ready | `Idempotency-Key`; `expected_revision` | See api.md route matrix |
| H12 | Distinguish starting / connecting / live / reconnecting / offline / auth failure / incompatible | Client only | 12.3 item 2 | 401 means a stale token; incompatible needs H2 |
| H13 | No device registration | Deferred | INTERFACE_PLAN | By design |

## 14. Loading and empty states

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| Y1 | "Starting Nexus Core" screen with log tail | Client only | | Reads `daemon.log` locally. A browser client cannot; show instructions (12.5 item 5) |
| Y2 | Connection lost: banner, read-only last view, then resync | Client only | | |
| Y3 | Wrong protocol version: blocking alert | Core work | 12a Version handshake | |
| Y4 | First run: welcome, sign in, choose project | Core work | 12a Settings over the API (sign-in) | `GET /api/provider` detects "none connected"; signing in needs the API |
| Y5 | Empty project shows new-session view | Client only | | |
| Y6 | Too-large session loads by turn | Core work | 12a? | See T19 |
| Y7 | Distinguish loading, empty, filtered-empty, failed | Client only | 12.4 item 1 | |

## 15. Settings

Order: Appearance, Providers, Models, Agents, Tools, Skills, MCP, Context,
Dictation, Keybindings, Notifications, About.

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| ST1 | Appearance: theme (light/dark/system/named), density, font and size (NSH) | Client only | | Per-client; shared token file |
| ST2 | Providers: list with connected state | API ready | `GET /api/provider` (`id`, `label`, `dialect`, `connected`, `verified`, `default_model`) | |
| ST3 | Providers: account, expiry | Core work | 12a Settings over the API | Not returned |
| ST4 | Providers: sign in (browser/device), sign out | Core work | 12a Settings over the API | Only local `nexus login` |
| ST5 | Providers: default model per provider | Core work | 12a Settings over the API | `default_model` is read-only from the provider table |
| ST6 | Providers: add OpenAI-compatible endpoint | Deferred | INTERFACE_PLAN "when Core supports custom providers" | Only four subscription providers now |
| ST7 | Models: catalog list | API ready | `GET /api/model[?provider=]` (`source`, models with `efforts`, `context`, costs) | Lists only connected providers |
| ST8 | Models: catalog refresh | Core work | 12a? | `catalog.load(refresh=True)` is not reachable over HTTP |
| ST9 | Models: default model and effort (read, write) | Core work | 12a Settings over the API | `GET /api/model/default` reads only |
| ST10 | Agents: loaded agents, source, path, problems | API ready | `GET /api/agent[?project=]` (`agents`, `problems`) | Gap: `mode` missing |
| ST11 | Agents: open agent file in editor | Core work | 12a Files and git | Local clients could open the path externally |
| ST12 | Agents: default root agent (write) | Core work | 12a Settings over the API | |
| ST13 | Tools: defaults for new sessions | Core work | 12a Settings over the API | Per-session switches exist; there is no global tool list or defaults |
| ST14 | Skills: discovered, sources, diagnostics, defaults | Core work | 11a Skills | |
| ST15 | MCP: servers, enabled defaults, status, tools, connect/disconnect/retry; browser-use default | Core work | 11a MCP, `nexus-browser/` | |
| ST16 | Context: handoff threshold percentage | Core work | 12a Compaction as handoff | |
| ST17 | Dictation: engine, language, push-to-talk key | Client only | | |
| ST18 | Keybindings: command list with editable keys | Client only | | |
| ST19 | Notifications: when, sound, OS | Client only | | |
| ST20 | About: client version, Core version, daemon pid, home | API ready | `GET /api/info` (`version`, `pid`, `home`) | |
| ST21 | About: API version | Core work | 12a Version handshake | |
| ST22 | About: link to inspector | API ready | `/inspector` | |
| ST23 | Unsupported capabilities show their reason | Client only | | Applies to every Core-work row above until it lands |

## 16. Inspector and wire view

| ID | Feature | Class | Source / step | Notes |
| --- | --- | --- | --- | --- |
| I1 | Link to the inspector from a session | API ready | `/inspector#token=…&session=…&level=1..4`; `nexus sessions inspector` | Static page; token in the fragment |
| I2 | Link from a tool line | API ready | X22 | Session-level link |
| I3 | Wire view toggle: request bodies and raw events inline | API ready | E: `wire_request` (body, redacted headers), `wire_response`, `wire_event`; `GET …/context`; `GET …/event?type=` | Stored events obey retention; `history_unavailable` is honest |
| I4 | Starting context shows what the model sees next | API ready | `GET …/context`, `GET …/snapshot` | |
| I5 | Stored event paging | API ready | `GET …/event?after=&limit=&type=` (≤ 5000) | |

## 17. Required flows blocked by Core

First-release TUI journeys (12.4) that cannot be completed without a Core
increment. The client must show the reason and must not fake the state.

| Flow | Blocked by | Step |
| --- | --- | --- |
| Start a session after reviewing and editing its starting context (N1, N4, N6, N8, N10) | No draft; editing an empty session pollutes context | 12a Session drafts |
| Send while the agent works (K3, K4, U5, H8) | Busy prompt is 409 | 12a Steer and queue |
| Browse files, mention `@path`, open an editor, save (F1-F4, F7, E2, E3, M4) | No file endpoints | 12a Files and git; Enforced read-before-edit |
| Git status and diffs (G1, G2, F3) | No git endpoints | 12a Files and git |
| Sign in on first run, manage providers, set defaults (Y4, ST3-ST5, ST9, ST12, ST13, Q8) | Sign-in only local; defaults read-only | 12a Settings over the API |
| Version-safe connection (H2, Z4, Z6, Y3, ST21) | `/api/info` has no API version | 12a Version handshake |
| Context window management (A5, O6, O7, L10, T15, ST16) | No handoff, no context limit | 12a Compaction as handoff |
| Meaningful titles, `/btw` (L21, O4, O5, R13, L11) | No one-turn sessions | 12a One-turn sessions |
| Search (L13, Q4) | No index | 12a Archive, search |
| Attach files and images (K10, U2, U4) | No attachments | 12a Attachments and images |
| "Show all" for clipped tool output (X19) | Full output not retained | 12a artifacts |
| Skills, MCP, web tools (N9, X14-X16, ST14, ST15) | Not implemented | 11a |
| Old-session metrics beyond event retention (R4-R7, T6) | Usage lives only in expiring events | 12a? aggregate |

## 18. Gaps found that no step lists

Candidates to add to Step 12a (all small):

1. **Approval replies**: "allow for this session" scope and a deny note (`W6`, `W7`). Also expose which choices exist per request so the card can show disabled choices (W8).
2. **Message paging**: `GET …/message?before_seq=&limit=` (T19, Y6). Needed for "go to turn" and search targets as well.
3. **Usage aggregates**: persist token counts per request/turn so session info and turn footers survive event retention (R4-R7, T6).
4. **Agent `mode`** in `GET /api/agent` (N2, ST10).
5. **Model catalog refresh** over HTTP (ST8).
6. **Project-level worktree list** that does not need a session id (L3).
7. **Cancel event for a normal interrupt of a pending approval** (A10, W11).
8. **Per-call anchors for the inspector** (X22), low priority.
9. **Diff context** in `edit`/`write` results or via the file/git endpoints (X5, X6).

## 19. Counts

Counts are by feature ID (one row each) in the tables above, regenerated with:

```sh
awk -F'|' '/^\| [A-Z]+[0-9]+b? \|/ {gsub(/^ +| +$/,"",$4); n[$4]++} END {for (k in n) print n[k], k}' interface/capability-matrix.md
```

Totals at the time of writing (rows with an ID): API ready 133, Client only 54, Core work 64, Deferred 8; 259 in all.
