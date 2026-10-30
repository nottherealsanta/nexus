# Interface plan: one interface for the TUI and NSH

Status: draft plan, 2026-10-27. Nothing here is built. This file covers what the user sees and does
in the native Ratatui TUI (`nexus chat`) and the NSH client (`nexus web`, `rust/app`). The core,
host and tool work these screens need is in [plan.md](plan.md); each item here links to its core
counterpart as **→ core §N**.

Sources read: `docs/surfaces.md`, `docs/ratatui-parity.md`, `docs/nsh-client.md`,
`docs/context.md` (compaction, same-session handoff), `docs/sessions.md`,
`plans/planning/nsh-tui-parity-inventory.md`, `nexus/host/protocol.py`, `nexus/view/model.py`,
`nexus/ui_support/details.py`, `nexus/ui_support/tool_details.py`, `nexus/tools/builtin/`.

## Contents

- [0. Principles](#0-principles)
- **Part A: Layout**: [A1 Shell](#a1-shell) · [A2 Left sidebar](#a2-left-sidebar-projects--sessions--files) · [A3 Right sidebar](#a3-right-sidebar-session--git) · [A4 Composer](#a4-composer) · [A5 Editor](#a5-built-in-editor)
- **Part B: The conversation**: [B1 Starting context](#b1-new-session-the-starting-context-editable) · [B2 Messages and turns](#b2-messages-and-turns) · [B3 Tool calls](#b3-tool-calls) · [B4 Child cards](#b4-child-sessions-inside-the-parent) · [B5 Child page](#b5-the-child-session-page) · [B6 Context handoff](#b6-context-handoff-compaction-as-a-new-session) · [B7 Copy](#b7-copy)
- **Part C: Waiting for the user's input**: [C1](#c1-one-idea-in-the-ui)
- **Part D: Alerts**: [D1](#d1-one-alert-system)
- **Part E: Commands and search**: [E1 Palette and slash](#e1-command-palette-and-slash-commands) · [E2 Search](#e2-search) · [E3 Session lifecycle](#e3-session-lifecycle-archive-delete) · [E4 One-turn sessions](#e4-one-turn-sessions-in-the-ui-title-btw)
- **Part F: Connection to the daemon**: [F1](#f1-connection-states)
- **Part G: Settings**: [G1](#g1-areas) · [G2 Themes](#g2-themes) · [G3 First run](#g3-first-run)
- [Part H: Phases](#part-h-phases) · [Part I: Checks](#part-i-checks) · [Part J: Open questions](#part-j-open-questions)

---

## 0. Principles

1. **One behaviour, two renderers.** Both clients have the same regions, rows, wording and actions.
   A difference is a bug unless `docs/surfaces.md` lists it as a medium difference (a click in
   NSH where the TUI uses a key, `Ctrl` where macOS NSH uses `Cmd`).
2. **What the user sees is what the agent sees.** The timeline the user reads and the context the
   agent gets are the same thing. Nothing is in the agent's context that the user cannot find in
   the timeline, and nothing in the timeline pretends to be context when it isn't. This is why
   compaction becomes a handoff to a new session (B6). It is now a rule in `AGENTS.md`.
3. **Host contract only.** Data comes from host commands or the reduced `ConversationView`, and
   durable state is in the log.
4. **One source for presentation.** Python row builders live in `nexus/ui_support/` for the TUI, and
   NSH ports them to `rust/app/src/present/` with the same names and shared fixtures.
5. **Everything is bounded.** Every list, text and scroll area has a stated cap and an announced
   "more".

---

# Part A: Layout

## A1. Shell

```
┌ top bar: session tabs · breadcrumb (project › worktree › ⎇ branch) · status ┐
├──────────────┬──────────────────────────────────────┬───────────────────────┤
│ LEFT         │ CONVERSATION                         │ RIGHT                 │
│ Projects     │  context header                      │ Session · Git         │
│ Sessions     │  timeline                            │                       │
│ Files        │  waiting-for-input tray (C)          │                       │
│              ├──────────────────────────────────────┤                       │
│              │ COMPOSER                             │                       │
└──────────────┴──────────────────────────────────────┴───────────────────────┘
```

- `Ctrl+B`/`▌` toggles the left sidebar and `Ctrl+L`/`▐` the right one. Each docks while there is
  room, else it opens as a drawer. The conversation keeps at least 56 cells / 480 px, and the left
  sidebar hides first.
- Widths: left 34 cells / 272 px, right 40 cells / 320 px. They can be dragged within 24..60 cells
  and persist per client.
- TUI: the left Projects/Sessions overlay (`sidebar::selector_area`) becomes a docked column again.
  NSH: build the tabbed `components/sidebar.rs`, a new `components/details.rs`, and the breadcrumb.

## A2. Left sidebar: Projects · Sessions · Files

### Projects
- Registered, current and recent projects (`ProjectSessionsList(projects=500, limit=0)`), newest
  first, the active one marked. Missing roots are left out. Linked worktrees nest under their
  repository.
- Row: folder name (the full path when names clash), session count, last activity. Selecting one
  opens it (`ProjectSessionOpen`), and Sessions and Files follow. `+ Add project` opens the directory
  picker.

### Sessions
- `+ New session`, a search box (E2), and a scope toggle **This project | All projects** that
  persists.
- Grouping: project (only in "All") → worktree (`main checkout`, `⎇ <branch>`; skipped when there
  are none) → day.
- Row: status glyph (spinner, `●` waiting for you, `✓`, `·`, `◇` archived), title, age; the
  current one has the accent bar. Root sessions only. A **handoff chain** (B6) is one row showing
  the latest part, with a `↻ 3` badge that expands to the parts.
- Row menu (right click / `…` / `m`): Rename title, Archive, Delete, Copy ID, Export (E3).
- More than 1,000 rows: the cut is announced.

### Files
- A tree of the current project (not session-specific): folders first, then by name. `▸`/`▾`
  folds a folder in place. Each expand calls `DirectoryList(path)` (lazy, 200 entries, the cut
  announced); the expanded set is cached per project, and `r` refreshes it.
- **Git status marks** on every row, from `GitStatus`: `M` modified, `A` added, `D` deleted, `?`
  untracked, `U` conflict, `R` renamed, coloured by token. A folder shows a dot when anything
  under it changed. Ignored files are dimmed.
- Opening a file opens it in the built-in editor (A5). Links are shown and never followed.
  Refused entries are announced ("hidden entries are not listed").
- Actions on a row: Open, Insert `@path` into the composer, Copy path, Reveal in the OS file
  manager (NSH native only).
- TUI keys: `↑↓`, `→`/`Enter` expand or open, `←` collapse or go to the parent, `Tab` changes tab.

## A3. Right sidebar: Session · Git

For now, only two tabs. Changes, MCP, Jobs and Logs leave the sidebar: jobs show inline as
Bash rows (B3), and MCP, Jobs and Logs are reached from the palette (E1) as dialogs.

### Session tab (labelled rows, grouped)

| Group | Rows |
| --- | --- |
| Overview | Status, Agent, Model, Effort, Started (local time + age), Last activity, Duration, Workspace / worktree / branch |
| Activity | Turns, Messages, Tool calls (total, then top tools by count: `Read 14 · Bash 6 · Edit 3`), Failed calls, Retries |
| Tokens | Input, Output, Cache read, Cache write, Reasoning, Cache hit %, Cost when the price is known |
| Context | Size and % of the window; handoff chain (`part 2 of 3 · continued from …`) |
| Child sessions | Launched, Running, then one row per child that opens B5 |
| — | Copy session ID |

Measured numbers only. A provider that reports nothing says so. This extends
`ui_support/details.session_rows`, with the same in `present/details.rs`.

### Git tab
- Git status of **the checkout this session is in**: the worktree when the session is in one,
  else the main checkout. The header says which: `⎇ brave-otter (worktree of nexus) → main ·
  ↑2 ↓0`.
- Sections: Conflicts, Staged, Changes, Untracked, one row per file (word + name). A click opens
  the file's diff in the editor (A5, read-only diff mode). Read only: nothing here changes the
  repository.

## A4. Composer

```
│ Ask anything…  (/ commands · @ files · ! shell)                         │
│ [image 1 · shot.png ×] [document · spec.pdf · 12 KB ×]                  │
│ ◆ Build ▾   claude-opus-5-5 ▾  anthropic   effort: high ▾      🎙  ⏎    │
│ 48K (24%) ━━━━━━━──────────────────┃────                               │
```

- **Editor:** multi-line, no border. Enter sends or steers (B2.5), `Ctrl+Enter` queues,
  `Shift+Enter` adds a newline, `↑` in an empty composer recalls history, `!` is shell mode, `@`
  completes paths, `/` opens commands (E1). The draft is kept per session and survives switching.
- **Agent / model / effort** pickers work **at any time, including mid-turn**. A mid-session change
  applies at the next model step and raises a **cache warning** through the alert system (D1): `Changing
  the model starts a new prompt cache: the next request re-sends ~48K tokens (≈ $0.14). Change /
  Cancel`. The warning names the cost when the price is known. A change is a durable event and
  shows as a row in the timeline (`◆ Build → ◆ Plan · from step 4`), so the user sees the same thing
  the agent got (principle 2).
- **Attachments:**
  - Paste an image or file, or **drag and drop** files onto the composer or the conversation. NSH
    reads the drop. The TUI receives a dropped path as pasted text: a paste made only of existing
    paths is offered as `Attach 2 files? · Enter attaches · Esc pastes as text`.
  - Each attachment is a chip (`image 1 · shot.png`, `document 1 · spec.pdf · 12.4 KB`) with `×`, and
    a click previews it. Uploads go through `AttachmentPrepare` (12 MiB each). A refused file (too big, wrong
    type) raises an inline alert naming the limit. Count and total size are capped (8 files, 32 MiB)
    and the cap is announced.
  - Large pasted text (over 2,000 characters or 40 lines) becomes a `pasted text · 312 lines`
    chip that can be expanded and edited, so the draft stays readable.
- **Voice dictation** (`docs/voice.md`):
  - The mic button, or a hold-to-talk key (`Ctrl+Space`, configurable). While recording: a level
    meter and the elapsed time, with the live partial transcript shown greyed in the editor and
    replaced by the final text.
  - `Esc` cancels; the transcript is never sent automatically.
  - States, all shown: model not downloaded (with an offer to download and its size, asked first),
    loading, recording, transcribing, failed with the reason, mic permission denied.
  - NSH captures in the browser (16 kHz PCM WAV to `/v1/web/voice`); the TUI captures natively.
- **Not in the composer:** GPT Live. A Live call is started from the palette and shows as a call
  bar in the top bar.
- **Context meter:** `48K (24%)` with the price-tier marks (`┃`). While thinking, the latest thinking
  heading sits beside it.
- **Child page:** the composer is read-only, labelled with the reason.

## A5. Built-in editor

A simple in-app editor for viewing and small edits, not an IDE.

- TUI: [`ratatui-code-editor`](https://github.com/vipmax/ratatui-code-editor). NSH: the NSH
  [editor element](https://nsh.dev/elements/editor/). Both get syntax highlighting, line numbers,
  find (`Ctrl+F`), go to line, undo/redo, soft wrap and read-only mode.
- It opens as a tab over the conversation column (`File · nexus/host/facade.py`) with a close `×`.
  `Esc` returns to the conversation. Several files can be open, each a tab beside the session tabs,
  marked as a file.
- Reads go through `FilePreview`, and saves through a new host command `FileWrite` (→ core §8)
  checked by `tools/permissions.py`. A save sends the hash it was opened from; if the file changed
  on disk since, the save is refused with an alert offering *Reload* or *Overwrite*.
- Bounds: files over 1 MiB open read-only with a notice. Binary files are not opened (`Not shown:
  binary · 2.3 MB`).
- Diff mode (read-only): from the Git tab or a tool's diff, the file with the changes marked.
- If the agent is writing the same file in a running turn, the editor shows a banner and becomes
  read-only until the turn ends.

---

# Part B: The conversation

## B1. New session: the starting context, editable

A new session opens on a **start page**: the context header expanded, showing everything the first
request will carry, with the composer below it.

| Block | Shows | Before the first turn you can |
| --- | --- | --- |
| Agent / model / effort | root agent and model | change (exists) |
| System prompt | literal prompt + tokens | edit for this session (→ core §6) |
| Environment | cwd, platform, date, git | read only |
| AGENTS.md / MEMORY.md | each file found + path | turn off, or edit for this session (→ core §6) |
| Skills | every skill, on/off, scope; SKILL.md | toggle (exists) |
| Tools | built-in + MCP families → tools → schema | toggle (exists) |
| MCP | servers, loading mode | toggle, set mode (exists) |
| Total | estimated first-request size, % of window | — |

- `ContextInspect` runs on open and after every change.
- An edited block is tagged `edited`, with *Revert to file*. *Save to AGENTS.md* is separate and
  explicit.
- After the first turn the header folds to one-line rows. Skill, tool and MCP switches and the text
  edits lock with the prompt-cache explanation. Agent, model and effort stay changeable, with the
  cache warning (A4).

## B2. Messages and turns

### B2.1 Turn structure

```
▼ #3  Add worktree grouping to the sessions list                  10:42
  [image 1 · sidebar.png] [pasted text · 312 lines]

  ◆ Build
    I'll look at how sessions are grouped first.
    ✓ Read 3 files                                              0.1s
    ✓ Edit  nexus/ui_support/sidebar.py                 +42 −8  0.2s
    The sessions list now groups by worktree …
                       claude-opus-5-5 · 1m 12s · ↑64K ↓3.1K · 81% cached · 800 r  ⧉
```

- **User block:** a fold chevron, the **turn number** `#3` (highlighted), the first line of the message
  as the heading, and the local time on the right. The full message follows when it is more than one
  line. Attachments are chips under it.
- **Folding:** a completed turn folds to its user row plus a one-line summary (`◆ Build · 12 tools ·
  +42 −8 · 1m 12s`). `[`/`]` fold or unfold all. The running turn never folds. Fold state is
  per client and not durable.
- **Agent label:** the first reply of a turn sits under `◆ <Agent>` in the agent's colour, and an
  agent change mid-turn (A4) adds a new label.
- **Turn footer** (right-aligned, muted): model, duration, `↑input ↓output`, cache %, reasoning
  tokens `r`, and cost when known, then `⧉` copy (B7). Parts without data are left out. A failed or
  cancelled turn ends in a red or grey `failed: <reason>` / `cancelled by you` line instead.
- **Between turns:** agent or model changes, worktree moves and handoff boundaries (B6) are labelled
  rows, placed by time.

### B2.2 Markdown

Assistant prose, user messages (when they contain Markdown), SKILL.md and reports share one renderer
(`ui_support/markdown` with a Rust port):

| Element | TUI | NSH |
| --- | --- | --- |
| Headings | bold, accent, `#` dropped; H1–H2 get a blank line above | sized headings |
| Paragraphs, emphasis, strike | wrapped to the column; bold/italic/dim | same |
| Lists, task lists | `•`/`1.` with hanging indent; `☐`/`☑` | same |
| Inline code | `element` background | same |
| Code blocks | panel background, language label, line numbers when 5+ lines, syntax highlight, **copy button**; long blocks clip at 40 lines with *Show all N lines* | same, 600 px max then scroll |
| Tables | box-drawn, columns shrink before wrapping; wider than the column scrolls horizontally inside the block | HTML table, horizontal scroll |
| Block quotes | `▎` gutter, muted | same |
| Links | text underlined, and the URL shown after when it differs from the text; opening asks first for non-`http(s)` | link shows the URL on hover; opens in a new tab with `rel=noopener` |
| Images | `image · alt · url`, never fetched | **not loaded remotely**; a placeholder with *Load image* |
| Mermaid | the existing native image fallback, else the source | rendered diagram, source on toggle |
| Math | the source in a code style | KaTeX (later) |
| HTML | shown as text, never interpreted | escaped |

Streaming: text appears as it arrives. The streaming write head stays, and a partially received
code block renders as code from its opening fence.

### B2.3 Images and attachments in the timeline
- Images: NSH shows a thumbnail (max 240 px) that opens full size in a viewer. The TUI shows the chip,
  and `Enter` opens it in a terminal-graphics preview where supported (kitty/iTerm/sixel), else
  `image · 1280×720 · 340 KB` and *Open externally*.
- Documents: a chip with name, type and size of the extracted text. The click opens the extracted
  text the model saw (principle 2), not the original file.

### B2.4 Large text
- A user message over 40 lines folds to 12 lines with `… 312 more lines · Show all`.
- Pasted-text chips (A4) show as a chip with a click to expand inline (capped by B3.4's limits).
- Assistant prose is never folded mid-reply. Code blocks inside it follow B2.2.

### B2.5 Steer vs queue

| | **Steer** (Enter while a turn runs) | **Queue** (`Ctrl+Enter`, or Enter when steering is off) |
| --- | --- | --- |
| When it reaches the agent | at the next model step of the **running** turn, after the current tool | when the running turn ends, as a **new turn** |
| Where it shows | inline in the running turn, where it was delivered: `↳ you (steer) · delivered at step 5` and the text; before delivery it's marked `pending` | in a **queue tray** above the composer: numbered rows with the text, `Edit`, `↑↓` reorder, `×` remove, *Steer now* |
| Changes the current turn | yes | no |
| Idle session | same as a normal send | same as a normal send |

Both are durable (`SessionQueueMove`, `SessionQueueRemove`, `SessionQueueSetMode`), shown the same in
both clients, and survive reconnect. The composer hint while a turn runs says which Enter will do:
`Enter steers · Ctrl+Enter queues`.

## B3. Tool calls

### B3.1 Collapsed by default, expand in place
- Every tool call is **one collapsed row** by default. A click or `Enter` expands it in place into a
  tool-specific body; again collapses it. Fold state is per client.
- This replaces "activating a call opens a modal, never inline" (`docs/surfaces.md`). The modal stays
  as **Open full** for long output. Record this in `docs/decisions.md`.
- Tool groups (`Read 4, Searched 2`) stay, and expanding a group lists its member rows, collapsed.

### B3.2 The collapsed row

```
✓ Edit   nexus/host/facade.py                           +12 −3   0.2s
⣾ Bash   pytest -q tests/test_host_facade.py                     4.1s
● Bash   rm -rf build/ · waiting for your approval
✗ Read   docs/missing.md · failed: not found
```

Status glyph (spinner / `✓` / `✗` / `●` waiting for you), sentence-case verb, main argument
(clipped `…`), right-aligned stats, duration.

| Tool | Main argument | Collapsed stats |
| --- | --- | --- |
| Edit, MultiEdit, Write, apply_patch | path, or `3 files` | **`+N` green, `−M` red** from `ToolCallView.diff`; new file `new · +N` |
| Read | path `:a-b` | `N lines` |
| Bash / shell | first line of the command | `exit 0` / `exit 1` red / `running` / `bg` |
| bash_output, kill_shell | job + command | new lines / `stopped` |
| Grep | pattern · path | `N matches in M files` |
| Glob, Ls | pattern / path | `N entries` |
| WebFetch | host + path | status, size |
| WebSearch | query | `N results` |
| Skill | name | — |
| ask (question) | the question | `waiting` / answer |
| worktree tools | action + name | — |
| code / kernel | `In [n]` + first line | nested call count |
| Task | a child card (B4), not a row | |
| MCP tool | `server · tool` + first argument | result size |
| Unknown | name + `compact_args` | result size |

A group and a folded turn show the summed `+N −M`.

### B3.3 Expanded body, per tool

Labelled sections from the `tool_details.py` vocabulary. Parameters not shown in the tool's own body
are always listed in a trailing **Parameters** section.

| Tool | Expanded body |
| --- | --- |
| **Edit / MultiEdit / apply_patch / Write** | Per file: header `path  +N −M`, unified diff with line numbers, `+` green, `−` red, context muted, `⋯ N unchanged lines` between hunks; multi-file patches are foldable per file; a new file is all added lines; on failure, the error first, then the intended change. *Open in editor* (A5 diff mode). |
| **Read** | `path · lines a–b of N · size`, then the **content read**, line-numbered and highlighted; images as in B2.3; tool clipping announced. |
| **Bash / shell** | **Input:** the full command, cwd if not the workspace, timeout, background. **Output:** stdout and stderr in recorded order, stderr tinted. **Footer:** exit code or signal, duration, size. While running, the live output follows. Background jobs: *Stop*, *Show log*. |
| bash_output / kill_shell | the job, then the output read. |
| **Grep** | pattern, path, flags; results grouped by foldable file, match highlighted, with line numbers. |
| Glob / Ls | pattern, then the paths as a compact tree. |
| WebFetch | URL, status, type, size; the converted Markdown (B2.2). |
| WebSearch | the query; title / URL / snippet rows. |
| ask | see C. |
| Skill | name, scope, the SKILL.md loaded. |
| code / kernel | notebook form (`_cell_sections`); nested calls as collapsed rows. |
| MCP tool | flattened parameters (`a.b[0]`); text result rendered; JSON as labelled rows. |
| Unknown | the generic `tool_details` sections. |

### B3.4 Caps and scrolling
1. **View:** strings are clipped at 8,192 characters (`browser_view.py`), and tool output is bounded by
   `_output_limit.py`.
2. **Body:** at most **20 rows / 400 px**, with its own scroll so the timeline doesn't jump. Bash opens at
   the end, and Read and diffs at the top.
3. **Beyond the view:** `Clipped: showing the last 200 of 12,480 lines (1.3 MB) · Open full`. *Open
   full* is a whole-window viewer that pages the full durable result through `ToolResultRead` (→ core
   §7), 64 KiB per page, keeping at most 4 MiB in memory. Running jobs page through `SessionJobLog`.
4. Inline caps: diffs 2,000 lines per file and 50 files; Grep 500 matches; the rest behind *Open full*.

### B3.5 Per-tool checklist
For each tool, cover the collapsed row, stats, body, running, failed, waiting-for-you, the cap line,
and a recorded fixture in `tests/fixtures/` that renders in both clients.

## B4. Child sessions inside the parent

```
◆ Explore · Find where sessions are grouped            ⣾ running · 12 tools · 1m 04s
  ⎿ Grep "group_by" nexus/ui_support

◆ Build · Add worktree grouping                        ✓ 34 tools · 48K tok · 3m 10s  ›
```

- **Header:** the agent's diamond (animated while running, red if failed), name, description; on the
  right, status, tools, tokens, duration (no duration while running); `›` opens the page.
- **Collapsed (default):** the header, plus a latest-activity line while running.
- **Expanded:** the **Task** the parent sent (full, capped), the last 10 child tool rows (`N
  earlier`), and the **Report**, rendered (B2.2). States from `AgentView`: running, interrupted,
  finished, stopped (by whom, why).
- **Open** (title, `›`, `o`): the child page (B5). Running card actions: *Stop* (`AgentCancel`);
  legacy children: *Close*.
- Parallel children sit together in launch order. Grandchildren show as a count on the card and in
  full on the child's page.
- A child waiting for the user's input (C) shows `●` on its card and raises the same tray item in
  the parent.

## B5. The child session page

```
┌ ← Back   Fix auth flow  ›  ◆ Build · Add worktree grouping  ›  ◆ Explore · …   ⣾ Working ┐
├──────────────┬──────────────────────────────────────┬─────────────────────────────┤
│ (left        │ context header (what the child got)  │ Session tab for THIS child   │
│  unchanged)  │ Task block                           │ Git tab for its checkout     │
│              │ timeline (same rows as B2/B3/B4)     │                             │
│              ├──────────────────────────────────────┤                             │
│              │ composer: read-only (subagent)        │                             │
└──────────────┴──────────────────────────────────────┴─────────────────────────────┘
```

- **Top bar:** the session tabs give way to `← Back` plus breadcrumbs (root title `›` ancestors `›`
  this child), each clickable, with the status on the right. `Esc` / *Back* goes up one level and
  returns to the parent **scrolled to the card it came from**.
- **Body:** the same renderers as a root. The read-only context header shows the request the child
  actually got, then a grey Task block, then the timeline and its own child cards.
- **Right sidebar:** the child's own numbers, and the Git tab for its checkout (a child in its own
  worktree shows that one).
- **Live:** a running child streams in place. A root follow-up shows as a `root_followup` user row.
- **Routing:** NSH `/app/#s/<root>/a/<agent>[/a/<agent>…]` so reload and browser Back work; the TUI
  keeps a page stack that survives reconnect.

## B6. Context handoff: compaction as a new session

The interface side of → core §2. When the context fills up, the session **hands off to a new root
session** instead of silently compacting, so the timeline and the agent's context stay identical
(principle 2).

- **End of the old session:** a boundary row:
  `↻ Context full (182K of 200K) · continued in "Fix auth flow (2)" ›`. The old session becomes
  read-only, with a banner linking to the continuation.
- **Start of the new session:** its context header has a **Handoff** block (the brief that was
  carried over: intent, decisions, open files, queue, children), labelled with its token count and
  its source: `from "Fix auth flow" · turns 1–14`. The brief is what the agent sees, word for word. A
  `↺ continued from …` link sits in the breadcrumb row.
- **Sessions list:** a chain is one row (latest part) with `↻ 3`, expanding to the parts (A2).
- **Before it happens:** at 85% of the window the meter turns amber, and an alert (D1) says `Context
  is 85% full · a handoff to a new session will happen at 90% · Hand off now / Details`. A manual
  handoff is in the palette (`Hand off to a new session`).
- **Settings:** the threshold and the brief style (deterministic / summarized) are under Context
  & memory (G1).

## B7. Copy

One click (NSH) or `c` on the selected row (TUI, OSC52) copies:

| What | Copies |
| --- | --- |
| User message | the text, plus attachment names |
| Agent's final message of a turn (the footer `⧉`) | the Markdown source |
| Any assistant block | the Markdown source |
| Code block | the code only |
| Tool call | input and output as labelled plain text; separate *Copy command* / *Copy output* for Bash, *Copy diff* for edits |
| Child card | the report |
| Session | the ID; *Copy as Markdown* (the whole transcript, from export) |

A copy shows a brief `Copied` alert (D1). Large copies (over 1 MiB) ask first and say the size.

---

# Part C: Waiting for the user's input

## C1. One idea in the UI

In core, a **permission request** and a **question** stay separate (→ core §3, §4). In the
interface they are one idea: **the agent is waiting for your input.** Both get the same card,
the same places, the same keys and the same lifecycle.

**Kinds** (all render through one `ui_support/waiting.py` model):

| Kind | Source | Choices |
| --- | --- | --- |
| Permission | a tool needs approval | Allow once · Allow for this session · Deny · Deny and tell the agent why (unavailable choices are disabled, as today) |
| Question | the `ask` tool (→ core §3) | the options it offered, and/or free text; multiple choice when asked |
| Worktree proposal | `checkout` isolate with `policy = ask` (→ core §9) | Move to a worktree · Stay in the main checkout |
| Sign-in needed | a provider token expired mid-turn | Sign in · Switch model |

**The card:**

```
● Waiting for you · Bash wants to run                               2m ago
  rm -rf build/
  in ~/repos/nexus · timeout 120s
  [1 Allow once] [2 Allow for session] [3 Deny] [4 Deny with reason…]
```

- A header with `●`, the kind in words, and what is asking (agent, tool). Then **everything the
  decision needs**: full parameters, a diff for edits, the path and its permission rule, the
  question's context. Then the choices, numbered.
- Keys: `1`–`9` choose, `Tab` moves, `Enter` confirms, and free text goes in an inline field.
- **No timeout in the UI.** A question waits until answered or the turn is cancelled (→ core §3). The
  card shows how long it has waited.

**Where it appears:**
1. **Inline**, at the call site in the timeline (the tool row or the child card shows `●`).
2. **The waiting tray** above the composer, pinned while the session has any waiting item, so it
   is visible when scrolled away. Several items stack, oldest first, with `1 of 3`.
3. `●` on the session tab, the Sessions row, the project row, and the child card in the parent.
4. An alert (D1) when the session isn't in view: a toast in the app, and an OS notification with a
   sound when the window isn't focused.

**Lifecycle:** pending → answered (the card collapses to one line: `✓ Allowed once by you · in
terminal · 10:42`) / cancelled (`turn cancelled`) / superseded. The answer is durable, and any
client can answer. Other clients update at once and show who answered and where. The composer stays
usable while waiting: typing a message while a question is pending offers *Send as the answer*.

---

# Part D: Alerts

## D1. One alert system

Every "tell the user something" goes through one model (`ui_support/alerts.py`, a Rust port), with
one look and one set of rules.

| Level | Use | Example |
| --- | --- | --- |
| **Needs input** | C1 items | `Bash wants to run` |
| **Confirm** | an action with a cost or risk, before it happens | cache invalidation (A4), delete session, large copy |
| **Warning** | something degraded or about to happen | context 85% full, provider usage 90%, daemon reconnecting |
| **Error** | a failure the user should know | send failed, save refused |
| **Info** | done or a passing note | `Copied`, `Session archived · Undo` |

| Placement | When |
| --- | --- |
| Inline (in the timeline or next to the control) | tied to a place: the cache warning next to the model picker, a refused attachment by the composer |
| Toast (bottom right, auto-dismiss 4 s for info, sticky for error) | transient; can carry one action (`Undo`) |
| Banner (top of the conversation, until resolved) | a state: disconnected, read-only session, version mismatch |
| Drawer (bell in the top bar, with a count) | the history of all alerts this run, newest first, filterable |
| OS notification + sound | needs-input or turn finished while the window is unfocused; configurable |

Rules: alerts are deduplicated by key (a repeated warning bumps a count), and dismissals of "don't
show again" warnings persist. Every alert says what happened, why, and what you can do. Durable
events (errors in the log) also appear in the timeline; alerts never replace the timeline.

---

# Part E: Commands and search

## E1. Command palette and slash commands

- **Palette:** `Cmd+P` (macOS NSH) / `Ctrl+P` (TUI and other platforms). Fuzzy search over
  **every action**, plus sessions, projects and files (prefixes: `>` commands, `#` sessions, `@`
  files, `:` settings). Each row shows the name, a short description, and its key binding.
- **Slash commands:** `/` in the composer opens the same command list, filtered to what can run as a
  message (`/btw`, `/review`, `/merge`, `/new`, `/model`, `/agent`, `/effort`, `/handoff`, …).
- **One registry:** palette and slash commands come from one table (extending `ui/cli/commands.py`
  and its Rust mirror). Each command declares its id, title, description, slash name (optional), keys,
  and when it's available (idle only, needs a session, root only). Unavailable commands are listed,
  disabled, with the reason.
- Must be in the palette: new session, open project, switch session, rename, archive, delete, export,
  copy ID, hand off now, `/btw`, change model, agent and effort, toggle sidebars, open file, search,
  Settings (each area), MCP servers, Jobs, Logs, Doctor, theme, start a GPT Live call, voice, keyboard
  shortcuts, reconnect, restart daemon (TUI).

## E2. Search

- **Sessions search** matches **only the session title and the user's messages**. This keeps it
  fast and keeps the index small (→ core §5). The left sidebar's search box and the palette's `#`
  use it.
- Results: title, a snippet of the matching user message with the match highlighted, project, and age.
  Opening one jumps to that turn.
- Debounced at 150 ms, at most 50 results, with the cut announced. The scope follows the This project /
  All toggle.
- **In-session find** (`Ctrl+F` in the conversation): search the loaded timeline, including expanded
  tool output, with next/previous and a match count. This is client-side over what is loaded, and
  says so.

## E3. Session lifecycle: archive, delete

| Action | Effect | UI |
| --- | --- | --- |
| **Rename** | changes the title (the immutable name stays) | inline edit on the row or the tab |
| **Archive** | hidden from the list, kept whole, reversible | row menu / palette; toast `Archived · Undo`; an **Archived** group at the end of the list (collapsed) with *Unarchive* |
| **Delete** | moved to trash, then purged after the retention period (→ core §5) | a Confirm alert naming the title and how many child sessions go with it; toast `Deleted · Undo` (30 s); **Trash** in Settings → Sessions & storage, with *Restore* and *Delete forever* |
| **Export** | JSONL / Markdown | palette, row menu |

A running session can't be archived or deleted: the action is disabled with `Stop the turn first`.
Archiving or deleting the current session opens the neighbouring one.

Worktree controls: **TBD**. The agent-side design is discussed in → core §9.

## E4. One-turn sessions in the UI: title, `/btw`

The interface side of → core §1.

- **Session title:** the title one-turn session runs after the first user message. The tab and row
  show `New session` and a subtle spinner until the title arrives. The right sidebar's Overview links
  `Title · made by claude-haiku-5-5 · view request`, so the request and answer can be opened like any
  session (principle 2). It is never listed in Sessions.
- **`/btw <question>`:** ask a side question while the agent works, without touching its context.
  - It opens a **side panel** over the conversation's right edge (or a drawer on narrow widths):
    `btw · based on the agent's context up to step 7 (Read facade.py)`, then your question, the
    streamed answer, and the footer (model, tokens, cost).
  - The main turn keeps running and is not interrupted. The answer never enters the main agent's
    context, and the panel says so: `Not sent to the agent`.
  - Actions: *Copy*, *Send to the agent as a steer* (turns the btw answer into a steer message, B2.5),
    *Close*. Several `/btw` panels in one session are listed in a small `btw (3)` menu in the top bar.
  - It works when idle too (then the context is everything up to the last step).

---

# Part F: Connection to the daemon

## F1. Connection states

Every client runs one connection state machine (`ui_support/connection.py`, a Rust port), shown the
same way:

| State | Shown as | Client does |
| --- | --- | --- |
| Starting | `Starting Nexus…` full-page (first launch only) | auto-starts the daemon (TUI), waits up to 10 s |
| Connecting | spinner in the top bar | `Health`; checks the protocol version |
| **Version mismatch** | **blocking banner, no interaction**: `This client (protocol 7) does not match the daemon (protocol 8). Restart the daemon / Update Nexus`; minor-compatible differences show a warning only | halts all commands; never guesses (→ core §10) |
| Connected | nothing (the status dot is quiet) | snapshot, then live stream |
| Resyncing | thin top bar progress, `Resyncing…` | re-snapshots the open session after a `resync` frame or a gap; keeps the draft, folds, scroll |
| Reconnecting | warning banner `Reconnecting… (attempt 3)` with backoff 1 s → 10 s; the timeline stays readable; the composer keeps the draft and **holds** sends, marked `will send when connected` | retries; on success resyncs |
| Daemon restarting | banner `Nexus is restarting` | waits, reconnects, resyncs, reopens the same session and page |
| Lost | error banner after 60 s: `Can't reach Nexus · Retry · Doctor` | stops retrying until asked |

- Nothing typed is lost: drafts, queued-while-offline sends (sent once, with a client request id
  so a retry never duplicates) and the waiting tray survive.
- A stale async answer (another session, an older request) is dropped, as today.

---

# Part G: Settings

Full-window page: the area list on the left, the area's page on the right. Both clients render the
**same typed page** (`ui_support/settings_page.py`). The page is rebuilt from the host after every
change. NSH has no Settings today; how it gets the pages is Part J question 1.

## G1. Areas

| Area | Contents | Host commands |
| --- | --- | --- |
| **Appearance** | Theme (System / Dark / Light / user themes), accent, font and size (NSH), density, centered layout, reduced motion, live preview | local prefs |
| **Layout** | sidebars open by default, widths, starting tabs, session tab orientation, tool calls start collapsed (default yes) | local |
| **Providers** | one section per provider, connected first: status, auth method, models found, usage; *Sign in* (OAuth or device code in place, with the code and copy), *Set API key* (masked), *Sign out*, *Refresh models* | `ProvidersStatus`, `ProviderLogin*`, `ProviderKeySet`, `ProviderLogout`, `ProviderModelsRefresh`, `ProvidersUsage` |
| **Providers → Add provider** | an OpenAI-compatible form: name, base URL, dialect (Responses / Chat), API key, headers; *Test connection* (lists the models found), *Save*; presets (OpenRouter, Ollama, LM Studio, …); *Remove* | → core §8 |
| **Models** | default chain, title model (E4), `/btw` model, subagent tiers, per-model effort, the catalogue (price, window, tiers) | existing |
| **Agents** | agent files: name, colour, model, tools, prompt; Edit / New / Reset | `SettingsRead/Write/Reset/Delete` |
| **Tools & permissions** | tools on/off by scope; approval mode; path allow and deny lists (shown, never loosened silently) | `SettingsWrite` |
| **Skills** | list, scope, SKILL.md, on/off | existing |
| **MCP servers** | global then project; add, loading mode, enable, state, tools | `SettingsMcp*` |
| **Context & memory** | AGENTS.md / MEMORY.md files found; handoff threshold and brief style (B6); context-header options | `SettingsRead/Write` |
| **Hooks** | configured hooks, read-only first | `SettingsInventory` |
| **Voice & speech** | dictation model (download, size, asked first), language, hold-to-talk key, mic device | existing |
| **GPT Live** | as today | existing |
| **Notifications** | OS notifications and sound for needs-input and turn-finished; reset dismissed warnings | local |
| **Keyboard** | every binding from the command registry (E1); rebinding later | local |
| **Sessions & storage** | database path and size, archived count, **Trash** (restore, delete forever), retention, export | → core §5 |
| **About** | version and protocol version for client and daemon, daemon status, `nexus doctor` as rows, update available, restart daemon | `Doctor`, `Health` |

## G2. Themes
- A theme is named colour tokens: background, panel, element, text, muted, accent, success/`+`,
  danger/`−`, warning, the agent palette, diff add/remove backgrounds, and Git marks. The tokens live in
  one Rust crate shared by `rust/tui` and `rust/app`, so the colours match. The TUI degrades to
  256 or 16 colours; NSH maps the tokens to CSS variables.
- Ships with Nexus Dark, Nexus Light, and System. Later: user themes as TOML in `~/.nexus/themes/`.
- The diff and Git colours are checked for contrast in every theme.

## G3. First run
With no provider connected, Settings opens on Providers with the callout `Connect a provider to
start`, then offers *Choose default model*. The same flow in both clients.

---

# Part H: Phases

Each phase ships **both clients**, updates `docs/surfaces.md`, `docs/ratatui-parity.md`,
`docs/nsh-client.md` (and `docs/module-map.md`, `docs/decisions.md`), adds tests, and lists its
core dependencies from [plan.md](plan.md).

1. **Connection states** (F1), plus the version handshake (core §10). Everything else rests on this.
2. **Shell, right sidebar** (A1, A3).
3. **Alerts** (D1) and the **command registry, palette and slash commands** (E1).
4. **Messages and turns** (B2): user blocks, folding, Markdown, images, large text, footers, steer vs
   queue.
5. **Tool rows** (B3.2), then **tool bodies** (B3.3–B3.4), then *Open full* (core §7).
6. **Waiting for input** (C1), with the `ask` tool (core §3).
7. **Child cards and pages** (B4, B5).
8. **Left sidebar** (A2), **search** (E2, core §5), **lifecycle** (E3).
9. **Composer:** pickers with the cache warning, attachments, drag and drop, voice (A4).
10. **Start page and editable context** (B1, core §6).
11. **One-turn sessions:** title and `/btw` (E4, core §1).
12. **Context handoff** (B6, core §2).
13. **Editor** (A5, core §8).
14. **Settings in NSH** (G), **themes**, **custom providers**.

---

# Part I: Checks

- **Shared fixtures** in `tests/fixtures/`: recorded sessions covering every tool (success and failure),
  a running Bash, huge output, a multi-file patch, nested children, a permission and a question, a
  steer and a queue, a handoff chain, a `/btw`, and attachments. The Python and Rust presenters are
  tested against them, so drift fails a test.
- **Python:** each `ui_support` model (details, waiting, alerts, connection, markdown, commands),
  stale answers, and the layering tests.
- **TUI:** PTY tests per phase, plus native screenshots (native-app-review skill) in Dark and Light.
- **NSH:** `cargo test` for the presenters, and Playwright on `nexus web` per phase (expand a tool,
  *Open full*, diff colours, child page with browser Back, a waiting card answered from the web while
  the TUI updates, drag and drop, a version-mismatch banner, reconnect with a held send).
- **Parity checklist:** one table, each feature with a TUI column and an NSH column, kept in
  `docs/surfaces.md`. A feature is done when both are ticked.
- Each phase states what was verified and what was not.

---

# Part J: Open questions

1. **Settings pages for NSH:** serve the typed page from the daemon (recommended, one source), or port
   each page to Rust?
2. **Fold memory:** keep expanded tools expanded after a reload? Proposed: yes per client, never in the log.
3. **System prompt editing:** full replacement (recommended) or append-only?
4. **Children in the Sessions list:** hidden (proposed) or expandable under the parent?
5. **Diff layout:** unified everywhere, or side-by-side when wide?
6. **NSH breakpoint** for drawers: proposed 1100 px.
7. **Skill/tool/MCP switches after the first turn:** stay locked (proposed), or allowed with the same
   cache warning as model changes?
8. **Delete retention:** how long Trash keeps sessions (proposed 30 days).
9. **`/btw` model:** the session's model (best cache reuse, recommended) or a cheaper configured one?
