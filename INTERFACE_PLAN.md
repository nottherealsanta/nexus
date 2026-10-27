# Nexus interface plan (TUI and NSH)

One interface, two clients: the Ratatui TUI (`nexus-tui/`) and NSH
(`nexus-nsh/`: browser, desktop, iOS). They are functionally the same. Both
speak only the Core HTTP API (`docs/api.md`) and differ only where the medium
forces it (mouse vs keys, pixels vs cells). This file is the spec for both.
Core work it depends on is tracked in [PLAN.md](PLAN.md) (Step 12 and the
steps before it).

Status: planning. Nothing here is built.

## Principles

1. **One timeline.** What the user sees and what the agent sees are the same
   context line as closely as possible. Every item the model receives (system prompt, user messages,
   tool results, notices, context insertions, handoffs) appears in the session
   view, in order, labelled. A child launch is in the parent's context line;
   clicking it opens the child's own context timeline. Activity indicators may
   show that child running and stop when it finishes without inserting model
   context. Label inspection details separately from model-visible content.
   Folding and collapsing change only how much is shown, never what exists.
2. **Context first.** Presenting context well is a core idea of Nexus: a new
   session shows its full starting context, every turn shows what it cost, and
   every clip is announced.
3. **Same features, same names.** A feature, command or setting exists in both
   clients with the same name, the same id and the same behavior. Where one
   medium cannot do something (inline images in some terminals), it falls back
   and says so.
4. **Append-only context.** The UI never offers an action that rewrites
   context already sent to a provider (it is cached, and editing it would
   invalidate the cache). Changes go forward as new items. Actions that will
   invalidate the cache anyway (switching model or agent) warn first.
5. **Core decides, clients render.** State lives in Core. Clients hold only
   view state (open panels, scroll, drafts) and resync from Core at any time.

## Layout

```
┌──────────────┬─────────────────────────────────────┬──────────────┐
│ Left sidebar │ Session view (timeline)             │ Right sidebar│
│  Sessions    │  breadcrumbs (child sessions only)  │  Session info│
│  Files       │  turns, tools, children, notices    │  Git status  │
│  Projects    │                                     │              │
│              ├─────────────────────────────────────┤              │
│              │ Waiting-for-input card (if any)     │              │
│              │ Composer                            │              │
└──────────────┴─────────────────────────────────────┴──────────────┘
```

- Both sidebars can be hidden (keys and palette commands). Narrow widths
  (phone, small terminal) show one pane at a time: sidebars become drawers or
  overlays.
- The centre column is the session view (or the new-session view, or an open
  file in the editor).
- Settings is its own view, opened from the palette or a fixed entry in the
  left sidebar.

## Focus and scrolling

Both clients support keyboard-only navigation with visible focus, a predictable
focus order and focus restoration after closing drawers, child views or dialogs.
Streaming follows the latest item only while the user is already at the bottom.
Scrolling upward suspends following; incoming text, folding and loading older
turns preserve the visible anchor. A **jump to latest** action restores following
and indicates new content below. Test these behaviors at narrow widths too.

## Left sidebar

Three icon tabs representing **Sessions**, **Files** and **Projects**. Hovering
an icon shows its word label: "Sessions", "Files" or "Projects". Keyboard focus
also exposes the label; retain accessible names on all three icons.

### Sessions

- **Scope toggle:** current project only, or all projects. With all projects,
  sessions are grouped under their project.
- **Grouped by worktree** when a project has worktrees: the main checkout first,
  then one group per worktree (branch name, path on hover or detail line).
- Each row: title (or "Untitled" until the title arrives), status (idle, busy,
  waiting for input, error), relative time of last activity, and the number of
  child sessions. Child sessions are not listed as rows; they are reached from
  their parent (see Child sessions). A session continued by a handoff shows as
  a chain (see Compaction).
- One-turn sessions (titles, `/btw`) are hidden here; they are reachable from
  their parent and the inspector.
