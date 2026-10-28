# Nexus 0.10 plan (branch nexus10)

Nexus rebuilt from scratch, starting at the provider wire: the exact JSON that
goes out and every event that comes back, token by token. It grows step by step
toward the two clients, the Ratatui TUI and NSH. Old Nexus (branch `nexus01`,
checkout `~/repos/nexus`) is reference and a parts bin. Nothing comes across
without a written reason.

Branch `nexus10` (an orphan branch, checked out as a worktree at
`~/repos/nexus10`). It is released as **0.10.0** once it replaces the old code
("10" is binary for 2).

This plan was rewritten on 2026-10-28: finished steps are summarised in one
line each, and their full design notes, verification logs and delivery reviews
stay in git (`git show 13651aa:PLAN.md`, `git show 13651aa:INTERFACE_WORK_PLAN.md`).
The docs in `docs/` describe what the code does now.

## Interface direction

Reuse the old Nexus TUI broadly, including settings and modal pages, adapting
it to the current Core API and the one-timeline principles. Polish a cohesive
design language for tokens, spacing, typography and states
(`interface/design-language.md`, `interface/theme.toml`). The TUI leads; NSH
follows once the TUI look is settled.

## Principles

1. **Context is visible.** The user can see everything the agent sees: the
   request body, every stream event, every tool argument and result, labelled.
   Any clipping is announced.
2. **Raw and normalized, side by side.** Each part a provider returns keeps its
   exact wire item (`raw`) and the provider that made it (`origin`). A replay to
   the same provider is lossless (reasoning `encrypted_content`, item ids). Other
   providers use the normalized fields.
3. **Task tools vs. control.** Tools act on the workspace: read, bash, edit,
   write, and code to script them. Changing Nexus itself (which session, workspace or worktree, which
   model, forking, subagents) is a separate control plane in a `nexus`
   namespace. Those are commands with their own event type and permissions. The
   user can run them directly, and the agent gets them only when granted. A
   worktree is an optional property of a session, never a mandatory tool.
4. **One step at a time.** Each step ends with something you can run and see.
5. **Separate state.** The command is `nexus` (the old uv tool install was
   removed). State stays in `~/.nexus10` (`$NEXUS10_HOME`) so nothing is shared
   with old Nexus (`~/.nexus`), which still runs from its own checkout.
6. **uv only.** `uv sync`, `uv run`, `uv add`. Python is pinned in
   `.python-version` and dependencies are locked in `uv.lock`.
7. **One timeline.** The timeline the user sees and the context the agent sees
   stay as close as possible. Every item the model gets (system prompt, messages, tool
   results, notices, context insertions, handoffs) is shown to the user in
   order. A child launch appears in its parent’s context line; opening it shows
   the child’s own context line. Activity indicators (running, waiting, done)
   may update without becoming model context. Execution details are labelled
   separately from what the model received. Presenting context well is a core
   idea of Nexus.
8. **Append-only context.** Context already sent to a provider is never
   edited: it is cached, and a change invalidates the cache. A session's system
   prompt is fixed when the session starts. Later changes (a new directory, a
   switched setting) go forward as new items. Actions that will invalidate the
   cache anyway (switching model or agent) warn first.

## Layout

Each part of the repo is a self-contained project with its own toolchain. The
root holds only plans and docs.

