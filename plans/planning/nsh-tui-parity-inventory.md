# Native Ratatui TUI feature inventory (parity target for the NSH client)

Status: read-only inventory, 2026-10-26. Sources: `docs/surfaces.md`, `docs/ratatui-parity.md`,
`docs/cli.md`, `docs/events-and-view.md`, `nexus/ui/cli/commands.py`, `nexus/host/protocol.py`,
`nexus/host/facade.py` (`subscribe_web`, `web_snapshot`), `nexus/host_support/browser_view.py`,
`nexus/host/web.py`, `nexus/ui/ratatui/`, `nexus/ui_support/`, `rust/tui/src/`.
Nothing here was run; claims are from reading code and docs.

## 0. Orientation (read first)

- **The Rust TUI does not read the view.** `rust/tui` only draws private JSONL snapshots (schema 3)
  that the Python bridge (`nexus/ui/ratatui/prototype.py::project`) builds from the reduced
  `ConversationView`. All presentation logic (grouping, row text, folds, gaps, prompts, queue rows, turn
  status, sidebars, settings pages) therefore lives in **Python** (`nexus/ui/ratatui/*.py`,
  `nexus/ui_support/*.py`) and must be ported to Rust for the NSH app, not just the `ui_support` helpers.
  Section 3 lists both.
- The NSH app today (`rust/app/src/{main,lib,model}.rs`, ~700 lines) polls, shows sessions + a plain
  timeline + send. No SSE, approvals, settings, attachments.
- Everything the TUI does goes through host commands. `POST /v1/web/command` accepts every command
  except `Shutdown` and `WebLaunch` (403). Command JSON is `{"type":"<ClassName>", ...fields}`; defaults
  apply to omitted fields. Result JSON has the same `type` tag; errors are `{"type":"ErrorResult","kind","message"}`.
  `PROTOCOL_VERSION = 7` (`Health` returns it; TUI refuses a mismatch).
- Things the TUI does **client-side / locally** (no host call): `~/.config/nexus/tui.json` preferences
  (theme, sidebar open/tab/width, details tab/width, favorites, recents, dismissed notices,
  context_preview, centered_layout, tabs_vertical), prompt history file, unsent drafts per session,
  disclosure/fold state, clipboard, git branch via `.git` read (web client must use `GitStatus.branch` or
  the `Doctor` report `git`), audio capture/playback, completion sound, OSC52 copy.
- **Not reachable from the web API**: raw event stream (so `turn.*` `slot_wait` data, `manager.show`
  events, `session.linked` are not visible; `SessionActivity` gives `queued`, `running_agents`,
  `wait_seconds`); daemon lifecycle (`/restart`, `/reload`, `Shutdown`); `SessionSubscribe`
  (use SSE). `/v1/web/ticket/redeem`, `/bootstrap`, `/logout` are auth plumbing.

## 1. View / patch contract

### 1.1 Endpoints
| Route | Notes |
| --- | --- |
| `GET /v1/web/session-view?session=ID` | `{"schema_version":1,"session":ID,"seq":N,"view":{...}}`. 400 invalid id (`[A-Za-z0-9][A-Za-z0-9_-]{0,79}`), 404 `session unavailable`, 413 `view too large` (32 MiB). |
| `GET /v1/web/session-events?session=ID&from_seq=N` | SSE. First bytes `retry: 1000\n\n`. Comment `: keepalive` periodically. `from_seq` must be >= 0 (400 otherwise). |
| `GET /v1/web/workspace-events` | SSE, `event: workspace`, `id: <revision>`, data `{"schema_version":1,"revision":R,"sessions":[SessionSummary...],"archived_count":N}`. Sent on connect and then only when the list or archive count changed (poll 0.5 s). Frames > 512 KiB end the stream. `SessionSummary` fields: `id,title,name,state(idle|running|awaiting_input|awaiting_permission),last_activity,last_seq,completion_seq,viewers,message_count,created_at,parent_id,fork_seq,last_user_at,namespace,kind,root_id,child_count,running_children,handoff_count`. This is the current workspace only; other projects need `ProjectSessionsList`. |
| `POST /v1/web/command` | 1 MiB cap request; response cap 32 MiB (413). 400 malformed, 403 `Shutdown`/`WebLaunch`, 500 generic. `ProjectSessionOpen` is routed through the daemon's project switch hook. |
| `POST /v1/web/attachment` | JSON `AttachmentPrepare` only (`data` bytes are base64 in JSON), 12 MiB cap. |
| `POST /v1/web/voice?request_id=ID[&partial=1]` | body raw mono 16 kHz PCM16 **WAV** (`audio/wav`), 8 MiB; returns `VoiceTranscribeResult` JSON (`request_id,text,duration_s,elapsed_s,language`). |
| `GET /v1/web/bootstrap` | `{schema_version,csrf,workspace}`. |
| Auth | ticket -> cookie (12 h) + CSRF; every non-GET needs exact `Origin` and `X-CSRF-Token`; the voice/command/attachment POSTs and the GET routes need the cookie. `POST /v1/web/logout`. |

### 1.2 Snapshot `view` (from `web_view(ConversationView)`, `browser_view.py`)
Every dataclass field is emitted (optional values are `null`, not omitted; only `PermissionView.targets`
is dropped when null). Strings are clipped to 8192 chars + `…` (message/thinking block text is not
clipped at 8192; it uses the 8 MiB message bound). Dict-typed fields stay JSON objects keyed by name/id.