- **Archived** sessions are hidden behind an "Archived" filter.
- **Search** (palette or a field at the top) matches session titles and user
  messages only, never tool output or assistant text. Core keeps an index of
  exactly these fields, so search is one indexed query, not a scan.
- Actions on a row: open, rename, archive or unarchive, delete, copy id, open
  in inspector.

### Session actions and where they live

Every session action is a palette command. The session row menu carries the
common ones.

| Action | Palette | Row menu | Notes |
| --- | --- | --- | --- |
| Rename | yes | yes | Also by clicking the title in the header |
| Archive / unarchive | yes | yes | |
| Delete | yes | yes | Confirm; whole subtree |
| Export | yes | yes | Markdown transcript or JSON (messages and events) |
| Handoff | yes | | Same as `/compact` (see Compaction) |
| Change session directory | yes | | Move to an existing path |
| Worktrees | yes | | List, create or delete; creating does not move the session |
| Integrate / merge a worktree | yes | | TBD with leaving a worktree |
| Interrupt | yes | | Esc |
| Continue | yes | | `/continue`: send existing context after interruption/failure |

No fork or revert: a session only moves forward. Export and integrate need
Core work (PLAN.md Step 10).

### Archive and delete

- **Archive** hides a session tree from the default list. Nothing is removed;
  it can be unarchived and resumed. Archiving a busy session is refused until
  it is idle or interrupted.
- **Delete** removes the session, its whole subtree, its one-turn sessions and
  its stored events. It asks for confirmation through the unified alert
  (naming how many child sessions go with it). There is no undo.
- Worktree cleanup when a session in a worktree is archived or deleted: TBD.

### Files

- The current project's files as a tree: folders expand and collapse, loaded
  lazily one directory at a time.
- Not specific to a session: it always shows the project (the main checkout,
  or the worktree of the open session; a selector when there are several).
- Git status markers per file (modified, added, untracked, deleted, ignored
  shown dimmed or hidden by a toggle).
- Opening a file shows it in the built-in editor in the centre column.
- Actions: open in the built-in editor, open in your editor (`$EDITOR` or the
  OS default), copy path, insert as `@path` into the composer.

### Projects

- Every project Core knows: name, path, session count, last activity.
- Select to make it current (the Sessions and Files tabs follow). Add a
  project by path; remove a project from the list (its sessions go with it,
  after a confirmation).

### Built-in editor

A simple editor, not an IDE: open, edit, save, find, syntax highlighting,
line numbers and a dirty marker. Saves are refused if the file changed since
it was read; reload before saving, preserving unsaved text for recovery.