```
docs/             how the code works; start at docs/README.md
INTERFACE_PLAN.md the TUI and NSH interface spec (both clients)
interface/        shared manifest, theme tokens, capability matrix, design language
scripts/nexus     repo launcher (nexus, nexus tui, nexus web, nexus check …)
nexus-core/       Python (uv): everything that is not a UI. Import name `nexus`, command `nexus`
  pyproject.toml, uv.lock, .python-version
  nexus/
    llmhub/         providers normalized to one conversation + stream shape
      types.py      Message/parts (Text, Reasoning, ToolCall, ToolResult), Request, stream events
      sse.py        server-sent events parser
      responses.py  OpenAI Responses dialect (codex, grok) + ResponsesQuirks
      chat.py       OpenAI Chat Completions dialect (copilot, opencode) + ChatQuirks
      providers.py  the provider table: dialect, endpoint, models.dev id, quirks, default model
      catalog.py    models.dev catalog: download, 24 h cache, per-provider model list
      hub.py        joins sign-ins to the table; `provider/model` refs; default model
      auth/         each provider's sign-in; hands the dialects a header source
        store.py    ~/.nexus10/auth.json (0600, atomic, flock)
        device.py   RFC 8628 device flow (copilot, grok)
        codex.py, copilot.py, grok.py, opencode.py
    tools/          read, bash (background jobs too), edit (replacement or apply_patch), write, ask, code; web_search/web_fetch planned
    workspace.py    workspace control tools: expose on demand, move directory, worktree specs
    worktrees.py    Git worktree list/create/delete, through subprocess argument arrays
    kernels.py      Monty kernels for the `code` tool, owned by the daemon
    bash_jobs.py    background bash jobs, owned by the daemon
    snapshot.py     stored session configuration, tool schemas and AGENTS.md
    observations.py per-session read observations (read-before-edit)
    questions.py    the `ask` tool's question waits
    files.py        project file listing, file reads, git status and diff
    agents.py       agent files: frontmatter + prompt + mode; builtin, global, project
    builtin_agents/ orchestrate, build, quick, scout, plan (.md) + NOTICE
    loop.py         the agent loop (no storage; yields MessageAdded)
    codec.py        JSON for messages and events
    export.py       read-only Markdown/JSON exports from one SQLite snapshot
    store.py        SQLite: projects, sessions (tree), messages, turn lifecycle, events
    bus.py          event bus
    eventlog.py     durable event ids, cursors, replay-to-live stream
    recovery.py     startup reconciliation of turns a crash left unfinished
    continuation.py manual continuation from stored context
    retry.py        bounded, visible provider retries
    retention.py    bounded event/lifecycle retention with recovery pins
    runtime.py      projects, sessions, turns, approvals, interrupts, child sessions
    server.py       the HTTP API (OpenCode-style /api/*)
    inspector/      the session inspector page (served at /inspector)
    daemon.py       serve / start / stop / discovery
    client.py       HTTP client (used by the CLI)
    cli.py          local commands + daemon-client commands
  tests/
nexus-search/     Local SearXNG container configuration: Docker and Apple container
nexus-browser/    Default browser-use MCP configuration and setup (upstream server)
nexus-tui/        Ratatui terminal client (Rust): shell, timeline, commands (README)
nexus-nsh/        NSH client (TSX): browser + desktop, same interface (README)
```

Nexus Core runs as a daemon and exposes everything over HTTP (`docs/api.md`).
Every surface is a client of that API: the CLI today, and the TUI and NSH later.
No surface reads Nexus state files.

## Done

| Step | What landed | Read |
| --- | --- | --- |
| 0–2 | Provider wire, normalized conversation types, the agent loop | docs/llm-providers.md, docs/loop.md |
| 3 | codex, grok, copilot, opencode providers; models.dev catalog; `provider/model` refs | docs/llm-providers.md, docs/models.md, docs/auth.md |
| 4 | Daemon, projects, sessions, turns, approvals over HTTP | docs/daemon.md, docs/api.md |
| 5 | Agent files (orchestrate, build, quick, scout, plan) | docs/agents.md |
| 6 | Session trees: child sessions with `launched_by`/`parent_call_id` | docs/daemon.md |
| 7 | Inspector page and the stored event log | docs/inspector.md |
| 8 | The `code` tool on daemon-owned Monty kernels | docs/tools.md |
| 9 | Tool descriptions from `Tool.guidance`, per-session tool switches | docs/tools.md |
| 9a | Background bash jobs (`bash_output`, `bash_kill`) | docs/tools.md |
| 10 | Frozen session snapshot and prompt, `AGENTS.md`, `ask`, approval-free in-directory edits, read-before-edit observations, workspace controls and worktrees, archive, export | docs/daemon.md, docs/api.md |
| 11, 11.1–11.7 | Durable events and cursors, crash recovery, manual continuation, idempotent mutations and revisions, bounded provider retries, `/api/snapshot` resync, retention | docs/api.md, docs/stream-events.md |
| 12.1 | Shared interface data: manifest, capability matrix, reference inventory | interface/README.md |
| 12.3 | TUI shell: connection supervisor, header, sidebars with icon tabs and rail, palette, alerts and terminal notifications, waiting card with `ask` answers, composer with `/` and `@` completion, full timeline, Files tab with search, Git groups and diffs, settings modal, saved client preferences | nexus-tui/README.md |
| 12.5 (web) | NSH web client with the shell, timeline, pickers and export, behind a hardened host proxy | nexus-nsh/README.md |