Top-level `view` keys:
`session_id, session_name, last_seq, code_store, phase (idle|running|awaiting_input|awaiting_permission|closed), opened, root, worktree, closed, turns[], permissions[], presence, input_queue[], agent_order[], extensions{}, mcp{}, context{}|null, model{}|null, registry|null, errors[], hooks[], diagnostics[], external|null, live_calls[], jobs[]`
plus computed on the wire: `messages[]` (flattened copy of every turn's messages; redundant, ignore),
`usage` (sum of turn usage), `pending_permissions[]` (**list of permission ids**, not objects),
and `agents[]` (**array in `agent_order`**, not a dict; each element is an `AgentView` whose `body`
is a full nested `ConversationView` with its own `turns/agents/...`).

Sub-shapes (all from `nexus/view/model.py`):
- `TurnView`: `id,index,phase(active|completed|failed|cancelled),stop_reason,error,iteration,limits,messages[],tools[],commit_shas[],permission_ids[],context[](ContextView{kind,iteration,data}),skills[],hooks[],retries[](RetryView),usage(UsageTotals{input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens}),started_ts,updated_ts,completed_ts,agent,reasoning_effort,user_ts,assistant_ts,elapsed_ms,kind(agent|shell)`.
- `MessageView`: `id,event_seq,role(user|assistant),blocks[],iteration,provider,model,attempt,stop_reason,done,ts,sent_ts,origin{kind:live|manager|subagent_report|root_followup,...}`. `BlockView`: `kind(text|thinking|image|...),text,image_url,signature,streamed,finalized,started_ts,elapsed_ms`.
- `ToolCallView`: `call_id,event_seq,iteration,name,target,status(requested|running|completed|failed),bundle,is_error,executed,code,error,duration_ms,nested_calls[],code_mode,progress[],requested_ts,started_ts,finished_ts,input{},result[],display,context_note,metrics,diff,child_agent_ids[],provenance`.
- `PermissionView`: `id,call_id,tool,key,bundle,preview,suggestions[],default_rule,persistence_available,status(pending|resolved|cancelled),decision,scope,grant,ts,targets[{role,path,reason}]`.
- `QueuedInputView`: `queued_id,mode(queue|steer|interrupt),content[],depth,origin,sent_ts`. `PresenceView{viewers,attended}`.
- `RootView{kind(main|worktree|external),path_display,branch,base_ref,base_sha,worktree_id,reason}`; `WorktreeView{enabled,base_ref,base_sha,branch,state,history[],moves[],identity_fallback,provenance_clipped,setup_trusted,ahead,behind,uncommitted,dirty_main,commits[],last_skip,promotion_error}`.
- `AgentView`: `id,parent,type,task,session,depth,tier,requested_tier,index,description,model,resolved_model,selected_model,route,tools,dropped_tools,clamped,status(spawned|completed|clamped|interrupted),ok,is_error,error,iterations,stop_reason,usage,max_tier,diagnostics,body,spawned_ts,completed_ts,parent_call_id,runs,continuable,stopped,stopped_by,stopped_reason,last_run_ts,background,usage_unknown`.
- `JobView`, `LiveCallView` (+`LiveTurnView`, `LiveDelegationView`, `LiveAppendView`), `ExtensionView`, `McpView`, `RegistryView`, `ErrorView`, `DiagnosticView{type,seq,ts,data,event_id}`. `context` carries the measured prompt/window/pricing (`context_measure`), `model` the selected model.
- `context.notice` diagnostics (`data.message`, `data.kind`, `data.model_visible`) drive notice rows.

Clients must ignore unknown keys and tolerate unknown block/tool kinds (events-and-view.md: "UIs must tolerate unknown types").

### 1.3 `session-events` frames
Transport frame (exact, from `web.py::_stream`):
```
retry: 1000

id: <seq>
event: view
data: {"schema_version":1,"session":"<id>","seq":<seq>,"ops":[ ... ]}

```
Other frames: `data: {"schema_version":1,"session":"<id>","seq":<seq>,"resync":true}` (same `event: view`,
sent in place of any frame whose JSON exceeds 256 KiB); `event: error` with `data: {"resync":true}` when the
server-side iterator throws; `: keepalive` comment lines. `seq` is the session `event.seq`, monotonic but
not necessarily contiguous (message/snapshot records share the counter). One frame per event; the
server drops events with `seq <= from_seq`, so a client must also drop `frame.seq <= its cursor`.

`ops` are produced by `json_patch(before_wire, after_wire)` and are applied **in order** to the previous
wire view (the same JSON as the snapshot `view`). RFC 6902 subset plus `append`:
| op | shape | semantics |
| --- | --- | --- |
| `add` | `{op,path,value}` | object: set key (key absent before). Array: path ends with `-` -> push value. |
| `replace` | `{op,path,value}` | set value at path; `path == ""` replaces the whole view (emitted when the root type changes). Also emitted for any scalar/type change. |
| `remove` | `{op,path}` | object key delete, or array index delete. |
| `append` | `{op,path,value:string}` | string concat onto the existing string at `path` (streaming text/thinking/progress). |
Pointers use RFC 6901 escaping (`~0` for `~`, `~1` for `/`); array elements are addressed by numeric index
(`/turns/3/messages/0/blocks/0/text`); agents by index in the `agents` array. Diff order per container:
object -> removes (sorted), adds (sorted), then recursive ops for common keys (sorted); array -> recursive
ops on common indices, then removes from the last index downwards, then `add .../-` for new tail items.
Applier: parse pointer, walk, mutate in place; unknown op, missing path or type mismatch = resync.

### 1.4 Connect / resync rules (implement exactly)
1. `GET session-view` -> store `view`, `cursor = seq`. (TUI bootstrap calls `Health` then `SessionOpen(create=true,recover=true)` first; do the same before the first snapshot of a session the client created or may not have open.)
2. Open `session-events?from_seq=cursor`. For each `view` frame: if `resync` -> go to 1; if `seq <= cursor` skip; apply `ops`; `cursor = seq`. Frame dispatch must be ordered (never apply out of order or drop).
3. Any stream end, HTTP error, `event: error`, ops application failure, or tab/app background -> re-snapshot (step 1) then reconnect. Use `Last-Event-ID`-equivalent = `cursor` via `from_seq`; browser `EventSource` auto-retries every 1000 ms but cannot send `from_seq`, so reconnect manually.
4. Guard stale async: tag every async result with `(session id, generation)` and drop if the active session changed (surfaces.md principle 6).
5. Session switch = new snapshot + new stream; keep per-session drafts and disclosure state locally.
6. The server stream needs the session to exist (`_session(create=False)`); a missing session ends the stream -> `SessionOpen` then retry.
7. Rendering cadence: coalesce ops (TUI coalesces 16 ms; flush immediately on user messages, permission/question tool requests, disconnects).

Data not carried by the view and fetched by commands is listed per feature below.

## 2. Feature areas
Format: behavior; host commands / view fields (class names, key fields); TUI source
(`P:` `nexus/ui/ratatui/` or `nexus/ui_support/`, `R:` `rust/tui/src/`).

### 2.1 Top bar
- Row 1 session tabs: `▌` left-sidebar toggle, one tab per visible session (status glyph, title first 24-36 chars, `×`), `+` new, then right buttons: notification counter `◔ N`, Manager `▣/■`, Sessions `▤`, details toggle `▥`. Tab set = current + opened in this window + sessions that are working / need input / finished unseen; `×` only hides; closing current opens neighbour; closing last tab = `/new`. Empty never-used tabs auto-close on switch. Overflow shows a window around the active tab; vertical-tab rail alternative (`/tabs vertical|top`, `tabs_vertical` pref) with wheel scroll. Glyphs: braille spinner working, `◆` needs input, blue `●` finished unseen, `◇` idle/archived. Source: `P:prototype.py::_tab_rows`, `actions.py::close_tab/refresh_session_tabs`, `R:render/chrome.rs`.
  - Data: `ProjectSessionsList(limit=1000)` polled every ~3 s (1 s when logs/jobs open); `SessionSummary.state/running_children/completion_seq` + local `seen` map -> `session_status` (`working|input|done|idle`); unread dot when `completion_seq > seen[id]` and id != current; completion cue played once on that transition (not for initial discovery).
- Row 2 breadcrumb: `~/path:branch` + worktree label (`⎇ <name>` bold purple for worktree root; `main checkout` muted for main in a Git repo) + status words (`Idle/Working/Needs input`). Branch: TUI reads `.git`; web must use `GitStatus(session).branch` / `Doctor` `report.git`. Worktree label from `view.root` (`kind,branch,worktree_id`) with no host call. `P:worktree_bar.py` (`session_worktree_label/kind/apply_bar/root_move_text`), `prototype.py::refresh_branch/_display_breadcrumb`; `R:render/chrome.rs` (also a project strip row under the composer).
- Subagent pages replace tabs with `←` Back + child title/model; Manager view shows `◆ Manager · model · Live: phase`.
- Update notice: `UpdateStatus(announce=true)` every ~30 s -> one info toast; `DatabaseNotice(claim=true)` one-time toast.
- Notification counter + drawer: see 2.11.

### 2.2 Left sidebar (docked/overlay; `Ctrl+B`, `/sessions`, top-bar `▤`)
Tabs Projects / Sessions / Files / Git (`ui_support/sidebar.py`, `P:sidebar.py::SidebarController`, `R:sidebar.rs`, `render/sessions.rs`). Width pref 24..60 (default 34), drag divider, hides when conversation < 56 cols. Focus by click; keys: Tab/Shift+Tab tabs, arrows/PgUp/PgDn/Home/End, Enter/→ open, ←/Backspace up, `r` refresh.
- **Projects**: registered/current/recent projects (temp checkouts filtered), `Add project` -> folder picker modal (editable absolute path, `~/` ok, parent/child navigation, files inert, "Open this folder · new session"; Enter/click on a directory opens it as a project). Commands: `ProjectSessionsList(limit,projects=limit)` -> `ProjectEntry{workspace,project_id,repo,worktree,updated,exists}`; `ProjectDirectoryList(path,limit=500)` -> `DirectoryListResult{path,entries,truncated}`; `ProjectSessionOpen(workspace,session,create=true)` -> `ProjectSessionOpenResult{project,session,url}`; project switch re-bootstraps the client against that workspace (every later command carries `project` id field where required).
- **Sessions** (polled 2 s while open): filter box (matches title/summary/project/branch case-insensitively; server search via `ProjectSessionsList.query`, up to 256 chars, 1000 results; empty restores recent 20), groups **Needs you / Working / Idle / Archived** collapsible (optional `g` group by project, heading `project › worktree · day`), row = glyph + title + age; selected row full-width accent band; only root sessions (no manager, no subagent children). Needs-you rows show the pending question + `waiting mm:ss` + keys `Enter open  a allow once  d deny` (`PermissionResolve` on a row's `pending.request_id` without opening). Row keys: Enter open, `n` new, `r` rename (`SessionRename(session_id,title)`, <=title limit), `d`/Delete archive with confirmation (`SessionArchive`; refused locally while working), `g` group, `t` ownership tree (`SessionTree`, `SessionInspect(namespace)`), `m` Manager, `Ctrl+W` worktrees, `Ctrl+K`/`/` search. Archived: `SessionListArchived(query,limit=200,cursor)` -> `ArchivedSummary`, `SessionPreview(session,max_chars)`, `SessionUnarchive` (Resume); archive count label `Archived · N+`. Fork: `SessionFork(session,at_seq)` (Ctrl+F, `/fork`). Handoff: `SessionHandoff(session,request_id)` / `SessionHandoffRead` (rows show `handoff_count`; children counts `child_count/running_children`). No per-row delete in the native UI (`SessionDelete/SessionRestore` exist but are unused there). Each row: `ProjectSession{workspace,project_id,session:SessionSummary,repo,worktree,last_user,last_agent,archived,pending{request_id,kind,summary,ts}}`. Group/age helpers: `ui_support/session_status.py`, `session_groups.py`, `P:workflows.py::session_rows/_time_bucket`. Open of a session in another project: `ProjectSessionOpen(workspace, session)` then reconnect there. Untitled shows `New Session`.
- **Files**: `DirectoryList(path,limit=200,session)` (folders first, `↑ ..`, links never followed); Enter/double-click (500 ms) file -> `FilePreview(path,max_bytes=65536,session)` modal `File · path` (`status text|binary...`, `size,shown,truncated,message`).
- **Git**: `GitStatus(session)` -> `branch,upstream,ahead,behind,rows[],counts,truncated,available,message`; sections Conflicts / Staged / Changes / Untracked (`ui_support/sidebar.py::git_payload`, GIT_GROUPS).
- Pinned **Manager** entry block above roots (see 2.17).

### 2.3 Context header
Opens every conversation (pref `context_preview`, toggle in Settings -> Layout). Blocks in order: System prompt, Environment, AGENTS.md, MEMORY.md, Skills, Tools, MCP; each a click target with title, counts (`Tools [13]`, `Skills [proj glob]`), token estimate (`~4 chars/token`), one-line preview for System prompt/AGENTS.md; empty blocks greyed; footer `total N tok · X% of the window`. Click -> section inspector modal (`context_show`); System prompt dialog = literal prompt without AGENTS.md; AGENTS.md rendered as Markdown. Data: `ContextInspect(session)` -> `ContextInspectResult` (`system_files, system_text, skills_index, mcp_index, mcp_servers, tools, tools_supported, included_parts, messages, request_context, params, budget, omitted, agent, context_locked, redacted_for_display, model, provider`). Refresh on cursor change while idle (host refuses while the session is active -> keep last preview, "as of" time). Child agent pages use the child's recorded context from `AgentTranscript(session,agent_id)` -> `context`. Helpers: `ui_support/context_header.py::header_blocks`, `context.py::header_prompt_sections/header_system_prompt/context_groups`, `P:prototype.py::_context_blocks/_compact_header`, `context_sections.py`; `R:render/context.rs`, `transcript/header.rs`.
- Context dialogs (<= 88 cols): **Tools** (families, MCP; tool page with description, flattened parameter table, schema; ON/OFF switch, `LOCKED` after first turn/read-only child pages), **Skills** (list + `SkillInspect(session,name,max_body_bytes)` Markdown page), **MCP** (`Open MCP settings…`, `↻ Refresh all` = `ExtensionsReload` then `McpServerRestart` for each enabled disconnected server; per-server toggle/`[Restart]`; server page Server/Indexed/Full groups via `McpServerShow(session,name)`; search/all loading `ContextMcpLoadingSelect(session,server,mode)`); toggles = `ContextExtensionSelect(session,category,name,enabled)`; Ctrl+R on a row restarts. `/tools`, `/skills`, `/mcp` open these.
- Context meter (composer bottom row): `context_measure(view)` -> `(used, window, measured)`; price-tier thresholds `price_tier_thresholds(view)` (`view.context.pricing`); `Thinking · heading` via `thinking_status`; click or `Ctrl+I`/`Ctrl+X C` opens context usage modal (usage by turn table + estimated next request, `context_turn_usage`, `context_detail_usage`, `context_pricing_note`).

### 2.4 Timeline (conversation)
Built by `P:prototype.py::_project_turn` + `verb_groups.py` + `transcript_data.py` + `ui_support/timeline.py`/`tool_signal.py`/`tool_details.py`; drawn by `R:transcript.rs`, `transcript/{groups,tools,thought,signal,fold,route,notices,header,look}.rs`, `markdown.rs`, `disclosure.rs`, `session.rs`, `sticky.rs`, `jump.rs`, `copy_button.rs`.
| Feature | Behavior | Data |
| --- | --- | --- |
| User card | Full-width band, left rail in agent color, `#N` (counts user-initiated turns only: not shell turns, steers, report notifications) + send time (`sent_at` = `sent_ts`, first card falls back to `user_ts`), fold chevron in margin, attachment chips (`▣ Image 1`, document chips with name/size; click opens image preview / message page), `steer` tag for 2nd+ user message of a turn, `via voice` for `origin.kind=live`, `starting…` for the idle-send head input before consume; hover `Copy`. Only newest two turns open; older turns fold showing first prompt line + `N tools · tokens · model`. Preview limit 16,000 chars/200 lines then `... (N characters total)`. Sticky pinned prompt when its card scrolls off (clip 3 rows, `↑ N lines of this turn above`). | `TurnView.messages[role=user]`, `sent_ts`, `origin`, `input_queue`; `ui_support/timeline.submitted_attachment_summary` |
| Assistant markdown | CommonMark: ATX/setext headings colored by level, bold/em/strike, inline code (warning fg), fenced/indented code with language label + Copy button, nested lists, task markers, quotes `▌`, rules, links (target visible), boxed wrapping tables, images as `[image: alt (target)]`, HTML literal, control chars shown `\u{..}`. Streaming write-head glow on last 12 graphemes while message not `done` and turn not terminal; incomplete fences render as code. Mermaid: Unicode renderer for flow/sequence/state/class/ER, text for pie/gantt/timeline, raster half-blocks (Ondine worker) else literal source. | `MessageView.blocks[kind=text]`; `markdown.rs`, `mermaid*.rs`, `graphics.rs` |
| Thinking | One headline row `Thought for 3.2s ▸` (first sentence + line count; amber, faint rule); `Thinking…` while live; expand for full text (dimmed, `**heading**` bold); provider that hides reasoning: `◇ Thought · not shared by the provider`. | `BlockView{kind=thinking,elapsed_ms,started_ts}`, `ui_support/timeline.thought_title` |
| Tool rows | Muted one-line summary (sentence-case verb + target, e.g. `Read path (lines 1-120)`, `Bash <cmd clipped 40 cells>`), duration; running = braille spinner `⠋⠙⠸⢰⣠⣄⡆⠇` ~8 Hz with live-counting clock; `N lines ▸` only when output > 8 lines; double-click (300 ms)/Enter expands in place; `i` opens full details modal; running shell shows latest 4 output lines under `⎿` with earlier count. | `ToolCallView.*`, `timeline.tool_heading/tool_summary/tool_row_text/running_output_tail`, `tool_signal.closed_summary/shell_preview/strip_job_header/clip_cells` |
| Tool groups | Run of tools+thoughts = one group; change tools (`edit,multiedit,write,apply_patch,patch,notebook_edit`) group only with each other, lone change keeps its own row; label `Read 3 files, Searched 1 pattern` (nouns dropped above 5: `Read 4, Searched 2, Ran 1`; code kernel nouns `Started 1 kernel/Ran 2 cells/...`; external: `Claude Code ran N tools`); finished groups collapsed, duration kept, failed member ends ` · failed`; running group header `Working` shows newest 5 members as a tree `├─ ╰─` with `… N earlier`; fold-on-complete animation; group id = `turn.id:activity:first member`; question/standalone tools never grouped. Expanded member = `┌ head 0.8s` + `│` rail + relevant rows (`tool_signal`) + `╰`. | `verb_groups.py::split_runs/build_run/activity_entries`, `R:transcript/groups.rs`, `fold.rs` |
| Tool detail fields (inspector / verbose) | Sections: Overview, Parameters (flattened `a.b[0]` rows), Code, Progress, Summary, Result blocks, Error, Context, Metrics, Diff; provenance row instead of Executed for external; every cut announced; labelled tones for diff +/-/@@. Limits: 20,000 chars/value, 400 rows/section. | `ui_support/tool_details.py::tool_detail_sections/sections_to_text/styled_lines/flatten` |
| Change blocks (Edit/Write/Patch) | Header `action path +added −removed` (`N files` for multi-file), starts open with unified diff (line numbers, green/red tint, first 12 rows until Enter, max 400 rows, remainder announced; Write without diff shows content as added); failure error in red; Edit/Patch inline diff from durable `ToolCallView.diff` only (never read files); split before/after layout also exists (`diff_split_rows`, hunks sep `⋯`). | `timeline.split_diff_files/diff_sections/diff_unified_rows/diff_split_rows/structured_diff`, `P:prototype.py::_change_block` |
| Question tool card | Prompt + one row per option; `●` recorded answer (`metrics.answer`), `○` others, `✎ answer` for free text, `… awaiting answer` if open; raw details behind `i`/verbose. | `ToolCallView{name=question,input.question,options,metrics}` |
| Code mode rows | `code`: numbered source + one row per nested call (`✓ ✗ ⊘ … –`, args compact, duration, size; `llm` model+tokens); kernel tools `code_start/attach/run/reset`: `⧉ kernel started servers (+N tools)`, cells `In [n]` + `✓ 0.04 s · 1 call`, stdout (12 lines), `Out [n]`, `↳` nested previews, restored `↻ restored after In [8]`, lost `kernel lost: reason`. Details sidebar Session tab adds Kernel/Attached/Cells/Snapshot/Liveness. | `ToolCallView.code_mode,nested_calls`, `tool_details.code_signal_rows/kernel_parts/kernel_signal_rows/kernel_heading/cell_result_parts`, `P:codemode.py`, `details.kernel_rows` |
| Subagent (task) cards | Two-line row: icon spinner (`⣿` when done, 50% blend of agent color; error color failed), capitalized agent name in agent color, task phrase; line 2 `model-effort (tier) · N tools · tokens · N external tools · duration`; route line (`low → provider/model · skipped X (monthly 1.5% left ≤ 2%, resets Nov 1)`; all_blocked in warning); state label `interrupted · run 1 / stopped by system: reason / running · run 2`; start refusal shows `not started` + redacted error (`selected_model` vs `resolved_model`); click opens child page (read-only, Back `←`/Esc/Up). Agent states: running (`status==spawned`), interrupted, finished, stopped. Agent color: host `color` else name hash (`agent_color`; build `#5C9CF5`, orchestrator orange). | `AgentView`, `ToolCallView.child_agent_ids/parent_call_id`, `timeline.agent_state/agent_state_label/agent_model_label/agent_actual_model/task_refusal`, `P:prototype.py::_agent_card_*`, `R:transcript/route.rs` |
| Subagent page | Task as grey prompt card, child's own context header (recorded request via `AgentTranscript`), thoughts/tools/reply, read-only composer, details sidebar for child; follow-up user card `from root` (`origin.kind=root_followup`), `Subagent stopped · by who / Reason`; interrupted state row; external-worker header/closing cards (`ui_support/external_worker.py::header_rows/footer_rows/external_blocks/task_note`: Provider, Model, Workspace, Permission mode, Tools, Authority, Needs-attention amber card, `Usage: unknown`). | `AgentTranscript(session,agent_id)`, `agent.body`, `view.external` |
| Agent report | `user` message with `origin.kind=subagent_report` -> compact `⮑ Agent  task · status` block (<= 2 preview rows, `… N more lines · open for the full report`), no `#N`. | `MessageView.origin{agent,run,status,agents[]}` |
| Notices | Dismissible `Notice` rows for `context.notice` diagnostics with `model_visible=false` (× chip or `x`/Delete when focused; dismissal stored in pref `dismissed_notices` as JSON `[workspace, session, seq]`, last 4096); model-visible ones render as undismissable `Context` rows; titles: `Subagent reports paused`, `Tool-call wrap-up reminder`, `Notice`; handoff divider `Context handed off · Earlier history is preserved · /handoff read`; `Provider overloaded · Retry n/3 after Ns` from `TurnView.retries`; worktree create/move rows `⎇ Created worktree name · from main @ sha · reason: "..."`; saved-turn row `Saved turn · sha · +added · −removed` (click -> `SessionTurnDiff(session,seq)`); projection failures as a `Notice` block. Placement by timestamp among turns. | `view.diagnostics`, `view.worktree.moves/commits`, `TurnView.context[kind=handoff],retries,commit_shas`, `view/notices.model_visible` |
| Errors | `turn.error` -> plain red `Error: …` row (supervisor-closing cancel shows none: `phase=cancelled`). `Disconnected…` stays a banner + disabled composer. | `TurnView.error` |
| Turn footer | Right-aligned dim `1:04 PM · model · 0.4s · turn ↑64K ↓3.1K · 81% cached · 800 r`; uses `completed_ts` (never "now"); durations `11.6s / 11m 40s / 1h 4m`. | `timeline.turn_footer_text/_turn_duration/_clock` |
| Live card | In-timeline `Live` card for ended calls (captions, `Sent to agent`, `Told voice`, commentary collapsed with count); delegated user cards `via voice`. | `view.live_calls`, `ui_support/live_card.py::card_rows/card_title/card_status/encode` |
| Streaming/scroll | Auto-follow only while at bottom; wheel/PageUp pauses, reaching bottom resumes; bottom buffer 20 rows; pill `↓ N more lines · Ctrl+End to jump` / `↓ New messages`; Ctrl+End or typing returns to live end; 30 % viewport padding on child pages. Activity word line above composer (`∴ Waiting for response…`, shimmer) + `4m40s ↓121k [stop]`; `⋯ 2 agents running · /tasks`; `⋯ Waiting for a free turn slot (4 of 4 running)` (needs `slot_wait`: only in raw events, use `SessionActivity`). | `P:turn_status.py::turn_status/running_agents` (activities: Waiting for response, Thinking, Running `tool`/`N tools`, Writing, Waiting for permission/answer) |
| Empty session | 4 grey hint rows (`ui_support/hints.py::pick_hints(seed=session id)`) in the middle, hidden while typing. | – |
| Selection/copy | Mouse selection copies via OSC52 + desktop clipboard; hover `Copy` on user cards and fences; `/copy` copies context JSON. | local |
| Keyboard focus | Empty draft + Tab focuses last clickable block; Up/Down/Tab/j/k move; Enter/Space open; `i` inspect; Esc returns. | `R:render.rs::targets` |

### 2.5 Right details sidebar (`Ctrl+L`, `Ctrl+X L`, top-bar `▥`)
Tabs **Session · Changes (stored `Files`) · MCP · Jobs · Logs**, width 24..60 (default 40), drag divider, docked >= 100 cols else drawer; `[`/`]` change tab, `c` copy session id, Esc closes drawer. `ui_support/details.py` (`TABS,normalize_tab,clamp_width,session_rows,modified_files,diff_preview_lines,kernel_rows,mcp_rows`), `P:prototype.py::_details_panel`, `R:details_sidebar.rs`.
- Session: rows Status, Agent, Model, Effort, Turns, Tool calls, Tokens (`unknown` for external), Context, Kernel rows. From view + controller (`AgentCurrent`).
- Changes: modified files with +/- counts from tool diffs, click expands diff preview (60 lines then clip notice), summary. From `modified_files(view)` over `ToolCallView.diff`.
- MCP: server list with status/notes from `Doctor` -> `DoctorResult.report` (polled every 30 s) and `view.mcp`.
- Jobs: `SessionJobs(session)` -> `JobSummary{job_id,command,cwd,status(running|completed|failed|timed_out|killed),background,exit_code,signal,started_at,finished_at,log_bytes,log_lines,log_available,can_background,source,persistent}` (cwd/pid/log path not shown); `SessionJobLog(session,job_id,tail_lines=200,offset,limit)` last 120 lines (`… earlier output not shown`); `SessionJobStop`; `SessionJobBackground` (only if `can_background`); keys ↑/↓ select, Enter/`l` log, `s` stop, `b` background, Esc back; polled ~1 s only while Jobs drawn. `P:jobs.py`. (View also has `view.jobs` from `shell.job_*` events.)
- Logs (Ctrl+E; also docked drawer 36 cols): `LogsRead(session,daemon_cursor,session_cursor,limit=50)` -> `LogsReadResult{daemon:DaemonLogPage,session:SessionLogPage}` entries `{source,seq,ts,level,kind,summary}`; folds routine entries (Ctrl+A), every toast mirrored as `client · toast` line; polls only while visible. `P:logs.py`.

### 2.6 Composer
`R:editor.rs`, `input.rs`, `render.rs` (composer card), `P:actions.py::submit`, `ui_support/completion.py`, `prompt_history.py`.
- Editor: grapheme editing, selection (mouse drag/click placement), undo/redo (Ctrl+Z/Ctrl+Y), Alt/Ctrl+B/F word move, Ctrl+A/E line ends, Tab inserts 4 spaces, Ctrl+J/Shift+Enter newline, 1 MiB draft cap, grows 8 -> 22 rows, empty-draft block cursor + placeholder, `Talk to the Manager…` in Manager.
- **Submit modes** -> `SessionEnqueue(session, mode, content, attachments[], attachment_labels[], client_request_id)`: Enter = `steer` (starts a turn when idle), Ctrl+Enter = `queue`, Alt+Enter = `interrupt`. `client_request_id` = uuid hex, **reused when the identical (target,text,mode,attachments) is resent** after a failure; a failed send restores the draft. `SessionStart` is unused by the TUI. Idle send creates a durable head input shown as `starting…`.
- **Stop**: Esc Esc within 1.5 s or `[stop]` click -> `SessionCancel(session, reason, drop_queue=true, return_queue=true)`; result `returned_messages[]` are put back in the composer (queue order, blank-line separated, before existing draft); single Esc shows hint; Ctrl+C clears a non-empty draft first (Ctrl+Z restores), empty -> cancel, two Ctrl+C within 1.5 s quit (first still cancels); with a selection Ctrl+C copies. Esc ends the root turn only; toast `N subagents still running (/tasks to stop)`. `P:actions.py::cancel`.
- **Waiting list** above composer: `▾ Steering N` (steer+interrupt) / `▾ Queued N` groups (4 rows, `+N more`), `#n text`; hovered/focused row controls: queued `↑ ↓ [Steer] ✕`, steering `[Interrupt] ✕`, interrupt `✕`. Commands: `SessionQueueMove(session,queued_id,offset=±1)` (queued rows only), `SessionQueueSetMode(session,queued_id,mode)` (promote queue->steer->interrupt, flash), `SessionQueueRemove(session,queued_id,return_content)`; removing a steer restores its full text + attachments (re-`AttachmentPrepare`d) to an empty draft only (`restore_if_empty`, info toast `restored from steer #n`). Keys: Tab on empty draft / Down from last block focuses; Up/Down/j/k/Home/End, Enter or `s` promote, Delete/Backspace/`x` remove, Alt+Up/Down reorder queued, Esc leaves. Source `view.input_queue` (items not the head input of an idle send), `P:prototype.py::_waiting_inputs/_queue_items/queue_edit`.
- **Shell `!`**: draft starting `!` (rail turns amber, `! bash` tag + "Enter runs in the workspace · output goes to context", `!type a bash command` placeholder, completion closed) -> `SessionShell(session, command)` -> `{shell_id}`; renders as a shell `TurnView(kind=shell)` with a bash row; refused in Manager.
- **`@` completion**: after 120 ms pause when token starts `@` -> `FileSearch(query, limit=30, include_directories=true, session)` -> `paths[]`; directories `dir/` insert without trailing space; 10 rows (prefix, substring/fuzzy, typo tiers), 32-entry request cache; `@path` is never expanded. **`/` completion**: local ranking over `commands.SPECS` + argument completion (`STATIC_ARGUMENTS`, `model_refs`, agents, `/theme`, `/export`, `/voice`, `/sessions`, `/attach`); Enter on a standalone `/command` runs the highlighted one; Tab forces. Unknown first word with `/` or `.` after the slash (`/Users/me/app.py fails`) is sent as text (`commands.is_command`).
- **Attachments**: `/attach <path>`, clipboard image paste (Ctrl+V; `clipboard.py::read_clipboard_image` <= 8 MiB), drag. `AttachmentPrepare(path|name+data)` -> `{attachment_id,name,kind(image|document|markdown...),preview}`; max 8; inline atomic chips `[Image 1]`/`[Document 2]` (stored `[image 1]`; per-kind numbering never reused after removal; removing the reference drops it); labels sent as `attachment_labels` (`image 1`, `document 1`); `AttachmentPreview(attachment_id,max_bytes)` -> `{media_type,data}` image preview (half-block/kitty/sixel/iterm2); attachments are per session (guard session change). Large paste (>=200 chars or >=3 lines) collapses to atomic `[Pasted ~N tokens]` and expands on submit.
- **Drafts & history**: unsent drafts kept per `(workspace, session)` for the client lifetime (text, cursor, selection, undo, attachments); persisted prompt history `~/.nexus` file (`prompt_history.load_history/append_history`, 500 entries, 1 MB each); Up/Down history; `/` `!` prompts also appended.
- **Model / effort / agent controls** (clickable in the composer: `Agent  model provider  effort`, price tier note): model picker modal `Select model` (search label + `provider/model`, groups Favorites/Recent/by provider+date, Ctrl+S sort `Updated ↓|Name A–Z`, Ctrl+F favorite, Ctrl+R `ModelsRefresh`, Ctrl+I details = `ModelShow(ref)`, dated models older than 6 months omitted, `←/→` effort); `ModelsList(selectable_only=true, scope="main")`, pick -> `ModelSelect(session, ref)` then `ReasoningEffortSelect(session, effort|null)`; remembered effort per provider/model; `Ctrl+T`/`Ctrl+X T` cycles effort (`AgentCurrent.supported_levels`); agent picker (`Shift+Tab` cycles, `Ctrl+G`/`a`) -> `AgentsList` filtered to root-capable, `AgentSelect(session,name)`, `AgentReset`, `AgentCurrent(session)` -> `{name,source,color,provider,model,reasoning_effort,supported_levels,stored_override,reasoning_effort_source,thinking_budget}`; locked after first turn (host enforces). Helpers `ui_support/model_choice.py::{sort_models,recent_models,rank_models,model_groups,preselected_effort,selection_effort}`, `fuzzy.py`.
- Voice strip/dictation, Live bar: 2.15/2.16.

### 2.7 Slash commands (`ui/cli/commands.py::SPECS`, dispatch `P:actions.py::command`)
Parsing: `parse()` resolves aliases to canonical names; `\` at line end or unclosed `"""` continues a prompt.
| Command (aliases) | Does | Host command(s) / data |
| --- | --- | --- |
| `/attach <path>\|clear` | attach file/image; no arg lists attachments | `AttachmentPrepare`, `AttachmentPreview` |
| `/new [id]` (`/clear`) | new session immediately (id = arg or uuid hex[:12]) keeping current root agent | `SessionOpen(create=true)` + `AgentSelect(new,agent)` + bootstrap |
| `/sessions [id]` (`/session`) | open left sidebar on Sessions, or switch to prefix-matched id | `SessionList`, local switch |
| `/open <name\|id>` | switch by immutable name or id; trashed/unknown -> notice | `SessionResolve(ref)` -> `resolution.logical_id`, `.trashed` |
| `/model [list\|ref]` | picker or set model | `ModelsList`, `ModelSelect`, `ModelShow` |
| `/effort [LEVEL]` (`/reasoning`) | picker (`default` + levels) or set | `ReasoningEffortSelect` |
| `/agent [list\|current\|reset\|NAME]` | list/show/select/reset root agent | `AgentsList`, `AgentCurrent`, `AgentSelect`, `AgentReset` |
| `/tools` | menu of tools used in transcript -> details page | view only |
| `/details` | session details dump | view only |
| `/context` | context usage modal | `ContextInspect` |
| `/reconnect` | re-attach and replay | resnapshot |
| `/restart` | stop and restart the workspace daemon (native only) | daemon lifecycle; **not available over web** |
| `/cancel` | cancel active turn | `SessionCancel` |
| `/close` | close current tab (last tab -> `/new`) | local |
| `/fork [at_seq]` | branch session, switch to it | `SessionFork(session,at_seq)` |
| `/handoff [read]` | idle cutover (fresh context, history kept) / read bounded brief | `SessionHandoff(session,request_id=uuid)`, `SessionHandoffRead` |
| `/export [json\|markdown\|jsonl]` | export (default markdown) shown in panel | `SessionExport(session,format)` -> `content` |
| `/help` | commands list + keys | local `SPECS` |
| `/hotkeys` | Settings -> Keyboard | local |
| `/tabs vertical\|top` | tab layout pref | local pref |
| `/notifications` | open drawer | local |
| `/exit` (`/quit`) | leave | local |
| `/worktrees` | project worktrees; per row Move here / Integrate into base / Remove checkout; child checkout review flow | 2.13 |
| `/copy` | copy assembled context JSON | `ContextInspect` (+clipboard) |
| `/diff [--staged] [ref]` | git diff panel | `GitDiff(staged,ref,session)` -> `patch,truncated` |
| `/cost` | token usage totals | `view.usage` |
| `/usage` | provider plan usage modal (also Ctrl+U) | `ProvidersUsage` |
| `/theme [dark\|light]` | theme toggle | local pref `theme` (`nexus-dark/nexus-light`) |
| `/settings` | Settings (opens on Appearance) | 2.10 |
| `/verbose` | toggle full tool output | local flag |
| `/mcp`, `/skills` | context dialogs | 2.3 |
| `/tasks` | background agents menu: `<agent> · <state label> · <task>`, `Cancel <agent> (ends this run)`, `Close <agent> (stop for good)` (legacy continuable) | `AgentCancel(session,agent_id)`, `AgentClose` |
| `/reload` | rebuild TUI + restart daemon (native only) | not applicable |
| `/reload-extensions` | reload extensions + MCP | `ExtensionsReload(trigger="chat")` |
| `/review`, `/commit` | send fixed prompt as a normal user turn ("Review the current workspace changes for correctness and report findings with file references." / "Review the current workspace changes, then create a commit for the completed work. Follow normal tool permissions.") | `SessionEnqueue` (default mode steer) |
| `/merge [note]` | worktree sessions only: enqueue `worktree_bar.merge_instruction(state, note)`; in main checkout a notice | `SessionEnqueue`; `view.root` |
| `/archived` (`/resume`) | browse/resume archived | `SessionListArchived`, `SessionPreview`, `SessionUnarchive` |
| `/voice [status\|download\|on\|off]` | dictation | 2.15 |
| `/live [stop\|pause\|resume\|mute\|new\|status\|revoke]` | GPT Live call | 2.16 |
| `/speak [download]` | speak latest answer (local ptts) | `Speak(session_id,download)`, `SpeakStop`, `SpeechStatus`, `SpeechPrepare` |
| `/manager [open\|live\|status]` | Manager | 2.17 |
| `/mock [list\|NAME\|clean] [--speed N] [--seed N]` | dev mode only | `MockList/MockStart/MockClean` |
Also palette commands from `Ctrl+P` and `Ctrl+X ?`.
`/update`: update help (Ctrl+X Z) using `UpdateStatus`. Manager sessions refuse `/model /effort /agent /fork /handoff` and `!` (`ui_support/manager.py::REFUSED_COMMANDS`, `SHELL_REFUSED`).

### 2.8 Approvals and questions
- Prompt docked above the composer (inert background): `◆ Allow <tool> <target>?`, `waiting on you · mm:ss` (from `PermissionView.ts`), **What happens** (targets/preview, <= 3 rows then `… N more`), **Why it asked** (`rule: default_rule`), choice chips with keys. Choices from `approval_choices(data)`: `Allow once` (`y`, `allow_once`), `Allow for session` (`a`, `allow_always`, hint = default rule; becomes `Allow this tool once`/`allow_once` when `persistence_available=false`), `Deny once` (`n`, `deny_once`), `Deny for session` (`d`, `deny_always`); all disabled except deny when targets unavailable. Up/Down/Tab select, Enter confirms. Ctrl+C cancels turn. `PageUp/PageDown` scroll. `PermissionResolve(session, request_id, decision, client_id)` -> `resolved` (false = "Permission already resolved by another client" warning; first responder wins daemon-side). `client_id` = `native-<random>` per process (use a stable per-client id). Source: `permission_prompt.py::permission_fields`, `ui_support/prompts.py::approval_choices`, `P:prototype.py::_prompt_of` (walks nested agent bodies; manager approvals surface in any session while a Live call follows the manager, title prefixed `Manager · `).
- Questions: pending = a `question` ToolCallView with `status=running` (also nested agents); `pending_questions(view)` -> `PendingQuestion{call_id,prompt,options<=3,agent}`; choices `1..3`, or free text + Enter -> `QuestionAnswer(session, answer, call_id)` -> `{resolved,error}` (error = shape failure text).
- Sessions sidebar "Needs you" rows answer approvals inline (`a` allow once / `d` deny once) using `ProjectSession.pending`.

### 2.9 Session lifecycle & misc actions
Open/switch (`SessionOpen`, `ProjectSessionOpen`), rename (`SessionRename`), archive/unarchive, fork, handoff, export, ownership tree (`SessionTree(session,namespace)` -> indented ownership list with refresh; `SessionInspect(session,namespace)` -> facts, durable transcript, request/context; child nodes never offer send/resume/fork; root nodes open the main session), focus reporting `SessionFocus(session, client_id)` (on start, after every switch **before** replay, every 15 s heartbeat, empty on exit; unknown-command error switches reporting off). `P:focus.py`, `ownership.py`.

### 2.10 Settings (full-screen modal `settings_page/`, `P:workflows.py` + `settings_pages/*.py`, `ui_support/settings_page.py`)
Typed page model (blocks: heading, note, row{label,description,scope,error,control}, controls toggle/segmented/select/stepper/text(secret)/button/readout, tabs, ordered list with up/down/remove/add, buttons, section collapsible, table, progress, callout); every operation sends `value`(+`action,index`) and the page is rebuilt from host data. Root: `/` search, Animate activity, Monochrome (Alt+A / Alt+M), inlined Appearance+Layout rows, drill rows with summaries (`2 of 6 connected`, `N shortcuts`). Keys: arrows, `←→` segmented/stepper/tab, Space/Enter, Ctrl+PgUp/PgDn tabs, `Alt+1..9` jump area, `?` -> Keyboard, Esc back. Autosave 700 ms after typing, flush on navigate/close; validation or disk-hash conflicts keep the edit with an error; leaving asks to discard invalid. Resets are confirmed and move files to settings trash. Text fields: 512 chars, paste only into the focused editor, secrets masked.
| Area | Content | Host commands |
| --- | --- | --- |
| Appearance | Theme Dark/Light, Reset | local pref |
| Layout | Vertical tabs, Details sidebar, Show context header, Centered conversation (80 cols); Reset | local prefs |
| Keyboard | read-only table of `SHORTCUTS` + `LEADER_SHORTCUTS` + list shortcuts | local |
| Providers | card per provider (connected first): Codex (browser or device code), GitHub Copilot (device code), OpenCode Go (API key password field), Claude (browser then masked code field; no Disconnect), Antigravity (manual `agy` sign-in + `Refresh models`); `Refresh plan usage` select (60..3600 s, default 300) | `ProvidersStatus`, `ProviderLogin(provider,method,domain)`, `ProviderLoginPoll(login_id)`, `ProviderLoginCode(login_id,code)`, `ProviderLoginCancel`, `ProviderKeySet(provider,key)`, `ProviderLogout`, `ProviderModelsRefresh(provider)`, `UsageRefreshSettings(+Set seconds)`, `SetupStatus` |
| Models | Default model chain (`Default Model`, ordered list, add via picker), Session titles (on/off + Title model: tier or model), Claude Code (`Allow Claude Code as a main model`, `Claude Code subagents run via` worker/sdk), Subagent tiers (low/medium/high/custom each ordered list: up/down/remove/`+ Add model`, per-model effort select, status `in use/fallback/skipped`, `Reset <tier> to default…`), Catalogue Refresh | `DefaultModelSettings`, `DefaultModelSet(refs)`, `SessionTitleSettings/Set(enabled,model)`, `ClaudeCodeSettings/Set(main_enabled,subagent_backend)`, `ModelTiers`, `ModelTierSet(tier,refs,efforts)`, `ModelTierReset`, `AgentMaxTierSet`, `ModelsList(scope=subagents\|all)`, `ModelsRefresh` |
| Voice & speech | Voice input enable, auto-send, device, recording limit (secs), model status + load/download, Speech language/voice/speed/device/model download/reset | `VoiceStatus`, `VoicePrepare(force,allow_download)`, `VoiceRemove`, `SpeechStatus`, `SpeechPrepare`, `SettingsWrite` (config) via `ui_support/voice_settings.py::set_voice_config`, `speech_settings.py` |
| GPT Live | enable, model (read-only `gpt-live-1-codex`), voice, longest call (1..240 min), paused-call limit (5..3600 s), carried turns (0..100), spoken progress, instructions file, raw event log, reset | settings via `ui_support/live_settings.py`, `SettingsWrite` |
| Agents | `New sessions start with` (`AgentDefaultSet(name,scope)`), file list (Edit/New/Reset), agent editor: Run on (Session model / Specific model + Fallback 1..8 / Tier), per-mode fields, Tiers checkboxes + `Make default`, conflict notes, `Edit prompt file…`; built-in edit makes a scoped override | `AgentsList`, `SettingsInventory/Read/Write/Reset/Delete`, `ModelsList`; `ui_support/agent_frontmatter.py`, `tier_settings.py::new_agent_template` (`tiers: [low, medium]`) |
| Tools | file list + Global/Project scope tab, New file/Reset category | `SettingsInventory(scope)`, `SettingsRead(scope,category,id)`, `SettingsWrite(...,expected_sha256)`, `SettingsDelete`, `SettingsReset` |
| MCP servers | Global then Project (no scope tab): per-server Status, Transport, Command/URL, Defined in, Tools, Tool filters, Ignored keys, Enabled toggle, Tool loading Search/All; whole-file problems as callouts; `Add server…` -> JSON editor -> masked confirm -> background `SettingsMcpAdd(dry_run preview then real)`; result as toast | `SettingsMcpEnabledSet`, `SettingsMcpLoadingSet(scope,server,mode,expected_sha256)`, `SettingsMcpAdd`, `McpServerShow` |
| Skills | file list + scope; `Add from npx skills…` (form `source [skill …]`, third-party-code confirm, background `SkillsInstall(scope,source,skills,overwrite)`, toast, conflicts offer overwrite) | `SkillsInstall`, `SettingsInventory...` |
| Config / Hooks / Soul | file categories in the CONFIGURE group (surfaces.md lists Hooks, Config, Soul; the native page list in `PAGE_AREAS` is the 11 above - Config etc. are reachable as raw file categories via `/settings` file lists) | `SettingsInventory` categories |
| File editor | text editor over a settings file with hash conflict detection, autosave, host validation messages (`SettingsWriteResult{status,sha256,loaded,unloaded,failed,config_reloaded,message,lines}`) | `SettingsRead/Write` |
| First-run setup | only asks to connect a provider; first connected + newest model saved; never required in dev mode | `SetupStatus` -> `required,providers`, `SetupSave(provider,model)` |

### 2.11 Notifications, toasts, drawer
- Toast (host-originated in TUI via `shell.toast(text, level, key, body, action)`; web client generates its own): levels info 4 s / success 3 s / warning 8 s / error 12 s, paused on hover, same-key merge `×2`, max 3 visible + `+N more`, top-right over everything, click `×` or action; identical text within 5 s suppressed; `Ctrl+X x` dismiss all, `Ctrl+X y` run newest action. Every toast also logged as `client · toast` in Logs. `R:render/toasts.rs`, `P:actions.py::toast/flash/notice_level/split_toast`.
- Drawer: client-side history (50, newest first, `glyph text age`), counter `◔ N` in top bar, `/notifications`, `Ctrl+X Shift+N`; Esc/click-outside closes. `R:render/notifications.rs`.
- Completion sound: two-note cue on `turn.completed/failed` for the shown session and on unread transition for background tabs (`NEXUS_COMPLETION_SOUNDS=off` disables; no cue on replay/cancel/subagent). `ui_support/voice_capture.py::play_cue`.
- Banners: `Disconnected…` disables composer; reconnect success toast.

### 2.12 Keyboard shortcuts (source `ui_support/shortcuts.py` + `rust/tui/src/main.rs`, `input.rs`, `editor.rs`)
| Key | Action |
| --- | --- |
| Enter / Ctrl+Enter / Alt+Enter | send=steer / queue / interrupt |
| Shift+Enter, Ctrl+J | newline |
| Ctrl+P | command palette |
| Ctrl+N | new session |
| Ctrl+O, Ctrl+B | open Sessions / toggle left sidebar (`shortcuts.py`; older parity text also lists Ctrl+S for Sessions) |
| Ctrl+K | search sessions (in Sessions) |
| Ctrl+F | fork session |
| Ctrl+G, `a` (empty composer) | root-agent picker |
| Shift+Tab | cycle root agent |
| Ctrl+L | toggle details sidebar |
| Ctrl+S | open Settings (`shortcuts.py`); inside the model picker Ctrl+S = sort |
| Ctrl+I | context preview and usage |
| Ctrl+T | cycle reasoning effort |
| Ctrl+Space | dictate |
| Ctrl+E | logs drawer |
| Ctrl+U | provider usage (takes precedence over delete-to-line-start; `r` refreshes in modal) |
| Ctrl+C | clear draft / cancel turn / copy selection / double -> quit (see 2.6) |
| Esc, Esc Esc | back/close dialogs / stop turn within 1.5 s |
| Ctrl+R | reconnect (on an MCP server row: restart) |
| Ctrl+Q | quit |
| Ctrl+Z / Ctrl+Y | composer undo / redo |
| Ctrl+A / Ctrl+E | composer line start / end (Ctrl+E is the Logs drawer toggle outside the editor; Ctrl+A folds routine logs while Logs is open) |
| Alt+B / Alt+F, Alt/Ctrl+Left/Right | word move |
| Ctrl+End, PageUp/Down | scroll transcript / jump to bottom |
| Ctrl+W | (Sessions/picker) worktrees; Alt+L project scope toggle on legacy page |
| Alt+A / Alt+M | Settings: animate activity / monochrome |
| Alt+1..9 | Settings: jump to area; `?` Keyboard page; `/` Settings search |
| F6 | focus composer <-> sidebar |
| Tab on empty draft | focus transcript blocks / waiting list |
| Ctrl+X leader (3 s window) then: `m` model, `v` dictation, `n` new, `o` sessions, `f` fork, `g` agent, `b` sessions, `l` details, `s` settings, `i` context, `e` logs, `t` cycle effort, `d` start/stop Live, `p` pause/resume Live, `a` or `.` Manager toggle, `u` usage, `r` reconnect, `c` context popover, `z` update help, `?` shortcuts, `y` run newest toast action, `x` dismiss toasts, `N` notifications drawer |

### 2.13 Worktrees and `/merge`
- No worktree modes; agent creates/moves with its own tools; bar follows `view.root`; history rows from `view.worktree.moves`; `WorktreeView.history` (name, branch, base, merged/unmerged/removed, turn range); commits -> saved-turn rows (`SessionTurnDiff(session,seq,path,max_bytes)`, `SessionTurnRevert(session,seq,include_later,confirm_token)`).
- `/worktrees` (also `Ctrl+W`): `SessionWorktreeList(session)` -> project worktrees (`← ` marks this session; merged / `N unmerged`, `dirty`, `checkout removed`, sessions); row actions: Move this session here -> `SessionWorktreeMove(session,to,reason="Moved by the user")` (idle only; `to` = `main` or a name), `Integrate into base branch…` -> `SessionIntegrate(session,confirm_token)` (preview then confirm token), `Remove checkout…` -> `WorktreeRemove(child_id,confirm_token)`, `SessionWorktreeStatus`, `SessionWorktreeTrust(session,decision,digest)`; fallback `WorktreeList`. Subagent child checkouts: `WorktreeInspect`, `WorktreeReview(child_id,review_id,cursor,limit)` (128 KiB pages), `WorktreeAcknowledge(child_id,review_id,digest)`, `WorktreeIntegrate(child_id,review_id,digest,confirmation_token)`, `WorktreeDiscard(child_id,force,review_id,confirmation_token)`; tokens come from the host and are replayed only after explicit selection. `P:worktree_pages.py`, `worktree_bar.py`, `workflows.py::review_pages/confirm_worktree/worktree_outcome`.
- `/merge`: see 2.7.

### 2.14 Subagents (see 2.4) - also `/tasks`, Esc toast, `agents_running` status line.

### 2.15 Dictation
`Ctrl+Space`, `Ctrl+X V`, `/voice [status|download|on|off]`. Flow: `VoiceStatus` (-> `state absent|...,progress,cached,device,enabled,auto_send,max_seconds,configured_device`), first use consent for model download (`VoicePrepare(allow_download)`), capture 16 kHz mono (max `max_seconds`), partial previews every ~0.2 s via `VoiceTranscribe(audio,request_id,session,partial=true)` (preview inserted inline in the composer at the capture position, red recording square + `mm:ss` on the activity rule, words resolve over 3 frames), final transcribe `VoiceTranscribe(partial=false)` -> `text` inserted as editable text (typed suffix preserved). Stop keys: Esc keep without sending, Enter keep and send (overrides `auto_send`), other keys keep + apply the key; capture-limit stop honors `auto_send`; explicit discard -> `VoiceCancel(request_id)`. Web: `POST /v1/web/voice`. `P:voice.py`, `ui_support/voice_capture.py`, `voice_settings.py`, `R:main.rs`, `input.rs::voice_stop`.

### 2.16 Live (GPT Live voice calls)
`/live`, `Ctrl+X D` start/stop, `Ctrl+X P` pause. Consent dialog first (`LiveConsent(accept, scope="" | "manager")`, `/live revoke [manager]`); `LiveStatus` -> `available,reason,state,phase,active_session,call_id,voice,muted,paused,pause_remaining_s,elapsed_s,max_minutes,levels_in,levels_out,consent`; `LiveStart(session, offer_sdp, voice, new_conversation)` -> `answer_sdp` (**WebRTC SDP offer/answer; the TUI uses the external `rust/live` helper process**, `ui_support/live_helper.py`), `LiveMute`, `LivePause(paused)`, `LiveStop(reason)`, `LiveHelperEvent(session,kind,detail)`. UI: call bar/pane under composer (`LISTENING ▁▃▅█ … 02:41`, phase, mic levels, Muted/`PAUSED mm:ss` badge, 6-row pane with captions, expand/collapse, `· in <session>`, `attention` chip when the manager has a pending approval), snapshot refresh every 250 ms while open. Timeline `Live` card at end. Needs microphone + WebRTC: **not feasible in the NSH web target without a WebRTC path**; treat as out of initial scope (surfaces.md: "The NSH client has no Live support"). `P:live.py`, `ui_support/live_helper.py`, `live_settings.py`, `R:live_bar.rs`, `live_wave.rs`, `live_anim.rs`.

### 2.17 Manager
Off by default (`[manager] enabled=false`). `ManagerState` -> `enabled,session,state,model,provider,signed_in,sign_in,codex_signed_in,live_consent,live_manager_consent,live_ready,managed[],max_launches_per_turn,max_live_managed`; `ManagerOpen` -> `{session,created}`. Pinned entry block (status glyph, `Manager`, `pinned · m opens`, `Voice M` button, status line `Ready · 2 sessions launched / Working / Needs you / Not signed in · sign in to ChatGPT (Codex) under Settings > Providers / Off · [manager] enabled = false`, `codex/gpt-6-luna · Voice ready`, privacy line). Top-bar toggle (`Ctrl+X A`/`.`, remembers the session left, restores it; never a tab); manager session: composer `Talk to the Manager…`, no role/model/effort/fork/handoff/shell; title `Manager`; managed-sessions board (`manager_board.py`: joined `managed` ids with session rows, six rows + `+N more · /sessions`, collapsible, `needs you` marker, row click = switch); autofocus on `session.linked kind=managed` / `manager.show` events (needs raw events: **not reachable via view**, use `ManagerState.managed` polling). Helpers `ui_support/manager.py::{entry,status_of,live_line,status_lines,REFUSED_COMMANDS,CONSENT_LINES}`, `P:manager.py`, `manager_follow.py`, `manager_board.py`, `R:render/manager.rs`.

### 2.18 Dev mode
`nexus --dev chat` / `NEXUS_DEV=1`: `/mock` command; `Health.dev`. `MockList -> MockListResult{scenarios[MockScenarioInfo{name,summary,tags,est_seconds,interactive,slow}]}`, `MockStart(scenario,speed,seed,session)` -> `{session,scenario,turn_id,interactive}`, `MockClean`. Setup never required.

### 2.19 Other
- Splash/startup: splash with milestones (daemon, workspace, providers, session, ready); NSH equivalent = loading state. `R:splash.rs`.
- Themes: Grok Night/Day palettes; accent; agent colors; ASCII (`NEXUS_ASCII`) and monochrome modes; hover/pressed states; backdrop dim 50 % behind modals. `R:render.rs::Palette`, `render/components.rs`, `render/appearance.rs`, `rust/widgets`.
- Context popover (`Ctrl+X C`), update help (`Ctrl+X Z`), Health/`Doctor` poll 30 s, `ProjectSessionOpen` for any project, TUI crash log, perf trace (`NEXUS_TUI_TRACE`) - TUI-only.
- Message page (click a user message with attachments -> whole text + image metadata modal); submitted image preview.
- Read-only inspectors for ownership tree, session export, `Doctor`.
- Provider-overload retry rows, external-worker cards (Claude Code / Antigravity) - see 2.4.
- Settings trash/undo for deletes; `SessionRestore(trash_id)` exists in protocol.

## 3. Shared presentation helpers to port
### 3.1 `nexus/ui_support/` (pure; shared contract)
| Module | Key functions / constants | Produces |
| --- | --- | --- |
| `tool_details.py` | `flatten`, `tool_detail_sections`, `sections_to_text`, `styled_lines`, `compact_args`, `code_signal_rows`, `kernel_parts`, `kernel_heading`, `kernel_signal_rows`, `cell_result_parts`, `DetailRow/DetailSection`, `VALUE_LIMIT=20000`, `SECTION_ROWS=400` | labelled sections + toned lines for the tool inspector; code/kernel compact rows |
| `timeline.py` | `group_tools`, `tool_heading`, `tool_output`, `format_arguments`, `tool_status`, `tool_summary`, `tool_row_text`, `running_output_tail` (4 lines), `split_diff_files`, `diff_sections`, `diff_split_rows`, `diff_unified_rows`, `structured_diff`, `thought_title`, `agent_state`, `agent_state_label`, `agent_model_label`, `agent_actual_model`, `task_refusal`, `turn_footer_text`, `turn_agent_label`, `submitted_attachment_summary`, `_clock`, `_turn_duration` | rows, summaries, footers, diffs, agent states |
| `tool_signal.py` | `tool_signal`, `closed_summary`, `shell_preview`, `strip_job_header`, `strip_workspace`, `clip_cells` (40), `SignalRow/SignalView`, `FIRST_LINES/LAST_LINES` | "relevant-only" expanded tool bodies and closed-row text |
| `context.py` | `header_prompt_sections`, `header_system_prompt`, `context_groups`, `tool_groups`, `tool_entry`, `page_markdown`, `context_summary`, `context_measure`, `context_usage`, `context_detail_usage`, `context_turn_usage`, `context_pricing_note`, `price_tier_thresholds`, `thinking_status`, `estimate_tokens` (~4 chars/token), `context_json` | context meter, usage modal, dialogs (Rich only for Markdown rendering in Python; Rust uses its own) |
| `context_header.py` | `header_blocks`, `agent_color` (name hash, `AGENT_COLORS`), `one_line_preview`, `group_tools`, `scope_counts`, `schema_param_rows`, `tool_detail_lines` | header chips |
| `details.py` | `session_rows`, `modified_files`, `diff_preview_lines`, `kernel_rows`, `mcp_rows`, `TABS`, `normalize_tab`, `clamp_width` (24..60, default 40) | details sidebar content |
| `sidebar.py` | `sessions_payload`, `project_rows`, `visible_session_rows`, `files_payload`, `git_payload`, `preview_panel`, `human_size`, `is_manager_row/is_child_row`, `GIT_GROUPS`, limits 400/500/500, width 24..60 default 34 | left sidebar data |
| `session_status.py`, `session_groups.py` | `session_status`, `relative_time`, `session_subline`, `SESSION_WORDS`; group-by-project/day | status/age/subline |
| `prompts.py` | `approval_choices`, `pending_questions`, `PromptChoice`, `PendingQuestion` | approval/question rows |
| `completion.py` | `rank_menu`, `command_matches`, `file_menu`, `canonical_command`, `STATIC_ARGUMENTS`, `FILE_LIMIT=30`, `MENU_LIMIT=10`, `root_agents` | slash/`@`/argument menus |
| `model_choice.py`, `fuzzy.py` | `sort_models`, `recent_models` (6-month rule), `rank_models`, `model_groups`, `preselected_effort`, `selection_effort`; `fuzzy_match/fuzzy_filter/highlight_spans` | model picker |
| `usage.py` | `bar`, `tone` (ok <70, warn <90, critical), `reserve`, `reserve_phrase`, `summary`, `heading`, `usage_lines`, `reset_phrase`, `duration` | `/usage` modal text |
| `hints.py` | `EMPTY_HINTS`, `pick_hints(seed)` | empty-session tips |
| `text.py` | `escape_controls`, `sanitize` | control-safe text (all host text is untrusted) |
| `prompt_history.py` | `load_history`, `append_history` (500 entries) | history |
| `shortcuts.py` | `SHORTCUTS`, `LEADER_SHORTCUTS`, `KEYBOARD_SHORTCUTS` | keymap + help |
| `settings_page.py`, `settings_help.py` | block builders, `accepts`, `operations`, `SETTINGS_HELP`, `SETTINGS_SECTIONS` | settings page model |
| `tier_settings.py`, `agent_frontmatter.py` | `tier_label`, `move_ref`, `remove_ref`, `titles_explanation`, `agent_tiers_notes`, `toggle_tier`, `make_default_tier`, `new_agent_template`; `agent_fields`, `set_agent_fields`, `run_mode`, `set_run_mode` | Models/Agents pages |
| `manager.py`, `live_card.py`, `live_helper.py`, `live_settings.py`, `external_worker.py` | see 2.16-2.17, 2.4 | |
| `voice_capture.py`, `voice_settings.py`, `speech_settings.py`, `speech_download.py`, `clipboard.py`, `native_images.py`, `native_schedule.py`, `session_controller.py` | audio/clipboard/image helpers; `SessionController` bootstrap/stream/switch/select_model/select_agent/cancel logic | |
### 3.2 `nexus/ui/ratatui/` projection logic (not in `ui_support`, but required)
`prototype.py::_project_turn/project/_agent_card_*/_queue_items/_waiting_inputs/_starting_input_blocks/_prompt_of/_tab_rows/_details_panel/_context_label/_context_tiers/_turn_number/_fold_summary/_agent_report_block/_change_block/_agent_origin_row`; `verb_groups.py` (`split_runs`, `build_run`, `activity_entries`, `_label`, `format_time`); `turn_status.py`; `transcript_data.py::tool_presentation`; `stream_projection.py`; `permission_prompt.py::permission_fields`; `worktree_bar.py` (`session_worktree_label/kind/state`, `root_move_text`, `merge_instruction`); `jobs.py::job_row`; `codemode.py::member_fields/sidebar_rows`; `context_sections.py` (skill_card, mcp_list, server_index, section, restart, server_page); `manager_board.py::board`; `ownership.py`; `workflows.py::session_rows/_time_bucket/_session_row`; `focus.py`; `preferences.py` (pref defaults/clamps); `settings_pages/*` (page builders).

## 4. Suggested work packages (independent, parallel)
Target crate: `rust/app` (NSH). Proposed module ownership (new files unless noted). Cross-cutting API for all packages: WP1's `Api` (command send/result decode) + `Store` signals (view, sessions, prefs, toasts).
| WP | Scope (features) | Owns |
| --- | --- | --- |
| **WP1 Transport, view store, shell state** | Auth/CSRF/ticket, `command()` with typed result decode (enum of results), snapshot + SSE `view`/`workspace` frames, ops applier (1.3), resync/reconnect (1.4), session switch and generation guards, per-session draft/disclosure maps, prefs persistence (`nsh/storage`), polling loops (sessions 3 s, doctor 30 s, context on cursor), toast/notification store, `SessionFocus` heartbeat, `Health` handshake. | `rust/app/src/{api.rs, sse.rs, ops.rs, view.rs (typed view structs), store.rs, prefs.rs, poll.rs, model.rs (rewrite), lib.rs (wiring)}` |
| **WP2 Chrome and sidebars** | Top bar (tabs, status glyphs, breadcrumb, buttons, notification counter/drawer, toasts UI), left sidebar (Projects incl. folder picker, Sessions incl. search/archive/rename/fork/handoff/children tree/Needs-you answers/archived, Files + preview, Git), right details sidebar (Session, Changes, MCP, Jobs, Logs), layout breakpoints, resizable panels, keyboard focus model. | `rust/app/src/chrome/{topbar.rs, tabs.rs, toasts.rs, notifications.rs}`, `sidebar/{projects.rs, sessions.rs, files.rs, git.rs, picker.rs}`, `details/{session.rs, changes.rs, mcp.rs, jobs.rs, logs.rs}` |
| **WP3 Timeline presentation** | Port of `_project_turn` and helpers: user cards/chips/folds/sticky, markdown (+code blocks, tables, mermaid fallback to source), thinking, tool rows/groups/signals/running tails, change blocks + diffs, question card, code-mode/kernel rows, subagent cards + route line, agent report, notices/dismissal, errors, retries, turn footer/clocks, streaming head, scroll/follow, empty hints, live card, external worker cards, handoff divider, saved-turn/worktree rows. | `rust/app/src/timeline/{mod.rs, project.rs, groups.rs, tools.rs, signal.rs, diff.rs, markdown.rs, thought.rs, agents.rs, notices.rs, footer.rs, scroll.rs}`, `present/{timeline.rs, tool_signal.rs, tool_details.rs (section model), time.rs}` + golden fixtures |
| **WP4 Composer and commands** | Editor (undo, chips, paste collapse, history), submit modes + idempotent `client_request_id`, stop/Esc/Ctrl+C semantics, waiting queue UI + queue commands, shell `!`, `@`/`/` completion, attachments (`/v1/web/attachment`, previews, labels), drafts, model/effort/agent pickers + remembered effort, slash command dispatcher for all of 2.7 (except daemon-lifecycle ones), command palette, keymap (2.12), turn status line + `[stop]`, `/diff`, `/export`, `/review`, `/commit`, `/merge`, `/tasks`, `/open`, `/new`, `/fork`, `/handoff`. | `rust/app/src/composer/{editor.rs, submit.rs, queue.rs, completion.rs, attachments.rs, history.rs, pickers.rs}`, `commands/{registry.rs, dispatch.rs}`, `keymap.rs` |
| **WP5 Approvals, session ops, worktrees** | Permission prompt + choices + first-responder feedback, question prompt, Needs-you answering from any row, nested-agent prompts, manager approvals; session lifecycle ops (rename, archive/unarchive, fork, handoff, export, resolve, tree/inspect pages); worktree bar/labels, `/worktrees` flows, session integrate/remove/move with confirm tokens, saved-turn diff/revert, subagent child worktree review pages; `Doctor`/`GitStatus` branch source. | `rust/app/src/prompts/{permission.rs, question.rs}`, `sessionops/{lifecycle.rs, ownership.rs}`, `worktrees/{bar.rs, pages.rs, review.rs}` |
| **WP6 Context and inspectors** | Context header, context dialogs (system prompt, environment, AGENTS/MEMORY, skills list + `SkillInspect`, tools + tool page, MCP list/server page/restart/refresh-all/toggles/loading mode with first-turn lock), context meter + usage modal + price tiers, provider usage modal (`/usage`), tool details modal, message page, submitted image preview, subagent page frame (context from `AgentTranscript`), details modal generic Markdown/panel host. | `rust/app/src/context/{header.rs, dialogs.rs, tools.rs, skills.rs, mcp.rs, meter.rs, usage.rs}`, `inspect/{tool_details.rs, message_page.rs, agent_page.rs, panel.rs}` |
| **WP7 Settings and onboarding** | Typed settings-page engine + all areas of 2.10 (Appearance, Layout, Keyboard, Providers incl. login flows/polling/masked fields, Models/tiers/titles/Claude Code, Voice & speech settings, GPT Live settings, Agents editor, Tools/Skills/MCP file editors with hash conflicts + autosave + reset/trash, Skills install, MCP add), setup/first-run, `/settings` search, update/database notices. | `rust/app/src/settings/{page.rs, appearance.rs, layout.rs, keys.rs, providers.rs, models.rs, voice.rs, live.rs, agents.rs, files.rs, mcp.rs, skills.rs, setup.rs}` |
| **WP8 Voice, speech, Live, Manager, dev** | Dictation via `/v1/web/voice` (capture, partials, stop keys, consent/download), `/speak`, completion cue, Manager entry/board/toggle/`ManagerState`/`ManagerOpen` polling (no raw events), Live status/consent/settings UI and a decision on WebRTC (likely deferred), `/mock` dev mode, clipboard-image paste helper (platform module), notification sounds. | `rust/app/src/voice/{dictation.rs, speak.rs}`, `manager/{entry.rs, board.rs, toggle.rs}`, `live/{bar.rs, consent.rs}` (stub), `dev/mock.rs`, `platform/{audio.rs, clipboard.rs}` |
Sequencing: WP1 first (others code against its `Api`/`Store` traits with fixtures); WP3, WP4, WP2 can start on fixtures (`--fixture` snapshot JSON). Golden tests: generate Python fixtures from `nexus/ui_support` + `nexus/ui/ratatui` for WP3/WP6/WP2 outputs and diff in `cargo test`.

### Known gaps / risks for the client team
- View lacks `slot_wait` and manager show events; use `SessionActivity` / `ManagerState` polling.
- Rust-side SSE streaming on the web target is the main technical risk (plan `nsh-cross-platform-clients.md` §4.1).
- Several TUI behaviors are local-only (prefs, history, drafts, notification history); NSH must persist its own (storage), not the host.
- `/restart`, `/reload`, `Shutdown`, `WebLaunch`, daemon start are impossible over `/v1/web/*`.
- Live needs WebRTC + microphone; dictation needs 16 kHz WAV encode in-app.
- Unverified in docs themselves: many Rust/PTY items were "written, not run" (see parity ledger); treat visual details as specification-by-intent.