- TUI: [ratatui-code-editor](https://github.com/vipmax/ratatui-code-editor).
- NSH: the [NSH editor element](https://nsh.dev/elements/editor/).
- Reads and writes go through Core (`GET`/`PUT` file endpoints), confined to
  the project, as for the tools.

## Right sidebar

For now it holds only two sections.

### Session info

- Title, id (copyable), agent, model, effort, directory
  (with worktree branch if any).
- Started at, last activity, total duration of turns.
- Turns, model calls (steps), tool calls (by tool, e.g. `bash 12 · edit 4`).
- Tokens: input, cached input, output, reasoning; context used against the
  model's context window (a bar); cost when the price is known.
- Child sessions launched from here: count, how many running, how many failed.
- Enabled and disabled tools.
- Kernels this session tree uses.

Not in the right sidebar for now: Changes (files the session modified, with
diffs), Jobs, Logs, and Cost across sessions and providers. Revisit
after the first release; Cost may go to Settings.

### Git status

Of the session's directory (the worktree, if the session is in one): branch,
ahead/behind, changed files with `+N −M`, staged vs unstaged. Clicking a file
opens its diff.

## New session view

A new session does not start empty. It opens on its **starting context**, so
the user sees and can change it before the first message:

- Agent (selector), model and effort, directory or worktree.
- System prompt: the agent's rendered prompt, as the model will get it.
- Instructions: `AGENTS.md` from the session's current directory, shown with
  its path. Directory changes load the destination's file as new context.
- Agent definitions are checked for changes whenever a root or child session
  is created. The session snapshots its agent and available tool descriptions;
  later turns do not reload edited agent files. Unavailable tools are omitted
  from its starting system prompt and schemas.
- Tools: each with its guidance and schema, each with a switch.
- Skills and MCP servers (PLAN.md Step 11a): listed with switches, source
  paths and load/connection status. Browser-use is the default MCP server.
- Token count of the whole starting context.

Edits here (switches, agent, model, a prompt override) change the session
before anything is sent. Once the first message is sent, this context is
frozen and shown at the top of the timeline as the first item, collapsed. The
composer is at the bottom as usual; sending starts the session.

Core: a session draft (`POST /api/session` with `draft: true`, or a preview
endpoint) that returns the starting context as labelled sections and their
token counts, and accepts the edits above.

## Session view (timeline)

### Turns

- A **turn** is one user message and everything until the agent stops. Turns are
  numbered (`#1`, `#2`…), and the number is a jump target (palette:
  "go to turn").
- Inside a turn: assistant text, reasoning, tool calls and results, child
  session cards, notices, in order.
- **Folding.** Any turn folds to its header line: number, first line of the
  user message, tools used, duration, status. "Fold all but last" is a
  command. Folding is view state only.
- **Turn footer** at the end of each turn: model, effort, duration, model calls,
  tool calls, tokens (input / cached / output / reasoning), cost, and how the
  turn ended (done, interrupted, error, step limit).
- **Reasoning** is shown collapsed ("Thought for 12 s"), expandable to the
  summary text. Encrypted reasoning is labelled as such (it exists, it is
  replayed, it is not readable).
- **Notices and insertions** (child finished, session directory changed,
  clipping, steering messages, context insertions from settings) are timeline
  items with their own style, because the model sees them too.
- **Errors and retries** (provider, auth, interrupted) are timeline items with
  the error text and, for provider errors, the status and body. Each retry
  (when Core retries) is a row with attempt number and wait time.
- **Handoff markers** (see Compaction), **directory-change rows** (session moves: old
  and new directory and branch), **hook rows** and **diagnostic rows** (when
  Core has hooks or diagnostics feeding context) are their own row styles. If
  they reach the model, they are in the timeline; if they do not, they are
  not (they go to Logs in the inspector).

### User messages

- Markdown as typed, with attachments as chips.
- **Large text** (a paste over a threshold, e.g. 40 lines or 4 KB) is shown
  collapsed to its first lines with "N more lines", expandable. It is sent
  whole.
- **Images** show as thumbnails (NSH inline; TUI inline when the terminal
  supports the kitty, iTerm2 or sixel protocol, otherwise a chip with name,
  size and dimensions that opens externally).
- Queued and steering messages are marked as such (see Composer).

### Markdown rendering

Assistant text is CommonMark plus GFM: headings, emphasis, lists, task lists,
tables, block quotes, links, inline code, fenced code with syntax highlighting
and a language label, and horizontal rules.

- Streaming renders incrementally; an unfinished fence or table is rendered as
  plain text until it closes, so the layout does not jump.
- TUI: tables fit the width and wrap cells; links show as underlined text with
  OSC 8 hyperlinks where supported; headings use weight and colour, not size.
- File paths like `src/x.py:42` are links that open the file in the editor at
  that line.
- Code blocks have a copy button (NSH) or copy key (TUI).

### Copy

One click (or one key on the focused item) copies: a user message, the agent's
final message of a turn, any assistant text block, a code block, a tool's
input or output, a session id, a file path. A short confirmation appears in
the unified alert area. NSH writes to the system clipboard; the TUI uses OSC 52
(so it works over SSH), with a fallback to the platform clipboard command.

## Tool rendering

Every tool call is **collapsed by default** to one line, and expands on click
(or Enter) to a tool-specific detail. A tool's line updates live while it
streams its arguments and runs.

**Collapsed line:** status icon (running, done, error, denied, waiting), tool
name, the key argument, a short result summary, and duration.

| Tool | Key argument | Summary on the collapsed line |
| --- | --- | --- |
| `read` | path and line range | lines read |
| `bash` | command (first line) | exit code, output lines |
| `edit` / patch | path (or file count for a patch) | `+N −M` in green and red |
| `write` | path | `+N` (new) or `+N −M` (overwrite) |
| `code` | first line of the cell | nested calls, `Out` type, kernel id |
| `session` | agent and title | child status (see Child sessions) |
| `web_search` | query | result count and source links |
| `web_fetch` | URL | status, returned lines and clip notice |
| MCP tool | server and tool name | status and result summary |
| Skill loading | skill name | source and loaded instructions |
| `ask` | first question | the answer, once given |
| `expose_workspace_tools` | activate workspace controls | directory and worktree tools available |
| `change_session_directory` | destination path | the new directory |
| `worktree` | list, create or delete | worktree paths or operation result |

**Expanded detail:**

- **edit / patch:** the diff, unified, with line numbers and syntax highlighting.
  NSH offers a split view when wide. A patch shows one collapsible diff per
  file. A failed edit shows the error and the attempted change.
- **write:** the diff against the previous file, or the new content when the
  file is new.
- **read:** the content that was read, with its line numbers and range, exactly
  as the model got it (including any clip notice).
- **bash:** the command (full), its working directory, then the output in a
  scrollable box with a fixed maximum height, then the exit code and duration.
  While running, output streams into the box.
- **code:** the cell source, stdout, `Out [n]`, and the nested tool calls as
  nested collapsed tool lines, each expandable like a direct call.
- **ask:** the questions, options and the user's answers.
- **Errors and denials:** the error text in the error colour; a denial says
  who denied it.

**Large output.** The box is scrollable and capped. The client shows at most a
fixed amount (for example the first 200 and last 200 lines, or 64 KB), with
"N lines hidden · show all" between. Show all fetches the full result from
Core. The model-side clip (what the model actually got) is a separate notice,
so the user can tell "the UI hides this" from "the model never saw this".

**Arguments and raw.** Every expanded tool has a "raw" toggle that shows the
exact arguments JSON and the result text as sent to the model.

## Child sessions

**In the parent's timeline**, a child is a card at the point of its `session`
tool call:

- Agent, title, status (running, waiting for input, done, failed,
  interrupted), whether the parent waits for it, elapsed time.
- While running: the child's current activity (last tool line or a streaming
  line of text), its tool count and tokens.
- When done: the first lines of its answer, expandable to the whole answer
  (which is what the parent received).
- A non-waiting child's later "finished" notice appears in the parent's
  timeline where the parent received it, linked to the card.
- Waiting-for-input inside a child bubbles up: the card shows it and the
  parent's waiting area shows it too, labelled with the child.

**Opening a child** (click the card) opens the child session in the centre
column. It looks exactly like a parent session view, with two differences at
the top: **breadcrumbs** (`root title › child title`, each crumb clickable)
and a **back button** (and Esc / Backspace) to return to the parent at the
card's position. A child's composer can prompt the child directly.

## Waiting for user input

In Core, approvals (`permission.asked`) and questions (the `ask` tool) are
separate things. In the interface they are **one idea: the agent is waiting for
the user's input.** They share one component, one place and one set of keys.

- **Where:** a card docked above the composer, and a marker on the tool line
  in the timeline. The session row in the sidebar shows "waiting". If several
  sessions (or children) are waiting, the card shows a count and cycles.
- **Approval:** the full parameters (not a summary), with a diff preview for
  edit, patch and write and the full command and working directory for bash.
  Four choices: allow once, allow for this session, deny, deny with a note to
  the agent. A choice Core does not offer for this call (for example "allow
  for this session" on a tool that is not session-grantable) is shown
  disabled with the reason, never hidden.
- **Question:** one or more questions, each with options (single or
  multi-select) plus free text, and an optional preview per option. Answer
  all, then submit.
- **No timeouts.** A question waits until it is answered or the turn is
  interrupted.
- **Cancelled.** If the turn is interrupted (by the user, a parent, or a daemon
  restart) while waiting, the card closes and the timeline keeps the question
  or approval with "cancelled: interrupted", and the tool result the model
  got ("interrupted by the user"). Answers typed but not submitted are kept
  as a draft only for this view.
- **Attention from elsewhere.** A wait in another session or project: the
  session row and its project get an attention mark, a banner offers "go to",
  the notifications drawer lists it, and the OS notification fires if the
  window is not focused (see Notifications). A wait inside a child shows on
  the child's card and in the parent's waiting area.

Core: the `ask` task tool and `question.asked` / `question.replied` contract
are implemented in PLAN.md Step 10, before workspace controls. This interface renders
those questions alongside the existing permission events.

## Composer

- Multi-line Markdown input. Enter sends, Shift+Enter (or Alt+Enter in
  terminals that cannot tell) adds a line.
- **Controls in the composer:** agent, model and effort selectors, usable at
  any time, including mid-turn. A change applies from the next model call.
  Already-running calls keep their original configuration. Tool/MCP changes
  affect only subsequent calls too; directory changes apply after active tools
  finish. Show when a requested change is pending and when it takes effect.
  Switching shows a unified alert first: the prompt cache may be invalidated
  (a different model or provider has no cache for this prefix; a different
  agent changes the system prompt). The change is recorded as a timeline row
  at the point it took effect, and the turn footer names every model used.
- **Attachments:** durable Core storage with explicit upload and total size
  limits and visible oversize errors; a button and drag-and-drop for files and images (NSH), a
  paste of an image (NSH, and TUI where the terminal passes it), and `@path`
  mentions with completion from the project's files (both). Attachments show
  as removable chips with size. Large pastes become a collapsed chip.
- **Voice dictation (important).** A mic button (NSH) or push-to-talk key (TUI)
  records speech and transcribes it into the composer as editable text. It
  never sends by itself. There is no live voice conversation with the model
  (no GPT Live call bar) in the composer. Options for transcription (decide in the step that
  builds it): the platform's speech recognizer (Apple Speech on macOS/iOS, the
  browser's API), a local model, or a provider transcription endpoint through
  Core. The TUI records in its own process.
- **While the agent is working**, the composer stays usable and offers two
  ways to send:
  - **Steer** (default while busy, e.g. Enter): the message is added to the
    running turn at the next model call, after the current tool results, so
    the agent sees it before its next step. Shown in the timeline at the point
    it was inserted.
  - **Queue** (e.g. Alt+Enter or a toggle): the message waits and is sent as
    the next turn when this one ends. Queued messages are listed above the
    composer, editable and removable until sent.
- **Interrupt:** Esc (TUI) or a stop button.
- **Drafts** are kept per session (view state).
- `/` at the start opens command completion (see Commands).

Core: steering and queueing endpoints (today a prompt while busy gets 409).

## Continue after interruption

`/continue` resumes from the existing session context without inserting a new
user message. Offer it after an interrupted or failed turn, once Core has
reconciled any incomplete calls; disable it while a turn is running. Show the
continuation linked to the interrupted attempt. Previously completed tools are
not rerun. Unknown tool outcomes and any partial output that cannot be replayed
are labelled visibly. Core owns this recovery contract (PLAN.md Step 11).

## One-turn sessions

A one-turn session runs exactly one model call and ends. It is a real session
(stored, inspectable, linked to its parent with a kind), hidden from the main
list.

- **Title.** After the first user message, Core runs a one-turn session on a
  cheap model with that message to produce the title. The title appears in the
  sidebar and header when it arrives.
- **`/btw <question>`.** While the agent is working (or idle), ask a side
  question without disturbing it. Core sends the current session's context up
  to its last completed tool call, plus the question, as one call with no
  tools. Because the prefix is the same, it reuses the parent's prompt cache.
  The answer appears in an overlay or side panel tied to the parent, not in
  the parent's timeline, and the parent's context does not change. Each btw
  can be reopened from a "btw" list in the session info.

## Compaction is a handoff

At a configurable percentage of the model's context window (default **80%**),
or when the user asks, the session **hands off** to a new root session of the
**same agent**. Settings accepts a numeric percentage rather than fixed presets.
Core reserves room for the summary and handles failed handoffs visibly:

- The old session ends with a handoff turn: the agent writes the summary (the
  state of the work, open threads, files touched), visible in its timeline.
- A new root session starts with that summary as its starting context, plus a
  link to the old one. Its new-session view shows exactly what it carries.
- In the sidebar the two show as one chain (newest on top, "continued from").
  The timeline of the new session starts with a "continued from …" item that
  opens the old one.

So nothing the model saw is ever hidden or replaced: each session's timeline
is still exactly what its model saw.

## Session directory and worktrees

Core exposes an always-available `expose_workspace_tools` entry point.
When the agent requests it, its result adds the definitions of two tools to
context and makes them callable from the next model call:

- `change_session_directory`: move this session to an existing directory.
- `worktree`: list, create or delete worktrees. Creating returns a path;
  a separate directory-change call moves the session there.

Activation is shown in the context line and persisted per session. It requires
no user permission and introduces no worktree settings. Each new child starts
with the entry point and activates independently. User commands can invoke the
same control operations directly through Core.

A directory change shows old/new paths and branch, loads the destination's
`AGENTS.md` as forward context, updates Files/git/header, and moves the session
to its worktree group when appropriate. Running children retain their paths.
Worktree deletion refuses dirty or in-use paths; leaving a worktree means first
changing directory. Integrate/merge remains future work.

Edits inside the session directory require no approval and have no permission
setting. Core still enforces path confinement and a fresh model-visible read
before changing an existing file. A refusal explains which file must be reread.

## Commands and the palette

- **Command palette:** Cmd+P (macOS NSH), Ctrl+P (TUI and others). Almost every
  action is a palette command: new session, switch session/project, search,
  archive, delete, fold all, go to turn, toggle sidebars, change model, effort,
  agent, tools, open settings, theme, sign in, open
  inspector, copy actions, `btw`, compact/handoff, interrupt.
- **Slash commands** in the composer are the same commands (`/model`,
  `/effort`, `/agent`, `/btw`, `/tools`, `/compact`, `/new`, …), with
  completion. A command has one id, title, optional slash name, keybinding and
  "available when" rule, defined once and shared by both clients (a single
  command list file in the repo that both build from).
- Keybindings are listed in the palette next to each command and can be
  changed in settings.

## Unified alerts and notifications

One system for telling the user things, in both clients, with levels:

- **Toast:** brief confirmations (copied, saved, title set).
- **Banner:** lasting state (daemon disconnected, reconnecting, a provider
  needs sign-in, version warning).
- **Confirm:** before an action with a cost or no undo (delete, switching
  model/agent mid-session and cache invalidation, removing a project).
- **Blocking:** a breaking client/Core version mismatch.

Timeline notices are not alerts: they are context, and the model sees them.

**Notifications drawer.** Every toast, banner and attention event is also
kept in a drawer (palette: "notifications"), newest first, with a link to the
session. Unread items show a count.

**Attention marks.** A session row, its project, and (NSH) the window or tab
title get a mark for: waiting for input (strongest), finished while not in
view (unread), error. Opening the session clears it.

**Sound and OS notifications.** A completion sound (on/off) when a turn ends out
of view; OS notifications on desktop and iOS, the terminal bell or OSC 9/777
notification in the TUI. Each kind is switchable in Settings.

## Connection to Core

- **Handshake.** On connect, the client reads `GET /api/info` (Core version and
  API version). Same API major: go. Newer minor: carry on, banner if the client
  lacks features. Different major: **halt** with a blocking alert that says
  which side to update. No requests are sent past a breaking mismatch.
- **Starting the daemon.** If none is running, the client starts it (as the CLI
  does), or tells the user how.
- **Reconnect.** When the event stream drops, the client shows a banner and
  retries with backoff. On reconnect it catches up from the last event id
  (stored log, `?after=`), then resyncs the open session and the session list
  from the API using a snapshot watermark. Catch-up transitions atomically to
  live events, with stable ids for deduplication. Cursors survive restarts or
  identify a new log generation. Overflow, an expired cursor or an explicit
  continuity failure triggers full resync. A gap in global `seq` on a filtered
  stream is normal and does not by itself trigger resync. Restore pending waits
  and queued messages too. Retried sends use request ids to avoid duplicate
  turns; competing clients receive explicit revision/conflict responses.
- **Daemon restart.** A new daemon (different pid or token) means: reread the
  token, re-handshake, full resync. Turns that were running when the daemon
  died show their partial state and "interrupted by daemon restart".
- **Client access.** Nexus is served on its configured port. Any client that
  can reach that port can use the Core API under the normal API access rules;
  there is no device-registration workflow.

## Loading and empty states

- **Daemon starting:** a "starting Nexus Core" screen with the log tail if it
  takes long or fails.
- **Daemon restarting / connection lost:** a banner, the last view stays
  visible but read-only, then a resync.
- **Wrong protocol version:** the blocking alert (see Connection).
- **First run:** no provider signed in: a short welcome that leads to sign-in,
  then to choosing a project.
- **Empty project:** no sessions yet: the new-session view directly.
- **Session too large to load:** the timeline loads by turn, newest first,
  older turns on scroll (folded), with a count of what is not loaded yet.
  Search and "go to turn" fetch the turn they need.

## Settings

- **Appearance:** theme (light, dark, system, plus named themes shared by both
  clients as one token file), font and size (NSH), density.
- **Providers:** each provider with its status (signed in, account, expiry),
  sign in (browser or device code flow, shown in the UI), sign out, and
  default model. Adding a new provider (an OpenAI-compatible endpoint with a
  key, when Core supports custom providers).
- **Models:** default model and effort, the catalog with refresh.
- **Agents:** loaded agents (built-in, global, project), their mode and
  source; open an agent file in the editor; the default root agent.
- **Tools:** tools on by default for new sessions.
- **Skills:** discovered skills, source paths, diagnostics, and defaults for
  new sessions; loaded instructions remain inspectable in their context line.
- **MCP:** server configuration, enabled defaults, status, discovered tools and
  connect/disconnect/retry. Browser-use is configured by default; missing setup
  shows an actionable message. Secrets are never displayed in diagnostics.
- **Context:** automatic handoff threshold as an editable percentage, default
  80% of the model context window.
- **Dictation:** engine, language, push-to-talk key.
- **Keybindings:** the command list with editable keys.
- **Notifications:** when to notify (waiting for input, turn done, error),
  completion sound, OS notifications.
- **About:** client version, Core version and API version, daemon pid, home
  directory, link to the inspector.

Settings that are Core state (providers and defaults) are
read and written through the API, so they are the same in both clients. Pure
view settings (theme, density, keys) live per client.

## Inspector

The inspector page (`docs/inspector.md`) stays the deep view (Steps, Requests,
Raw). Both clients link to it from a session and from any tool line ("open in
inspector"), and both have a wire view toggle that shows the request bodies and
raw events inline.

## Core work this needs

Tracked in PLAN.md. In short: session drafts and starting context; frozen
system prompt; `ask` tool and question events; steer and queue; one-turn
sessions (title, btw); handoff instead of compaction; on-demand directory and worktree tools; archive, delete, search index; files, file read/write and
git status endpoints; attachments and images in both dialects; transcription
(maybe); API version in `/api/info`; stream resume; sign-in and settings over
the API; skills and MCP (including default browser-use); web search/fetch;
durable artifact storage and size limits; enforced read-before-edit
checks; idempotent mutations and multi-client conflicts. Core is accessible to
clients through its configured port.

## Open questions

- Dictation engine per platform.
- Steer key vs queue key defaults.
- Worktree cleanup on archive/delete, and leaving a worktree.
- Whether a user can steer a child session from the parent's card.
- Output caps (lines vs bytes) and whether they are a setting.