## Next

Work one reviewed increment at a time. Each increment updates the matching
docs and this plan, runs the focused tests plus the suite and lint, and is
checked in the real app (TUI screenshots through `nexus-tui/tools/webterm`).

### Step 12 — TUI polish (current)

**Design pass (2026-10-28):** [DESIGN_PLAN.md](DESIGN_PLAN.md) holds the decided look for
both clients (the user's picks from the `nexus-design-mockup/` chooser). Increments I0–I4
have landed (chrome band, header tabs, timeline rhythm, Markdown and diffs, the composer in both clients). I6
through I10 remain; see DESIGN_PLAN.md "Progress".

Landed 2026-10-28 (see the Done table): icon tabs in the sidebar, the
effective model everywhere instead of "default model", a context preview in
the new-session view, plain one-line diagnostics, `/` and `@` completion.
Open from that round:

- Check the new look against the old Nexus TUI in a real terminal
  (screenshots in `nexus-tui/tools/webterm`); the user reviews visually.
- The new-session context preview is client-side (agent list, catalog,
  project path). Replace it with Core's real starting context once session
  drafts exist (Step 12a).
- Completion requests go out on every keystroke with no debounce; add one if
  large workspaces make search slow.
- Terminal notifications: verify OSC 777 in kitty (it may need OSC 99) and
  track terminal focus.

The visual target is the old Nexus TUI (screenshots reviewed 2026-10-28).
Landed 2026-10-28 (second round): the composer matches the old look (panel
ending half a row under the controls; the strip `name branch path` with
`worktree` only in a linked worktree); the starting-context section lists
items with estimates, previews and a window share; the turn footer shows
cost; head+tail tool output caps with `o` to show all (and `r`/`o`/`i` now
work on a focused tool); Session info shows effort, turns, calls, tools by
name, tokens, a context bar and cost; the Sessions list puts the main
checkout first; right-click or `m` opens a row menu (sessions and projects);
projects are added (`+ Add project`) and removed (confirmation) from the TUI;
Settings shows what Core reports (default model, catalog, agents, refresh) and
says why the rest cannot change. Still open:

1. **Timeline**
   - `path:line` and OSC 8 links (inline hit targets in Markdown and tool
     output; ratatui cells need an escape-safe way to carry OSC 8).
2. **Files**
   - Syntax highlighting (the library is still undecided).
3. **Settings**
   - Editing the defaults needs Core settings (Step 12a, "Settings over the
     API").
4. **Tests**
   - PTY checks at 80×24 and 160×48 for anchor, follow and jump-to-latest.
   - A review gallery from fixed fixtures (`interface/review.md`).
5. **Session info**
   - Child sessions failed count, enabled/disabled tools and kernels.

### Step 12a — Core work the interface needs

- **Steer and queue:** a `mode` on prompt. Today a prompt while busy returns
  409.
- **Session drafts and the new-session view:** the starting context before
  the first prompt.
- **Search:** an FTS5 index over titles and user messages.
- **One-turn sessions:** the cheap-model title and `/btw`.
- **Handoff instead of compaction:** `continued_from`, plus a context
  threshold setting.
- **File writes:** `PUT` file with `expected_revision`, a `files.changed`
  event, then the built-in editor. Bash enforcement of read-before-edit stays
  open (Known gaps).
- **Attachments, images and durable artifacts:** artifact storage comes
  before web/MCP image results.
- **Settings over the API:** sign-in and sign-out, defaults, usage totals
  per session.
- **Agents:** a `mode` field in `/api/agent/{name}` and an optional `color`
  in agent frontmatter.
- **Approvals:** "allow for session" and "deny with a note" (today only
  allow/deny).
- **Messages and turns:** message paging and load-older-turns. The snapshot
  caps at 200 messages.
- **Warnings:** warn before an agent or model switch that invalidates the
  cache (Principle 8).
- **Transcription:** undecided.

### Step 11a — web, skills, MCP

- `web_search` and `web_fetch` tools against `nexus-search/`. That setup is
  not smoke-tested yet and its image is unpinned.
- Skills: discovery, defaults per session, visible loaded instructions.
- MCP client, with browser-use from `nexus-browser/` as the default. It is
  not wired into the daemon, and browser-use is unpinned and was never
  launched.

### NSH (12.5 rest)

- Mirror the TUI look once it is settled:
  - the composer
  - verb groups and the Working header
  - two-line session rows
  - the files tree with line numbers and copy/mention actions
  - message and code copy
  - reduced motion
  - shimmer
- Pin NSH.
- Build the macOS desktop bundle.
- Files, Git and worktree UI.

### 12.6 — parity, performance, accessibility

- Side-by-side review of the TUI and NSH in light and dark.
- Budgets for large sessions.
- Keyboard-only paths.

### Before 0.10.0 (release gate)

- Run kernels in a scrubbed helper process instead of inside the daemon.
- Back up before migrations, and recover from a failed migration.
- Verify `nexus login copilot|grok|opencode`, and add login tests.
- Define the platform matrix (macOS and Linux) and run it.

## Changes from old Nexus (ledger)

| Old | New | Why |
| --- | --- | --- |
| Old Markdown replaces supported Mermaid source with a rendered diagram | Adapted diagram renderers show the diagram followed by its original fenced source; each fence has its own copy action | Preserve every model-visible item while retaining the old diagram presentation |
| Old Settings builds a modal from its host snapshot and widget kit | Settings is the old centred modal over the dimmed session: one searchable list with section headings, values right-aligned, ←/→ to change, rows from Nexus 0.10's manifest (2026-10-28) | Familiar layout on the new Core and client contracts |
| Old vertical tabs: a session rail with initials and hover details | The left sidebar resizes by its edge and collapses to that rail; Sessions/Files/Projects are icons inside the sidebar (a column on the rail) with labels on hover (2026-10-28) | Old look, without a second tab model beside the sidebar tabs |
| Old TUI composer owns the grapheme editor | The copied editor backs the composer, while its public interface and Core send path remain in Nexus 0.10 | Reuse selection, undo and paste behavior without changing the Core-facing input contract |
| Old TUI shimmer uses RGB theme colours and a host-side activity clock | The adapted TUI shimmer resolves the shared theme tiers and uses the client frame clock, with reduced-motion and monochrome fallbacks | Preserve the old motion while keeping Core-independent rendering and terminal accessibility |
| Worktree is a tool, effectively mandatory | Worktree is a session property set from the control plane | Where work happens is not a task step |
| Task tools and Nexus-state tools mixed in one list | Separate task tools and `nexus` control commands | Different permissions, events and UI |
| Normalized IR only; provider details reconstructed | Each part keeps its `raw` wire item and `origin` | Lossless replay; nothing hidden |
| `~/.nexus` | `~/.nexus10` (until 0.10.0) | Old Nexus keeps working from its checkout |
| `nexus auth codex login` | `nexus login codex` | Sign-in is per provider, one verb |
| Provider ids `github-copilot`, `opencode-go`, `xai` | `copilot`, `opencode`, `grok` | Short names users type |
| Provider subclasses and host-detection branches | Two dialect modules + a quirks table | One place per difference |
| Many vendors (Anthropic, Gemini, NVIDIA, Ollama, agy, Claude agent…) | Only codex, grok, copilot, opencode | Subscription sign-ins only, for now |
| Custom host protocol (commands over a socket) | OpenCode-style HTTP `/api/*` + one SSE stream | Any language can be a client; curl works |
| Workspace is the unit | Project (a path) owns sessions; a session has its own `directory` | Room for worktrees per session |
| `auth/` beside the model layer | `llmhub/auth/` | Sign-in exists only to serve providers |
| Agent definitions mixed into config and code | One Markdown file per agent (frontmatter + prompt) | Readable, editable, per project |
| One general agent that also spawns helpers | `orchestrate` (delegates) vs `build` (does the work); children are `build`, `quick`, `scout`, `plan` | Clear roles; the orchestrator's context stays clean |
| Subagents run inside a tool call | Child sessions with `launched_by`, `parent_id`, `parent_call_id` | A child's work is a full, visible, resumable session |
| A parent could only launch a child and wait for it | The first `session` call exposes `message_session` (once, persisted): a steer for a working child, a follow-up turn for an idle one; the answer returns as a notification (2026-10-28) | Delegation needs steering and follow-ups, not only fire-and-collect |
| Four kernel tools (`code_start`, `code_attach`, `code_run`, `code_reset`), one kernel per agent | One `code` tool; kernels owned by the daemon, shared by id across sessions | Simpler to use; a session tree works in one kernel |
| Cancelling a cell could restore a signed checkpoint | Forced cancellation discards the kernel in v1 | Monty closes the checkout; checkpoint recovery is deferred |
| bash was synchronous with a fixed timeout | No default time limit (optional `timeout`), plus daemon-owned background jobs read with `bash_output`/`bash_kill` | A long command need not block a turn; its result arrives as a notice |
| Live events only; messages in the log | Every session event stored (`event` table) | The inspector can show any past session down to the wire |
| Agent prompts name their tools in prose | `{{tools}}` placeholder filled from the session's actual tools (`Tool.guidance`) | A switched-off tool is never described to the model |
| Fixed list of child agents in the orchestrator's prompt | The `session` guidance and enum are snapshotted from freshly checked agent files at each new session | New sessions pick up agent changes without rewriting existing prompts |
| Agent files read again on every turn | Resolved once into a stored `session_snapshot`; later turns replay it | A cached prefix stays stable; edits cannot silently change an existing session |
| No session instruction file | Exactly `<session.directory>/AGENTS.md`, labelled in the starting context with explicit absence/error/size status | Project instructions are visible to the model and inspector, without ancestor discovery |
| Old sessions have no original prompt record | Lazy reconstruction marked `reconstructed`, with a visible note and a one-time `session.snapshot` event | Honest migration; stable afterwards |
| Worktree tool, effectively mandatory | On-demand workspace controls: activate change-session-directory and worktree list/create/delete when needed (done 2026-10-27) | Keep ordinary sessions small and separate task work from Nexus state |
| A session always ran in its project path | A session can move to any existing directory; the project identity is unchanged and children inherit the move (done 2026-10-27) | Worktrees and sibling checkouts without pretending they are new projects |
| A directory change rewrote context | The move is announced forward, the destination `AGENTS.md` is loaded then, and earlier prompts/messages stay byte-for-byte (done 2026-10-27) | A cached prefix stays stable |
| Worktrees were created and removed by hand or without checks | Git argument arrays, real worktree metadata, dirty/main/in-use refusals and no branch deletion (done 2026-10-27) | Never destroy a checkout, a session's directory or an unrelated directory |
| Sessions had no archive state | `archived` on every session of a root tree; one atomic idempotent transition, default/only/include list filters, and notices that do not wake an archived tree (done 2026-10-27) | Hide finished work without deleting it, stopping jobs or losing context |
| Session history was inspectable but not portable | `GET /api/session/{id}/export` (Markdown transcript or versioned JSON with snapshots, messages and the retained event log), scoped to one session or its descendants, read from one SQLite read boundary (done 2026-10-27) | Take the context out without a provider call, a mutation or a silent truncation |
| The SSE `id:` was the per-daemon `seq`, so every restart invalidated clients | Durable event row ids with a `log` generation and a `<generation>:<id>` cursor; `seq` is only a live counter (done 2026-10-27) | A resume cursor must survive restart and reject a replaced database |
| Only session events were stored; project changes and deletions were live-only | Global lifecycle events and minimal `session.deleted` tombstones are stored and replayed; a session's own history still cascades (done 2026-10-27) | A reconnect must be able to rebuild project and session state |
| A reconnecting client started a fresh live stream and had to catch up out of band | Atomic replay-to-live handoff: subscription and head boundary first, paged replay, overlap skipped, explicit resync codes and overflow (done 2026-10-27) | No lost or duplicated events across the connection boundary |
| A crash left the last in-memory turn permanently busy, with no durable record of what ran | `turn`/`turn_request`/`turn_tool` records; startup recovery marks the turn interrupted, leaves the session idle and reconciles only durable evidence (done 2026-10-27) | Busy state was never durable, so recovery needs its own evidence |
| An interrupted provider stream could be mistaken for a finished reply | Partial output stays diagnostic in the event log; only a response that durably reached `Done` is restored, and a recovery notice labels the rest (done 2026-10-27) | Never present partial text, arguments or reasoning as complete |
| A crashed daemon silently dropped pending approvals and questions | Recovery publishes explicit `permission.replied`/`question.replied` cancellations; late ids cannot resume work (done 2026-10-27) | A vanished wait is not an answer |
| Continuing meant sending a new message ("continue"), which appended model-visible text | An explicit `POST …/continue`, `/continue` and `nexus session continue ID` send the stored context unchanged, reconcile dangling calls from durable evidence, and record continuation provenance (done 2026-10-27) | The model receives exactly the context the user sees, with no invented user turn |
| Compaction rewrites the session's history | A handoff to a new root session linked by `continued_from` (planned, Step 12a) | What the user sees stays what the model saw |
| System prompt rendered per turn | Rendered once at session creation and replayed unchanged | A stable cached prefix |
| Workspace edits require approval unless allowed | In-directory `edit`/`write` (patches and nested calls too) are auto-approved with no setting; bash approval is unchanged (done 2026-10-27) | Editing authorization and command execution are separate |
| Read before edit was prompt advice | Per-session observations persist hash, ranges and clipping; local changes need affected-line coverage and whole-file operations need complete same-revision coverage (done 2026-10-27) | Stale or unseen content cannot be changed through the file tools |
| No mutation serialization for files | One per-daemon lock, revision recheck before write, whole-patch validation before any write (done 2026-10-27) | Nexus writers cannot interleave a check and a write |
| Bash can write files with no tool checks | Documented enforcement boundary; full sandbox/restricted execution remains open (PLAN.md, Known gaps) | Do not claim a filesystem invariant the tool contract cannot guarantee |
| Web tools named `websearch` / `webfetch`, deployment in `websearch/` | `web_search` / `web_fetch`, deployment in `nexus-search/` (planned, Step 11a) | Explicit names and self-contained service setup |
| Extension system and MCP integrations in old Core | Focused skills/MCP support and upstream browser-use default in `nexus-browser/` (planned, Step 11a) | Carry over needed capabilities deliberately |
| A lost mutation response was ambiguous and a retry could create a second turn | `Idempotency-Key` mutation journal; prompt/continue acceptance and turn are one transaction; a replay returns the original result and schedules nothing (done 2026-10-27) | Clients can resolve a lost response without duplicating work |
| Competing updates silently overwrote each other | Monotonic project/session `revision`, `If-Match`/`expected_revision`, and 409 `revision_conflict` before any change (done 2026-10-27) | Conflicts are deterministic and visible |
| A transient provider failure ended the turn immediately | Bounded, visible pre-stream retries with attempt rows and events; no tool/partial replay (done 2026-10-27) | Survive transient faults without hiding attempts |
| A reconnecting client had to catch up out of band | `/api/snapshot` watermark plus `Client.watch` reconnect/deduplicate/resync; CLI cursor advances only after applying (done 2026-10-27) | Rebuilding state is consistent and cannot resubmit a prompt |
| The event log grew forever and the floor stayed 0 | Prefix retention with a durable head high-water mark, recovery/replay pins and an explicit `history_unavailable` boundary (done 2026-10-27) | Storage is bounded without losing recovery evidence |
| Separate TUI and NSH designs | One interface spec for both (INTERFACE_PLAN.md) | Same features, same names |
| Old host `search_files` over the custom protocol, with read-deny roots from host policy | Core `GET /api/project/{id}/files/search` (`q`, `limit`, `directories`, `session`): the same bounded, Git-aware, ranked walk, with no read-deny roots (done 2026-10-28) | Clients complete `@` paths over HTTP. nexus10 has no read-deny policy, so the search takes no deny roots and reads only the selected root |
| Old composer strip `name branch path worktree`, with `main checkout` for a main checkout | Same order, but a main checkout says nothing; `worktree` (violet) appears only in a linked worktree. The panel ends half a row below the control row (2026-10-28) | Show only what is unusual; match the old look |
| Old counts `1.2K` | Counts are `1.2K`/`3.4M` everywhere in both clients (2026-10-28) | One format, as the old composer showed it |
| Old context header: headings with token figures, previews and a total | Same, as a rail list with a right-aligned token column, detail rows and a window share; estimates are characters ÷ 4 and marked `~` (2026-10-28) | The context is visible and costed before the first request |
| Turn footer without price | Footer adds cost from the catalog's prices when the model has them (2026-10-28) | Spec: every turn shows what it cost |
| Output clipped to the first 200 lines, "show all" did nothing | A tool's detail is the old box (`┌ │ ╰`): the command's first 2 rows and the last 8 output rows with `↑ N earlier lines · enter show all` between (diffs and the raw view keep a head and a tail); `o` or Enter on the open tool toggles the whole output; `r`, `o`, `i` work on a focused tool (2026-10-28, restyled by DESIGN_PLAN D06) | Clipping is announced and reversible; the model's own clip stays separate |
| Projects were added with the CLI only | `+ Add project` and a confirmed remove in the Projects tab; right-click or `m` opens a row menu of the manifest's `row_menu` commands (2026-10-28) | Core already serves `POST`/`DELETE /api/project` |
| Turn footer was a rule plus a left-aligned row; context items were a rail list | Footer is right-aligned `▪ Build · model · …` with no rule; context items are chip headings with indented details, as in old Nexus (2026-10-28). The footer carries no end-status word or glyph: an interrupted turn adds `/continue` (`enter continue` with an empty composer), a failed turn puts its first error line on the row above (DESIGN_PLAN D05) | The user asked for the old look, then for no end status |
| Old user card: padding rows and a left gutter; old sticky user card | The user message is a full-page-width band on the raised surface (rail `▌` at the edge, fold marker inside, `#n · time` at the right); the sticky header from old Nexus `sticky.rs` is kept: at most 3 rows, a `░` shade row with `↑ N lines of this turn above`, pushed up by the next band, click to scroll back (DESIGN_PLAN D04) | The user chose the band; the pin keeps the question in view |
| Activity groups: `<1 s`, `Ran 1 command, Read 1 file`, groups without verb glyphs | Durations `0.4s 4.2s 1m 05s 1h 02m`; summaries in first-seen order (`Read 2 files, ran 1 command`) with `+N −M` and `✕ N failed`; live `Working` with the newest 5 members and `… N earlier`; verb glyphs `→ ← $ * λ ? ↳ ↗` (`[glyphs.verb]` in theme.toml); Thought rows `▸ Thought for 2.1s · preview` (DESIGN_PLAN D06, D07) | Old Nexus's groups, polished |
| Header showed the title and breadcrumbs | Session tabs as in old Nexus: opened sessions with status glyph, `×`, `+`, `alt+x`/`alt+[`/`alt+]`; clicking the viewed tab renames (2026-10-28) | The user asked for tabs |
| Composer had a half-row edge and `used · tier% · window%` on the control row | Soft card with half-row edges, no rail or status band, the old `╱` meter and `82.0K · 41%` on the control row, `ctrl+p commands` on the project strip, completion as wide as the card (2026-10-28, DESIGN_PLAN D11, user choice; replaces the rail and status band of `5409281`) | The user chose the soft card over the rail |
| Activity groups were collapsed or live-open only | Three detail levels (low/medium/high, `alt+d`, Settings, persisted), medium by default (2026-10-28) | Spec: scan, then drill down |
| Agent/model/effort pickers did nothing before a session existed | They set a draft sent with the session's creation request (2026-10-28) | `/model` and `/agent` must work on the new-session screen |
| Composer completion was host-driven (the host ranked `/` commands and searched `@` files) | The TUI ranks the manifest's slash commands locally (prefix, subsequence, typo, then the rest; always ten rows) and asks Core's `GET /api/project/{id}/files/search` for `@` paths, padded from an empty-query listing; replies are cached per token and a reply for an old token is dropped | Completion needs only the Core HTTP API, and the popup keeps its height while typing |
| Old modal backdrop: cells under a modal repainted in muted text; boxed panels with titles in the border | The TUI blends every cell of the frame 50% toward `surface.canvas` (`paint::scrim`; `DIM` where colours cannot be blended) and draws one borderless `surface.raised` block (2 cells of padding left and right, 1 row top and bottom, title and `esc` on the first row, key hints on the last, within 2 cells of the frame edge and at most 70% of its height) for the palette, pickers, Settings, confirms, help and notifications; Settings has UPPERCASE muted headings and the selection band instead of the accent fill, and `[ Cancel ]` is the selected default (DESIGN_PLAN D13, I7) | One modal recipe in both clients; the surface step and the scrim separate the modal from the session |

## Known gaps

- Kernels have no checkpoint recovery; a timeout, interrupt, crash or resource
  limit discards the kernel. Startup recovery reports the loss for a reconciled
  turn and preserves nested evidence, but restores no variables or cells. Monty
  workers run inside the daemon with its environment (a scrubbed helper process
  is planned before 0.10.0). Linux, a forced worker crash and cold-start latency
  are unverified.
- Background bash jobs live in the daemon's memory. A normal shutdown kills
  them; a daemon crash can leave the command running with no job record, and
  retained output is lost. Recovery reports an unfinished `bash.started` when it
  reconciles that session's turn, but a job that outlives an orderly turn in a
  session with no interrupted turn is not reported individually. Recovery never
  kills a process from a stored pid (it may have been reused); there is no job
  supervisor.
- Approval cancellation now publishes `permission.replied` with an explicit
  outcome during normal teardown, cell cancellation and startup recovery.
  Questions and approvals remain distinct waits; none survives a restart.
- Startup recovery closes an interrupted outer `code` call with an unknown
  outcome, preserving the durable `code.call` and `code.cell` events and saying
  the kernel was lost. It does not invent a successful call or recover kernel
  state.
- A provider retry can duplicate provider-side computation/billing; it is
  bounded and visible, not provider-side exactly-once. An ambiguous external Git
  effect (worktree create/delete) is persisted only as intent/evidence and must
  be inspected, never blindly repeated. Manual continuation is an explicit
  client action and never repeats an ambiguous POST. There is no context limit
  or compaction (planned as handoff, Step 12a below).
- Explicit updates apply to subsequent model/tool calls. An active `code` cell
  retains the task-tool bindings it started with until that outer call finishes.
  Agent switches retain the initially loaded `AGENTS.md` section; a directory
  change loads the destination's instructions as forward context instead of
  rewriting the frozen prompt.
- A client-requested directory move that is waiting for a running call lives in
  memory: a daemon crash before the next boundary loses it. Worktree deletion
  checks session directories and pending moves, not arbitrary `bash` or external
  processes using the worktree. There is no merge/integrate or branch-cleanup
  operation and no worktree permission setting.
- Archive transitions are stored in the root session's event log only; a client
  subscribed solely to a descendant stream does not see the root's transition.
  Archive has no bulk/project operation, timestamp or worktree cleanup (by
  design archiving never deletes one), and background jobs keep running.
- A session export holds one SQLite read snapshot (WAL) while streaming, so a
  very large export delays checkpointing until it finishes; writers are not
  blocked. Markdown omits wire events, and the JSON document (with every event)
  can be much larger than the messages alone. Export has no attachments or
  artifacts to include yet and does not re-clip output.
- Sessions from before snapshots existed are reconstructed once and marked
  `reconstructed`; the note is visible, but the original prompt is unknowable.
- Reconnect resumes the event stream; startup recovery reconciles unfinished
  turns before the daemon serves requests and publishes their idle status,
  cancellations and `turn.recovered`. Manual continuation after recovery is an
  explicit request. Keyed mutations replay safely and clients resync from
  `/api/snapshot` on a cursor failure or overflow. Archive transitions are still
  stored only in the root's log.
- Event, turn lifecycle and mutation retention is bounded: a contiguous event
  prefix expires by age/payload, while unfinished turns, open waits, live jobs
  and in-flight replays pin their evidence. Messages, snapshots and model context
  are independent of diagnostic expiry. A cursor below the floor gets
  `history_unavailable` with a resync head. Expired keys are no longer safe to
  retry automatically. A very large export still holds one WAL read snapshot
  while streaming.
- Sign-in is only the local `nexus login`, not yet an API endpoint.
- Nothing checks that a running daemon runs the current code: `nexus daemon stop` after changes.
- No cross-provider message conversion beyond text and tool calls; switching
  provider mid-session works (`/model`), but foreign reasoning is dropped.
- Copilot GPT models are sent over Chat Completions; whether some of them
  require Responses is not checked.
- No images in either dialect yet.
- Tool paths are confined to the workspace, but `bash` is not sandboxed, so
  "read-only" (`scout`) and "Markdown only" (`plan`) rest on their prompts.
  Read-before-edit is enforced only for the `edit`/`write` tools: unrestricted
  bash can write any file without an observation. There is also no in-process
  control over external writers, so a file can change between the final hash
  check and the write. Full enforcement needs a sandbox or restricted
  write-capable command execution before the invariant can be guaranteed.
- File observations persist per session and have no API view or pruning yet;
  they are removed with their session and replaced when a newer revision is
  read. An interrupted read that never produced a tool result is dropped.
- Child sessions launched with `wait: false` are tested offline only.
- `instructions` uses the Responses field. The current Codex CLI sends a
  developer message instead. Both work today; revisit if the backend changes.

## Open questions

- Syntax highlighting library for the TUI file view.
- Dictation engine per platform.
- Focus ring colour: shared with the accent or a separate cool hue.
- Worktree cleanup on archive/delete, and leaving a worktree.
- Whether a user can steer a child session from the parent's card.
- Output caps (lines vs bytes) and whether they are a setting.
