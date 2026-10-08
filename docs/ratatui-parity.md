# Ratatui implementation and parity ledger

Session titles setting saves preserve the parent navigation history. Toggling
automatic titles or choosing a model returns to the existing Session titles page;
Escape still returns to its parent rather than losing the Settings stack. Both
paths have Python workflow regression coverage; native PTY verification remains
pending in `plans/done/SETTINGS_REVAMP_PLAN.md`.

Untitled sessions display as **New Session** in the native tab bar and session
picker, including immediately after creation and after session-list refreshes.
Existing titles remain unchanged; session IDs are retained for navigation, not
used as fallback names.

### Completion notifications

The native client plays a Nexus-generated two-note completion cue through
`sounddevice`, using the same nonblocking playback path as recording cues—not
an OS system sound or terminal bell. Missing audio dependencies or output
devices are silently tolerated. `NEXUS_COMPLETION_SOUNDS=off` disables this cue.
Known background tabs also notify once when they transition to unread completed
state; repeated session refreshes do not repeat the sound. An unread finished
tab shows a solid blue circle until the session is viewed; idle/read tabs retain
the quiet gray indicator. Opening a session acknowledges the replayed completion
watermark as well as streamed terminal events, even before the next sessions
refresh. General `last_seq` remains the cursor for all log records; presence-only
updates when a view detaches cannot restore an acknowledged completion dot.
Switching away cannot restore the dot or replay the cue for that viewed completion.
Initial discovery of an old completed session does
not play a sound.

The context header ends with a right-aligned `Context total · ~N tokens` footer,
using the same subdued summary styling as per-turn usage. It sums the shared
header section estimates (not conversation history or future output), and shows
`tokens unavailable` until a context preview is available. Child-agent headers
use their own preview in the same way.

User-message borders use the turn's recorded agent color, falling back to the
active agent color when the turn has no recorded color.

Live turn completion and failure emit the terminal notification bell. The Python bridge carries a monotonic `completion_bell` counter; native
snapshots ring only when it increases, so redraws and historical replay stay
silent. Cancellation and subagent activity do not ring. Terminal emulator bell
settings determine whether the notification is audible or visual.

Context-header inspection uses an inset modal (System prompt, Environment, AGENTS.md, MEMORY.md and tool pages use the compact `detail` layout; Skills/MCP lists keep the full-width one) spanning the available
conversation area, rather than the compact picker dialog. The header and context
picker show System prompt, Environment (neutral rail), AGENTS.md,
MEMORY.md, Skills, Tools and MCP in that order. Each header line's title uses the
agent colour and carries a `│` left rail (the same glyph and column as the user card rail, no
opacity change); inventories and previews under it have no rail. System prompt, Environment and MEMORY.md
open literal viewers using the same shared projection as the header. System
prompt contains only core instructions when known parts reconstruct the exact
complete prompt; MEMORY.md also includes memory-scoped instruction wrappers.
Clipped, incomplete or unclassified previews retain the full literal system text
rather than silently dropping context, and separate prompt-row estimates become
unavailable so that fallback text is not counted twice.

Skills open card lists (`ui/ratatui/context_sections.py`): one selectable item per
skill whose dim rows (`Item.lines`, wrapped at 76 columns) carry the frontmatter and
token figures. Enter opens the skill page (`SkillInspect`: frontmatter table and the
full Markdown body, `detail` layout). Space or a click toggles; locked after the first turn.

The MCP dialog mirrors the Tools list: a thin `list` layout (≤ 72 columns). Its first rows
are `Open MCP settings…` (the Settings → MCP page) and `↻ Refresh all`; servers follow
grouped by scope (`Global · ~/.nexus/mcp.json`, `Project · <source_path>`). Every server is
listed: one off in `mcp.json` is labelled `· off in mcp.json` with a locked toggle (a session
switch cannot enable it), and an entry that failed to parse is `<name> · invalid` with its
wrapped error and no toggle or Restart (Enter opens the settings page). Each server has dim
second rows (`<transport> · <command or URL> · N tools`, then any error). A line shows the
name (plus ` · <status>` when not connected), a right-aligned `~indexed / ~full`
token column, a `[ Restart ]` chip (click, or Ctrl+R on the row, sends `McpServerRestart`)
and the on/off toggle (Space or click; session-scoped, locked after the first turn).
Title: `MCP · N of M on [· K invalid] · ~X indexed / ~Y full` (N, M count valid servers only). "Indexed" is the server's own entry in
the frozen MCP index (split on `- server: <name> ·` lines, ~4 chars/token); "full" is
`schema_tokens`, every tool schema the server offers. The top row `↻ Refresh all` runs
`ExtensionsReload` (re-reads `mcp.json`: adds, removes, reconfigures servers), then
restarts every enabled server that is not connected (reload alone skips a failed server
until its backoff expires), then redraws in place and flashes `MCP refreshed · N servers ·
K reconnected` or the problems.

Enter on a server opens the server page (also `list`, ≤ 72 columns): a `Server` row
(status · transport · scope · server name/version; Restart chip and toggle; Enter opens a
detail page with every field, including untrusted instructions), then the
`Indexed · ~N tokens in context now` group, showing that server's index entry (Enter
opens the literal index), then `Full · ~N tokens · K tools · deferred to McpSearch|sent
every request` with one line per tool (name, ~tokens, toggle); Enter opens the tool page.
Resources and Prompts follow as their own groups. Toggling a tool there redraws the
server page, not the Tools list.

Tools opens a thin `list` panel (width `clamp(widest row + 6, 44, 72)`, where a row counts its detail, token column and action chip; height counts group headings; the token column leaves room for the chip; centred; below
44 columns it uses the default modal): one line per tool in the order sent, with the
name, a right-aligned token column (`Item.trailing`, aligned by Rust) and the toggle.
Enter opens the tool page (`tool_show`, `detail` layout: at most 88 columns, as tall as its text, Markdown): facts, full
description, a flattened parameter table and the exact schema sent. Esc returns to
the same list. Space on the page toggles the tool in place (`Snapshot.panel_toggle`; absent when locked).
Tools open with their families expanded. Each tool, skill and MCP server has a
right-aligned ON/OFF switch: click it or press Space to change the session
selection, and click the label or press Enter to inspect definitions/details.
Switches show LOCKED after the first turn and on read-only child pages; the host
still enforces that lock. These controls change session context, not config files.
Covered by native workflow, Rust layout/hit-target and controlling-PTY tests.
Live-provider and real-desktop visual verification are not claimed.

Tools and reasoning share activity groups interleaved with visible agent replies.
Each reply closes the preceding group; task/subagent cards remain separate, and so
does every file change and question (Edit, MultiEdit, Write, apply_patch or its `patch`
alias, Question: `STANDALONE_TOOLS` in `prototype.py`). Each such call is its own
`change` block, never a group with a duplicated member row: the header is the action
and path followed by `+added −removed` in diff green/red (zero counts are omitted; a
multi-file patch shows muted `N files` first; a failure appends its error in red, a
question its answer). It starts open and shows a unified diff, one column with line
numbers, added rows green on the add tint and removed rows red on the delete tint
(`diff_lines`, from `diff_unified_rows`). A Write without a diff artifact shows the
content it wrote as added lines. Only the first 12 rows show until Enter; at most 400
rows are sent, the rest announced. Calls without a diff (a question, a failed edit)
show their labelled details instead. Clicking the header folds the block.
Totals describe the work (`Ran 2 commands · Edited 2 files · Read 2 files ·
Thought 3 times`) rather than listing tool names or a separate Explored row.
Older/completed groups collapse by default. The trailing group in an active turn
previews only its latest item, announcing earlier hidden items. Click/Enter
expands the full group; another toggle explicitly collapses it even while live.
Member toggles retain all labelled parameters, results, reasoning and diffs;
`/verbose` reveals everything. Group identity follows its first durable member.

The transcript auto-scrolls only while the reader is at the bottom: scrolling up
(wheel, PageUp) stops following, and any scroll that lands back on the last row
(wheel, PageDown, Down) resumes it. Typing, sending and Ctrl+End also return to the live end.

Settings → Layout → **Centered conversation** (`centered_layout`, off by default, saved
in `tui.json`) caps the conversation column (top bar, transcript, composer) at
`render::CENTERED_WIDTH` = 80 columns, centered between the sidebars; narrower
columns are unchanged. Dialogs that live in the transcript center with it.
Covered by Python projection and Rust disclosure tests; live-provider visual
verification is not claimed.

Transcript rows follow the OpenCode-style layout. Prompt cards keep the left
rail unbroken on every row; the fold chevron (`▾` open, `▸` collapsed) sits in the
left margin outside the card in the border colour, barely visible, and the row
still toggles the turn. `Thought: 671ms` is amber behind a faint left rule; the duration
is derived in the reducer from the `thinking.delta`/`thinking.end` event
timestamps (`BlockView.elapsed_ms`, so replay reproduces it), and the reasoning
opens beneath its activity member dimmed, with a whole-line `**heading**` shown bold.
Expanded activity members show their full heading (`✱ Grep …`, `→ Read …`). A subagent
call is one row, `<spinner|✓> Explore Subagent — short phrase · model (effort) · time`,
with elapsed time after the model/effort only once finished, derived
from the child's durable spawn/completion timestamps. Running subagents do not
show a duration. Missing
timestamps omit the duration rather than inventing one. The tick uses the muted
tool tone. A subagent page opens with its task as a
prompt card whose rail uses the agent's colour (no context-header strip, so the
child's context chips are not shown there). The dark background is `#0B0B0B`.
Covered by Rust transcript tests and `tests/test_ratatui_projection.py`; not
verified in a real terminal or against a live provider. The bottom
Subagents/Shell/Terminals panel from the reference screenshots is not ported.

Transcript column: the first character of every row is aligned with the first
character of the prompt text (column 5): the reply text, thoughts (rule), tool
rows and groups (`→`/`✓`), expanded members, subagent rows, agent labels, errors
and the context-header glyphs. The context header is a vertical list, one chip per
row: the glyph `◈` in the block's colour, a bold title, a dark-grey dot leader, counts, and right-aligned token
figures, all rows padded to one length with a blank row between them; each row is its own click target. Hovering a clickable context row changes its background to dark grey (the light theme uses its matching highlight tone). The highlight follows the same column ranges as clicking, clears outside those ranges, and is suppressed behind dialogs. Diffs and the empty-session hints keep their own layout.

List dialogs (model picker first) use a borderless panel: bold title with a
dim `esc` at the right, a `Search` row, purple group headings, the selected row
as a solid accent bar, and `●` plus an accent name on the active choice. The
model picker is a centred modal titled `Select model`; each row is the model
name once followed by a provider discriminator on a single line. Ctrl+I opens
read-only details for the highlighted model, including its full original metadata;
missing token limits and pricing are explicitly `unknown`. Escape returns to the
picker without selecting a model. Sort, favorite, refresh and details keys sit
on the bottom row (`panel_hint`). Agent model/fallback pickers share compact
names and Ctrl+I details. The `Search` box matches the
label and the hidden `provider/model` reference, so a provider name such as `open` finds
OpenCode Go (the browser and desktop clients already did this; the native filter
previously matched the label only). Not verified visually in a
real terminal; the `Free` price tag and the "Connect an integration" action from
the reference are not ported.

The composer card (background `#1E1E1E` in the dark theme, its own `composer`
palette colour; light keeps the panel tone) has a heavy box-drawing `┃` rail in the agent colour with a sliver of
background before the card, one padding row above the editor and a half row below
the controls (a `▀` row: card colour on top, background below, so the card ends
halfway and leaves a half-row gap above the path; the `┃` rail ends with `╹`
(heavy up: same stroke, top half) there, so it stops exactly at the card's edge); the workspace line sits under the card on the plain background. One
blank row always separates the last editor row from the agent/model controls,
however many rows the draft has (`composer_keeps_a_blank_row_above_the_controls`).
A blank background row always sits directly above the card as a margin; queued
messages and the attachment line get their own rows above that margin.

`nexus chat` launches the most recently modified native executable among the one
beside the interpreter, `rust/tui/target/{release,debug}` and `PATH`, so a fresh
`cargo build` is used on the next launch; `NEXUS_TUI_BINARY` overrides this.

Launch order: `nexus chat` starts the native client first (`ui/ratatui/run.py:spawn`), then
connects to the daemon. Until the first snapshot arrives the client draws a splash (the word
"Nexus", centred) and ignores input except Ctrl-Q and resize (`loaded` in `rust/tui/src/main.rs`).
The first snapshot needs only the session bootstrap and the context preview. The git branch
(`doctor`), catalogue display names (`list_models`) and first-run `setup_status` run
concurrently afterwards (`finish_startup` in `prototype.py`) and each repaints when it lands, so
the breadcrumb first shows the bare workspace path. Measured with a warm daemon over a PTY:
splash about 0.2 s, full UI about 0.3 s (was about 1.0 s); cold daemon start not measured.

The workspace/branch/worktree/status bar uses the black conversation background,
with its path starting at the composer card's `┃` rail. It sits below the composer controls and
directly above the activity meter. Session tabs remain at the top when the
sessions sidebar is hidden; the workspace bar's details and update click targets
move with it.

User message cards stop one cell short of the column's right edge (an unstyled
margin cell; the hover Copy button stays inside the card) and have one blank row of panel-colored padding above and below
their content, including collapsed turns and messages with attachments. Padding
retains the card's left rail and turn-toggle click target.

Agent replies have one additional column of left padding throughout, including
tool counts, expanded tool details, and nested diffs. Wrapping reserves that
column; nested content receives the padding only once.

The composer border and agent name share the active agent's context-header
color, including configured identity colors. One blank line precedes the
System prompt header, matching the spacing between context blocks.

**Shell mode indicator.** A draft starting with `!` (`render::is_shell_draft`) is sent
unchanged as a normal `submit`; the daemon runs the rest as bash. While it is
typed the composer rail turns the palette `warning` amber (both themes), and the
blank row above the controls shows a ` ! bash ` tag plus "Enter runs in the
workspace · output goes to context". A draft of exactly `!` shows the placeholder
`!type a bash command`. The indicator uses the existing blank row, so layout and
control hit-testing are unchanged. Shell drafts never open slash or `@`
completion (`main.rs`). Covered by `shell_drafts_show_a_bash_indicator_and_warning_rail`.

Parallel tool calls render exactly like standalone calls: no `┌│└` or `∥` gutter
markers, so every tool row shares one text column. Verified with Rust column tests, Python projection/PTY tests and native
browser screenshots. The web browser suite is not verified: its existing
`wait_for_function` fails against the page's Content Security Policy before
reaching tool rows.

`nexus chat` uses Ratatui as its sole terminal client. Missing executables report
install/build guidance. The following ledger records native behavior and remaining
verification gaps.

New-session actions (`/new`, named `/new`, and Ctrl+N) open the session
immediately, without an agent picker. They capture the current/last-active
root agent before switching and persist that selection through the host in
the new session; `/agent` remains the explicit way to choose another agent.
Covered by native action regression tests.

New-session actions (`/new`, named `/new`, and Ctrl+N) open the session
immediately, without an agent picker. They capture the current/last-active
root agent before switching and persist that selection through the host in
the new session; `/agent` remains the explicit way to choose another agent.
Covered by native action regression tests and Textual functional journeys.

| Area | Implemented | Remaining verification or work |
| --- | --- | --- |
| Host/reducer | Shared session controller, replay, continuous follow, bounded automatic reconnect, cross-project routing | Reconnect and project switching under real daemon churn |
| Transcript | Ordered turns (prompt card with `▼ … #N`, thought, `◆` agent label, reply, tool rows with batch gutters, right-aligned footer) with collapsed margins computed in Python (`gap`) and drawn by `rust/tui/src/transcript.rs`; word-boundary wrapping; split diffs, child-agent pages | Task/subagent card header, inline diff line numbers, long-history performance |
| Context | Header opens the transcript as labelled chips with token estimates, provided by `ui_support/context_header.py`; clickable sections, extension toggles and locks, context/activity meter, context-usage modal and Markdown AGENTS.md | Side-by-side check of a populated header |
| Composer | Grapheme editing, selection, undo/redo, multiline movement, persisted history, paste, completion requests via shared `ui_support/completion.py` (visible commands sorted, `@` files limit 30, `/model` `/agent` `/effort` `/theme` `/export` `/voice` `/sessions` `/attach` arguments, case-insensitive prefix) | Word wrapping at cursor; completion menu now supports keyboard and mouse selection |
| Submission | Queue/steer/interrupt, returned queue restoration, failed draft recovery, keyboard negotiation, Ctrl+X leader | Supported terminal matrix and race checks |
| Prompts | Durable nested permission and question projection, disabled decisions, free text, arbitration feedback | Mouse choice focus and multiple-question journey checks |
| Navigation | Searchable pickers, tabs with open/close clicks, sidebars, cross-project sessions, archive/trash/undo, model favorites/recents; `/model` picker uses shared `ui_support/model_choice.py` (freshness filter, Favorites/Recent/Recently-updated order, atomic model+effort with preselected effort); fuzzy filtering while typing is still the Rust substring filter | Focus navigation, visual group/last-tab checks |
| Inspection | Context/tools/tasks/extensions/usage/diff/archive/export panels, explicit desktop clipboard copy | Export destination handling verification |
| Attachments | Host preparation, eight-item limit, numbered references, previews (converted documents open their preview), clipboard images, individual removal keeping numbers (a new attachment never reuses a removed number), session-change guard while converting | Verified against the real host (`tests/test_ratatui_journeys.py`): png, txt, md, pdf and docx fixtures, labels sent with enqueue, failed conversion not attached. Not verified: rendering of submitted attachment chips (owned by `timeline.py`), clipboard on a real desktop |
| Settings/providers | Scope/category editor, autosave/hash conflicts/reset/delete, default agent/model setup, login/key/code flows | Verified against the real host facade (`tests/test_ratatui_journeys.py`): nested Back stack (editor returns to its category list, lists refresh after new/delete/reset), hash conflict keeps the draft, host validation error keeps the draft, built-ins cannot be deleted and an edited agent resets to built-in, category reset lists the files it trashes, starter templates for agents/skills/mcp, API-key, Claude code, device-code, cancel/resume/logout flows, setup default selection. Not verified: Voice and Appearance/Layout reset, real browser/OAuth sign-in, Rust rendering of any of these pages |
| Voice | Bounded capture, partial previews, final-only insertion, cancellation, preparation consent | Hardware/audio runtime verification |
| Worktrees/Git | Review pages (cursor advances by whole 128 KiB diff pages, identity/digest pinned across pages), exact digest acknowledgement, integration/discard, explicit force discard, host confirmation tokens bound to child/review/digest | Verified against real `git init` worktrees through the real facade (`tests/test_ratatui_journeys.py`): multi-page review, acknowledge, integrate, cancel, clean discard, force discard, stale/forged token refusal, list after discard. Host bugs found and fixed: review digests were redacted to `***` by the facade and the list failed after a discard. Not verified: concurrent clients mutating the same child |
| Preferences | Compatible saved themes, sidebars, preview, model favorites and recents | Full theme visual checks |
| Distribution | Locked Rust build, native executable in wheel/sdist, clean macOS arm64 wheel install | Linux/macOS/Windows wheel matrix, CI, installer compatibility |

Python owns the host contract and canonical reducer. Rust owns the terminal and
receives versioned private JSONL presentation snapshots. Crossterm reads keys
from `/dev/tty`; stdin/stdout are bridge pipes. The setuptools-rust binary build
keeps the existing packaging backend. The Python bridge has no terminal widget toolkit dependency.

Tool inspection shows every labelled, redacted, control-safe parameter and
output. Session generations invalidate stale menus and forms. Worktree mutation
confirmation tokens come from the host and are replayed only after an explicit
selection. Closing the client does not cancel daemon work.

Verification includes native editor/schema/layout tests, Python workflow and
live/replay tests, a controlling-PTY submit/permission/form/quit/restoration
check, and the offline suite (4,841 passed, 312 skipped, four timing-sensitive
checks excluded). These counts describe the checked revision, not later changes.

`PYTHONPATH=. python tests/playwright_ratatui_check.py` captures native terminal
screens with deterministic events, draft input and narrow resizing through a
loopback-only PTY server. Screenshots live in ignored `artifacts/ratatui-parity/`.
Hardware voice and supported terminal/wheel matrix verification remain separate.

Native Python projection now reuses sanitized output for unchanged canonical
turn objects. The cache resets across sessions and is bounded to 4,096 entries
and 8 MiB of serialized presentation data. Expansion, collapse and verbosity
changes invalidate presentation reuse. A local synthetic 1,000-turn check
measured approximately 164 ms cold and 0.6 ms cached; streaming IPC and complete
terminal rendering performance still need measurement.


## Visual matching (2026-10-02)

The native dark theme uses explicit black for the conversation and dialogs;
other color roles follow `rust/tui/src/render.rs`, and the light theme remains available.
The four-row top bar contains tabs, a divider, workspace/status and a bottom rule.
Sessions retain two-line cards with no blank row between cards; sidebar toggle
hit targets span three columns and the first two top-bar rows. User messages have
a one-cell left inset, thin blue rules, and muted turn numbers on the card's own
background. The composer uses the same thin rule, a runtime row, and a blank row
below it. Its bottommost row shows labelled context usage/limit and activity, with a
spinner and the terminal meter's moving segment while running, context fraction
and price-tier marks while idle; clicking that row opens context usage. The repository
breadcrumb appears only at the top.

Presentation snapshots declare `panel_layout` (`modal`, `drawer`, `page`),
`panel_format` (`plain`, `markdown`) and `panel_loading`. Context meter data
(`context_used`, `context_window`, `context_marks`) and context notes are derived
from the shared context helpers. Old snapshots without a
layout retain full pages. Command, model and root-agent choices dock above the composer; context inspection
and provider usage use bounded modals.
Settings editors retain full pages. Painting and mouse hit-testing share the
same rectangles. Escape/outside click dismisses, mouse selection and wheel
navigation operate on filtered picker items, and the composer draft is retained.
Slash/file completion uses explicit palette colors and follows the actual
composer position as the editor grows.

`Ctrl+U` and `/usage` open immediately with the last fetched report, or a loading
state on the first open. A bounded background request refreshes the same modal;
`r` refreshes, errors retain cached data, and stale panel/session responses do not
replace another view. The cache holds one host report and resets across workspace
changes. `AGENTS.md` preserves its included body/newlines and source label and
uses the existing Markdown renderer; the system prompt remains literal text.
Context usage shows shared accounting and the assembled request in
labelled groups; unavailable previews still open with observed usage and an error.

### Shared native components and hover contract

`render/components.rs` provides button, toggle, section and selectable style
primitives. Composer agent/model/effort/tier controls and the context indicator
use the shared button style and the same layout ranges as their click handlers.
Mouse movement changes only local presentation: a bounded 100 ms background-color
transition, including a smooth reversal on exit. It never sends host commands,
changes focus, text, padding, hit geometry or selected state. Disabled styles
suppress hover; selected and focused styles remain distinct (accent/bold and
underline respectively). The event loop requests component redraws only while a
transition or its final endpoint frame is pending, not continuously at rest.
Filtered model/menu rows now use the shared selectable style; selected rows retain
an accent bar with a separate hover tint. Menu toggle hit zones use the shared
toggle style (locked toggles suppress their own hover). Top tab controls and
details-sidebar tabs tint their existing cells using their existing click ranges.
Panel overlays resolve their own menu hover rather than suppressing it, and do
not hover the composer/tabs underneath. Generation, panel/filter/selection and
layout changes clear stale targets; a fresh pointer movement is needed afterwards.
Prompt/completion overlays suppress underlying hover and tint their own choices
and completion rows. Settings navigation uses shared selectable styles and hover
targets; heading rows are not actionable. Form buttons, session rows and
agent/detail-page actions are not migrated yet; section primitive adoption is
also still incremental.

### Composer chrome and details sidebar

Context inventory rows share their headings' gutter and occupy at most 100
terminal cells, so wide windows do not scatter short tool names across the
viewport. Skills/MCP headings show one total count rather than unlabelled scope
pairs. There is no overflow footer, no `None included` text and no `Context total` line;
an empty block (no MEMORY.md, no MCP servers, no skills) is greyed out. Skill rows label the estimated tokens of their actual included
index entry, not the full skill body; missing entries say `tokens unknown`.

The workspace breadcrumb (`~/repo:branch`) starts at the composer card's left border, the `┃` rail
(two cells from the conversation edge), reserving its right-hand update/status target when
truncated. Only the true home directory or its descendants abbreviate to `~`;
similarly prefixed sibling directories do not. This is display-only: workspace,
session metadata and file paths retain their full values. Build uses `#5C9CF5` and
orchestrator uses orange as fallback identity colors; explicit host colors win.
Composer context metadata uses the darker quiet foreground while retaining its
hover affordance and usage meter.

The details sidebar (SESSION METADATA, CHANGES, MCP SERVERS)
and context header come from the toolkit-free `ui_support/details.py` and
`ui_support/context_header.py`; the tool row, turn footer and agent label text
come from `ui_support/timeline.py`, which the native client uses.
Metadata has labelled values and an explicit unavailable state. Changes show
file and aggregate line counts, bold basenames with quieter directories, and an
explicit empty status. Expansion previews, wheel scrolling and file-row hit
targets remain attached to the rendered rows, including after section spacing.

`tests/playwright_ratatui_check.py` captures reference, permission, picker,
panel and light-theme screens for both clients in `artifacts/ratatui-parity/`.
Not yet compared: file-row expansion in the details sidebar, the MCP refresh
control, the task/subagent card, and voice, worktree and settings screens.
Streamed snapshots are sent compactly, identical ones are skipped, and Rust
parses only the newest of a queued backlog unless an older one carries a
one-shot composer effect.

The composer grows with its wrapped content from the 8-row resting layout up to
`max-height: 22` editor rows, reserving four transcript rows when space permits
(`render::composer_height`; mouse hit-testing uses the same height).

Running tool rows animate: Python puts the private-use slot `U+E000` where the
spinner glyph goes, and Rust draws the current braille frame there and redraws
at ~8 Hz only while some block holds a slot. Cached wrapped rows keep the slot,
so animation costs no re-wrap and sends no snapshots.

Completion is requested as you type, not only on Tab: when the token at the
cursor starts with `/` or `@`, or the draft is a slash command with an argument,
Rust sends one `complete` action after a 120 ms pause (the same trigger as
`refresh_completion`). Tab still forces a request. Escape hides the
list for that token until it changes. As-you-type argument requests cover every slash command; supported choices are
provided by the shared completion helper.
Enter on a standalone `/command` runs the highlighted command ;
on an argument that is already complete it submits the draft. Completion and choice menus span the transcript width without borders.

Empty sessions show the same grey tips as `EmptyHints`
(`ui_support/hints.pick_hints`, seeded by session id): Python sends a `hints`
block (`keys\ttext` rows padded to equal widths) and Rust centres it. Typing
blanks the rows without moving the layout; the first turn removes the block.

Modified-file rows in the details sidebar expand on click: Rust sends
`file_toggle` with the path, Python keeps `open_files` (bounded to 256) and then
sends that file's diff lines (`ui_support/details.diff_preview_lines`, headers
removed, 60 lines then a clipping notice); Rust colours `+`/`-`/`@@` rows. The
sidebar scrolls with the wheel. Keyboard expansion is not implemented yet.

The sessions sidebar (40 columns, `render::SESSIONS_WIDTH`) shows the title row
(`▌ Sessions` … `+`), then **project chips** pinned under it: `All` and every project
in the list, each in its color, wrapped onto at most three rows (the rest folds into
a `+N more` chip), and a rule. Below scroll `SESSIONS N`, `project · day` headings and
**one row per session**: a status glyph (spinner while working, `●` needs input, `·`
otherwise) and the title. Each heading colors the project name with a stable
per-project hue (`project_color`, hashed from the project path) and the day in cyan;
session rows carry `project`, `day`, `repo` and `worktree` beside `group` for this.
Message counts and ages are not shown per row (the heading's day dates the group).
The current session has a left bar and bold text. An `Archived · N` row under the
list (counted by the poll, `+` when more than 200) opens the archived sessions menu.

**Worktrees** group with their repository. The host marks rows whose workspace is a
linked Git worktree with `repo` (the main checkout) and `worktree` (its branch); the
project key is `repo`, else the workspace. So a repository and its worktrees share
one chip (`nexus +2wt` counts the worktrees with sessions), one color and one
filter. Within each day the main checkout's sessions come first, then each worktree
under its own heading `nexus › feat/x · Today` (the branch muted). Opening a session
still targets its worktree workspace.

Clicking a chip, or a group heading, shows only that project's sessions (`SESSIONS
n of m · project`; headings then drop the project name, keeping `› branch` and the
day); clicking the heading again, or `All`, shows every project. The `/ Filter
sessions` box on the last row (click it; type; Enter keeps, Escape clears) matches
title, id, workspace, project, branch or heading within the picked project. The
picked project is client state and falls back to all projects when it leaves the
list. The sidebar avoids symbol glyphs that terminals draw from fallback fonts
(`☰`, `⌕`, `⎇`), which can spill into the next cell; it uses `▌` and plain text.
Per-session delete is not in the native sidebar (use the right-click session
actions).

Assistant Markdown (`rust/tui/src/markdown.rs`) follows the `.timeline-assistant`
rules: headings coloured by level (accent, purple, success, warning), inline code
using only the warning foreground colour (no separate background highlight),
fences and quotes on the panel colour (fences show
their language label, quotes a `▌` bar), nested ordered/bullet lists with
hanging indent, task markers, strikethrough and boxed tables whose cells wrap inside fitted columns. HTML stays
literal and link targets stay visible.

Item 12 reuses [ratatui-markdown](https://github.com/celestia-island/ratatui-markdown)
0.3.6's `MarkdownRenderer` for code-fence borders and language labels through a
native palette adapter. License/compatibility were checked before integration:
upstream declares **MIT OR Apache-2.0**, Rust 1.74/edition 2021, and ratatui 0.29,
matching this crate's ratatui types. Cargo pins 0.3.6 with defaults disabled and
only `markdown` enabled; no copied upstream source or graphical dependencies.
The upstream line parser and character-count wrapping are deliberately not used:
the existing CommonMark event walker, grapheme/cell-width wrapper and per-block
incremental transcript cache remain in place. Copy operations retain original
source, including Markdown and control characters; only display text is escaped.
Streaming incomplete fences are rendered as code. Structured tool results keep
their existing rendering path.

Supported reply features: ATX/setext headings, bold/emphasis/strikethrough, inline
code, fenced/indented code, nested ordered/bullet lists, task markers, quotes,
rules, links (including reference links), and tables. Explicit terminal-only
fallbacks: Markdown images show `[image: alt (target)]`, including empty alt;
HTML is shown literally, never executed; Mermaid is labelled literal source,
not a dropped diagram. Unknown languages are literal labelled code. Escape and
other non-tab/newline control characters display as `\\u{hhhh}`. Syntax
highlighting, image decoding, Mermaid drawing, browser HTML/CSS and clickable
URL protocols are not enabled. Upstream JSON/tree/rich-scroll widgets are outside
reply Markdown scope; tables remain plain aligned cells, not graphical widgets.
Focused Rust tests cover these fallbacks, incomplete fences, literal code,
Unicode/grapheme wrapping, source/copy preservation and existing styling; the
full Rust suite also exercises incremental caching and structured tool outputs.

Inline file diffs under Edit and Patch rows follow a split
layout: `path (+a, -r)`, real file line numbers, removed lines tinted red on the
left and added lines green on the right (a removal and an addition pair up on one
row), long lines wrapped inside their column, hunks separated by `⋯`, and a
clipping row after 400 rows. Python sends the rows
(`ui_support/timeline.diff_split_rows`); the old before/after text is gone.
Syntax highlighting inside diffs is not done.

The tool details panel is toned `ToolDetailsScreen`: bold section
titles, dim `label: ` before each value, dim block labels and green/red/purple
diff lines. Python sends one tone per line (`ui_support/tool_details.styled_lines`,
joined it equals `sections_to_text`), Rust colours and wraps them
(`render::toned_lines`). Other panels stay plain. It is a panel over the
transcript, not a floating modal.

Tool rows are not clickable by default. Only a row whose output (Result, Summary,
Error, Progress) exceeds 8 lines gets `N lines ▸`; clicking or Enter expands the
full details in place (`block_toggle` on `<call_id>:output`, kept in `shell.expanded`).
Only a subagent row opens a page; it never expands the tool details in place (verbose mode included), and only a failed child's `Error:` line shows inline. On that page the top bar shows the child's
model, and Up returns to the parent like Escape (PageUp/PageDown/wheel scroll).
The page keeps 30% of the viewport as blank padding below the last row so tools never
sit on the bottom edge, and every scroll key clamps to the same `max_scroll` as the
wheel (no overshoot). Laid-out rows are cached per page, so Esc back to the parent
does not re-wrap the whole conversation.

The Tools dialog (click the Tools chip) mirrors `ToolsModal`: one row per tool
family and MCP server with its tool names and token estimate; a row expands to its
tools (expanded initially), and `Edit tools…` opens Settings. Each tool row has a
right-side ON/OFF switch; clicking it or pressing Space changes the session selection
(`ContextExtensionSelect`, category `tools`). Enter or a label click opens descriptions
and schemas. After the first turn switches show LOCKED; inspection remains available. Each header chip owns its own click range
(`context_chips` rows, resolved in `main.rs`), so a click opens that chip's section and
Enter on the strip opens the section menu. Skills and MCP chips show two unlabelled
counts, project then global (project greyed), enclosed together in square brackets.
Native context rows have no diamond/dot prefix: labels are immediately followed by
counts (`Tools [13]`, `Skills [1 0]`, `MCP [0 0]`) and a two-space gap before any
token estimate. The native top bar is one row, with no separator row below it.
The workspace/status row no longer repeats the details-sidebar toggle beside
`idle`; the top-bar toggle and keyboard shortcut remain available. Tool groups carry no `✓`/`✗` or `· N failed`
(the harness recovers on its own; the expanded call still shows the error output). A
subagent row has a blank row above and below and shows only its latest tool call.
Settings pages show each area's help line (`ui_support/settings_help.py`, shared with
terminal); Appearance and Layout use labels and end with `Reset to default`.
Settings has two-pane shape: a left list of areas (GENERAL: Appearance,
Layout, Keyboard; CONFIGURE: Providers, Models, Session titles, Voice, Speech, Agents,
Tools, MCP servers, Skills) beside the current page, with notes wrapped in full above
the list. Workspace, Config, Soul and Hooks are hidden for now. Left moves focus to the
area list (`▶` marker), Up/Down there switch areas, Right returns to the page; a click
also switches. Settings stays open for every operation except an explicit close
(`close_panel`, `context_show`); `Workflows.NAV_KEEP` no longer exists. Clicks and
keys are ignored while a page is loading. Models lists the default model chain and each
tier in the order the router runs it (`ModelRouter.tier_candidates`), with `in use`,
`fallback` and skipped statuses; Alt+Up/Alt+Down reorder and Delete removes. An agent
opens with a `Run on` row (Session model, Specific model, and for subagents Tier);
Specific model shows `Model` and `Fallback 1..8`, Tier shows the tier rows, and
switching mode removes the other mode's frontmatter fields (`set_run_mode` in
`ui_support/agent_frontmatter`) while keeping the discarded values for the visit.
Each change is saved at once through the host with the same hash check as the editor;
`Edit prompt file…` opens the raw file. Not verified: native PTY screenshots of the
revamp, real sign-in and real title generation. The typed-row header/groups design in
`plans/done/SETTINGS_REVAMP_PLAN.md` §3.4 is only partly built (name/value/status/scope
fields render; groups and wrapped footers are not).

Keyboard focus over the transcript: with an empty draft, Tab focuses the last
clickable block (tool row, thought, prompt card, agent card, diff); Up/Down, Tab,
Shift+Tab or j/k move, Enter or Space open what a click would, and Escape or any
other key returns to the composer. The focused block gets the raised background
and the view scrolls to keep it visible (`render::targets`; the PTY test drives it).

The Logs drawer (Ctrl+E) docks on the right, 36 columns wide like terminal's
`#logs-drawer`, with a `Logs … ctrl+e ×` title bar and a strong left border; the
transcript and sidebars shrink to make room (`Regions.logs`). Below 100 columns it
falls back to the lower half of the transcript.

Mouse selection in the transcript: press and drag highlights rows by column
(reversed colours); releasing copies the text through OSC 52 (reaches the terminal
clipboard even over SSH) and through the daemon-side desktop clipboard
(`copy_selection` action; its failure over SSH is reported, not hidden). A press
that is released without dragging is still a click on the block. Any key clears the
selection. Only the transcript can be selected; sidebars and dialogs cannot.

Hover-revealed Copy buttons (`copy_button.rs`): while the pointer is over a user
card or a fenced code block, ` Copy ` appears over the blank right end of the
block's first row (the card's top padding row, the fence's `╭─ lang` header);
over the button itself it takes the hover tint. Clicking it copies the whole user
message, or that block's literal source, through the same OSC 52 and
`copy_selection` path as a selection. A click elsewhere on the block keeps its
own action (fold, attachment inspection). Rows carry only a small
`{"kind":"copy","block","fence","operation"}` marker; the copied text is
recovered from the snapshot at click time, and the marker doesn't create or split
keyboard focus blocks. The button isn't drawn when that end of the row has content,
and it is suppressed behind dialogs. A host-folded card copies only its visible
first line.

The transcript shows a thin scrollbar on its right edge when it overflows, and
typing returns the view to the live end.

The runtime row's usage meter carries extras (`price ↑ at N` for tiered
models and the live `Thinking · …` summary). Pending steering, queued and
interrupt messages sit in a rounded `Sending next` box directly above the composer
margin (on the rail's columns). Each row starts with an `S`, `Q` or `I` badge and
ends with `↑ ↓ ✕` controls (move up, move down, remove, by durable `queued_id`
through `SessionQueueMove`/`SessionQueueRemove`); four rows show, then `+N more`.
The composer grows to fit the box; attachments sit above it. A newer release is announced
once as an info toast that expires on its own (`UpdateStatus(announce=True)`;
the host records the announced version in `~/.nexus/cache/update-announced.json`,
so restarts do not repeat it); the footer never shows it, and `/update` still names
the version and command.
There is no separate connection-status row or activity progress bar: disconnects
and errors appear as labelled notices in the transcript.

The model picker lists models under group headings (Favorites, Recent, then by
provider/date) ; headings are display-only, so selection and favourites
count models. Ctrl+S toggles `Updated ↓` and `Name A–Z`. Fuzzy ranking stays in
Python (`model_choice`), and the Rust filter is a substring match on the label.

Labelled panels read as dim `label: ` then value. `/usage` renders per-provider
limit bars coloured by tone (`ui_support/usage.usage_lines`, the same wording as
modal). `/settings` opens on Appearance, like terminal.

Dictation shows a strip above the composer like `VoiceStrip`: `● Recording m:ss`,
a level wave of the last 28 samples (kept in Rust from the `voice_level` field of
each snapshot), the live partial text and the cancel hint. Not exercised with real
audio hardware.

Context chips open read-only while a turn runs (the host previews from the durable
records; toggles are locked after the first turn). They say "unavailable" instead of
failing only when the host has no preview. Rows are menu lines, so there are no swatch colours or aligned
token column yet.

The tab row follows `SessionTabs`: sessions toggle, one tab per open session
(status glyph, title up to 24 columns, `×`; the current tab on the panel colour),
`+`, details toggle. Overflow scrolls the leftmost tabs out so the current one
stays visible. Drawing and mouse hit-testing both use `render::tab_cells`; the
toggles and `×` are clickable. The current tab reads "working" while its turn runs.

## UI polish verification (2026-10-02)

Rust rendering/layout tests and native Python regressions,
layering/docs checks and Ruff passed. The controlling-PTY check covers modal
mouse selection, outside dismissal, refresh keys, preserved drafts and the
context-row click. Browser captures under `artifacts/ratatui-parity/` use the
newly built debug binary explicitly (an installed binary can otherwise be stale).
Reviewed native wide/narrow layout, agent drawer, Markdown, cached usage with
spinner, and completion colors. This does not establish complete terminal parity.

A focused web browser check (`tests/playwright_usage_check.py`) verifies cached
refresh, errors, stale replies and dismissal. The broad web check currently
stops before usage checks because its `wait_for_function` evaluates a string
under the application's CSP; full web regression is not verified by that run.

## Native UI refinement (implemented)

The native header uses three gray rows with one black divider. Sessions show
numeric message counts and compact ages, stronger boundaries and no shortcut
footer. Sidebars retain their existing height. Root response labels and new-session
tips are removed; child-agent identities remain visible.

Composer agent, model/provider and effort controls are clickable. Agent completion,
selection and cycling filter root-capable definitions; host validation still
enforces the post-turn lock. Context figures are right aligned inside the composer
as used / reported pricing boundary / capacity and percentage. The full-width
activity meter animates without a separate status label or spinner. Pricing
boundaries use thin ticks.

Verification: 83 native Python checks, 37 Rust checks (one manual benchmark
ignored), and the real-terminal keyboard/mouse check passed. Full-height sidebar
reflow remains optional and was not implemented.

Visual verification: refreshed wide/narrow terminal, agent drawer, Markdown, usage
and completion captures in `artifacts/ratatui-parity/`; reviewed the main layout.
The workspace `.venv/bin/nexus-ratatui` was refreshed from the local build.

Native Ctrl+X, V starts capture immediately when voice is enabled and its model is
ready. Setup retains a bounded dialog when enablement or download is needed. Live
preview words resolve changed ASCII letters over three 125 ms frames, preserving
stable preceding words, whitespace and Unicode. Final transcription remains the
only inserted text. The Ctrl+X leader has no visible shortcut banner. Physical
microphone latency and real inference remain unverified.

Native dictation now previews directly inside the editable composer, at the capture
insertion position. Recording shows only a one-cell pulsing orange outline square
below the agent control; the floating waveform/status strip is removed. Typing
stops capture and also applies the typed key. Final text replaces the temporary
preview at the captured position, preserving typed suffix text. Escape keeps the
final transcript without sending; Enter sends it. These explicit stops override
`auto_send`; a capture-limit stop retains the preference. Explicit discard cancels.
The composer grows for live previews. Real microphone/model latency is unverified.

The native activity meter has two-cell side margins and an independent 60 Hz
render clock. Its moving segment eases through a six-second round trip, blending
boundary-cell colors for motion between terminal columns. Other spinner and
dictation animations retain their existing cadence. Actual terminal refresh rate
depends on the terminal and rendering load.

Context footer accounting now falls back to the host inspection when durable turn
accounting has no figures (including a new conversation). Reported durable usage
retains precedence. Missing accounting says `unavailable` rather than `?`. Opening
context inspection refreshes the cached preview. Plain/Markdown panel rows are
cached across frames and scrolling is clamped to the last viewport, avoiding
repeated wrapping of large context bodies. Build and Ruff passed; the reported
scroll crash has not been reproduced on the user's session.

The completed-turn `turn ↑… ↓…` footer reports cumulative provider usage across
the turn's iterations; the composer context meter reports the latest request's
prompt occupancy. These intentionally differ on multi-request turns.


Settings opens in a large inset modal so the conversation remains visible around it.
The native Voice section configures enabled input, auto-send, processing device
and recording duration through host Settings commands without starting capture.
Provider pages group sign-in options and connection management beneath a labelled
connection status. Model downloads remain explicitly confirmed.

Subagent cards now open a read-only conversation page using the canonical child
body, shared transcript blocks, the child's recorded context and details sidebar.
Child tool/message inspection and nested subagent navigation use the selected
child view; Escape returns to the parent, preserving its draft. Context fetching
is session/generation guarded and retries while the first request is unavailable.
Native and web transcripts display overload retry attempts and delays.

Verification for the subagent page: native terminal screenshots were
inspected side by side (`artifacts/ratatui-parity/*-subagent.png`); the real PTY
check verifies ignored typing/paste, Escape and retained parent draft. Focused
projection/workflow/reducer/provider checks and Rust tests pass. The broader web
browser check stops at its string-based `wait_for_function` CSP violation; web
transport checks and JavaScript syntax pass. Exact pixel parity and a live
provider overload are not verified.


## Composer command choices (2026-10-02)

The `/` and `@` menus always show 10 rows (fewer only when fewer exist) so the popup
never changes height while typing. Rows rank prefix matches, then substring/fuzzy,
then typo matches (adjacent swap or one substitution, so `/mew` finds `/new`), then
padding. Python (`ui_support/completion.rank_menu`) serves desktop and `@` files; the
TUI ranks slash commands locally with the same tiers (`rank_commands`).

Native slash/file completion and command choice menus use the full transcript
width directly above the composer, without borders. Agent, model, effort and
follow-up operation menus remain in this dock; Settings retains its navigation
modal. Every slash argument requests completion after the existing 120 ms pause.
A bounded 32-request cache filters matching ancestor results while replies arrive,
including on backspace; command/token context and session generation isolate
cached candidates. Exact host replies retain the host's search ordering/results.

Verification: Rust regressions, native Python checks, controlling-PTY check and
browser captures for completion, agent and model choices. The native captures
were inspected. terminal/web menu placement has not been changed or verified.

### Remembered model effort

Model picks reuse the last explicit effort for that provider/model (including
Default) without another effort prompt. `/effort` changes the remembered choice;
preferences survive daemon restarts (see [models.md](models.md)).

## Native redesign (2026-10-02)

`plans/RATATUI_REDESIGN_PLAN.md` supersedes the earlier native composer and
sidebar layout descriptions. This presentation is native-only; shared grouping
helpers keep canonical projection behavior.

Sidebars occupy the full terminal height: Sessions is 30 columns, details is 40,
and both fit from 130 columns. Below that, the last opened sidebar wins; the
right sidebar overlays the conversation below 100 columns. Preferences survive
resizing. Sessions pins its heading and filter, marks open tabs with `◦`, and
removes the redundant center tab row while visible. Details pins Session, Files,
MCP and Logs tabs. Brackets change tabs when focused, `c` copies the session ID,
and Escape closes the narrow overlay. Ctrl+E opens Logs. Logs pins daemon/client
identity, follows the tail, folds routine entries, and polls only while visible.

The composer has no reserved blank context row. Its controls end in ten slanted
context bars and only reported used/window/tier figures; missing data is `?`.
Click the figures or press Ctrl+X,C for a context summary, then Enter for full
context. The context header opens a section picker. Preview information is
cached with an as-of timestamp during a running turn, and agent selection refreshes
it centrally; accents follow the selected agent even before a fresh preview.

Consecutive tools and thoughts form stable native activity groups, interrupted
by visible replies or task/subagent boundaries. Groups report activity totals
and running status. Single-item groups use the same summary (`Read 1 file` or
`Ran 1 command`); parameters and results stay behind member expansion, not in
the group heading. Only the latest active group auto-previews its latest member.
Members reveal labelled parameters/results and independently folded output;
`/verbose` reveals all details. User cards put their chevron in the left margin (column 0) and
text in column 5, the common transcript column. The turn footer (turn, model, calls, tokens) has one blank row above it and one below
(the next turn's card gap, or the composer margin after the last turn).

The live bridge uses schema 2 suffix patches (`blocks_from`) and block revisions;
schema 1 full snapshots remain readable. Patches are applied in order and cannot
be dropped like full snapshots. Rust caches wrapped block parts and draws only
the visible indexed range. Optional `NEXUS_TUI_TRACE=1` records bounded latency
samples and presents percentiles in Logs; `NEXUS_TUI_TRACE_FILE` selects the
exit report. Trace files contain timings, not conversation bodies. See the plan
ledger for measured limits, including the remaining streaming CPU budget gap.

### Empty composer cursor

The native composer retains its accent-colored insertion marker before the
placeholder when the draft is empty, including after deleting or sending text.
Native editor insertion markers use a solid one-cell block (`█`) rather than a
thin line, including composer, filter, and answer fields.
The placeholder uses muted text; typing continues through the normal grapheme
editor and wrapping path. This is a rendered cursor, independent of terminal
cursor visibility settings.

Native unsent drafts are retained by workspace/session while the client is open:
switching tabs restores text, cursor, selection and undo state, alongside pending
attachments and their stable numbering. This is not restart-persistent storage;
prepared attachments retain the daemon's existing expiry policy.
Pasted/attached images insert `[image N]` references (documents use `[document N]`).
The editor treats valid references as atomic navigation/deletion units; removing
one excludes its payload from submission, and undo restores it. Up to eight
attachments may be included in a message. The model receives the original bytes
alongside a labelled reference, not merely the marker text.

Attachment More info uses `ratatui-image` for a resized native preview with name,
media type and byte size. Image bytes come only from the daemon's bounded
`AttachmentPreview` command, never a surface filesystem read. Rendering uses the
library's non-query picker (half-block fallback) to avoid competing with the input
thread for terminal capability replies. Decode dimensions/allocation are bounded;
an undecodable image shows a labelled notice. Real graphics-protocol terminal
rendering has not been verified.

Normal sends (Enter, including attachment-only sends) steer an active turn at
the next model step after the current operation; idle sends start a turn.
Ctrl+Enter explicitly queues a new turn; Alt+Enter interrupts. Bridge submit
actions that omit `mode` also default to steering.

### Shared desktop bridge attachment actions

The operation allowlist includes `chip_operation` recursively alongside block and
output operations. This lets the GPUI client inspect submitted attachment chips
using the existing message page; it accepts only operations projected for the
current view. Covered by `test_projected_attachment_operations_are_allowed_recursively`.

Forms also project a defaulted `can_delete` capability for settings-file targets.
The desktop shows delete/reset only where the shared host workflow supports it;
confirmation and built-in-default protection remain in that workflow.

## Desktop image presentation shared seam

The GPUI launcher opts into bounded inline-image projection; Ratatui does not
fetch draft thumbnail data. Submitted-message inspection now replaces embedded
base64 with labelled media/size information and exposes each image as a projected
preview operation. Both clients retain complete text and other content blocks.
The daemon remains the source of draft image bytes. See [desktop.md](desktop.md)
for bounds and native capture evidence.

## Sole terminal renderer

The legacy terminal client, widgets, dependencies and browser fixtures have been
removed. Ratatui is the only `nexus chat` renderer; `auto` remains an alias. A missing
executable reports build/install guidance. Shared controller, context, history and
workflow helpers remain toolkit independent. Native screenshots use a loopback
PTY server with vendored xterm assets, without a legacy browser-server dependency.


## Local transcript disclosure and responsiveness

The live terminal supplies complete, control-safe tool members, parameters,
results, diffs and thought bodies even when hidden. Existing result clipping stays
announced. Output folding uses a line boundary instead of duplicating full and
clipped bodies. Rust owns group/detail/output, thought and turn disclosure;
mouse and keyboard activation redraw without an action to Python. Choices persist
for the client lifetime, with 4,096 LRU entries per session/page and at most 32
scopes. `/verbose` temporarily reveals all content; disabling it restores choices.
Only the newest two turns of a page start open: every older turn that has a fold handle
(its user row) starts folded, including when a new turn arrives mid-session. The rule
lives in Rust (`Disclosure::sync_window`), so it sends no action; an explicit open or
fold of a turn always wins over the default and survives the window moving. Turns
without a user row are never folded, since nothing could reopen them. A folded turn shows
its prompt's first line (ending ` …` when more was hidden) and, beneath it in the muted
color, `N tools · tokens · model` (`_fold_summary`; token and model parts with no data are omitted), drawn outside and below the prompt card, aligned with its text.
Fold choices live only in the running client and are not saved anywhere.
Desktop retains its existing projection and wire encoder.

The terminal writes schema 3: omitted sections are unchanged, explicit empty/null
values clear them, `reset` establishes a session/page, and `blocks_from` replaces
an ordered transcript suffix. Schema 1/2 remain readable; unknown schemas fail.
One-shot insert/restore fields are never inherited by omission. History is sent
initially, with appends supported thereafter. Static slash-command names/aliases
complete locally on the next frame; file and argument completion retain 120 ms
host debounce. Slow independent actions run in at most eight tracked tasks;
completion and session-bound replies have stale guards. Submit/cancel/answer and
operations remain ordered.

Host events are ingested immediately; ordinary updates coalesce over 16 ms. User
updates, permission/question tool requests, disconnects and one-shots flush
immediately. Ordinary root text/thought deltas reuse the historical block prefix;
other events and child pages use full projection. Unchanged tool formatting is
cached separately (1,024 entries / 8 MiB). Rust borrows ordinary blocks, wraps only
disclosed content, and retains prefix/suffix row parts around a local change.
Typing invalidates only empty-session hints. Geometric offset adjustment around a
fold can still touch suffix metadata; it does not rewrap those blocks.

Sidebar visibility, details tabs, file expansion and routine-log folding apply
locally before a sequenced action is sent for persistence/data fetching. Rust
reapplies unacknowledged choices over older echoes. Preference writes debounce
250 ms in a worker thread and flush on exit.

`NEXUS_TUI_TRACE=1` adds bounded Python project/fingerprint/encode/write+drain/byte
samples alongside native timings in Logs. Native tracing includes click→frame,
event→frame and layout block counts. Recent host event timestamps are carried to
the draw; historical replay falls back to bridge-ingress time. Exit traces contain
only timings, never bodies. The Python report uses the native trace path plus
`.python`.

Verification and measurements: see
[the responsiveness ledger](../plans/done/TUI_LOCAL_INTERACTION_PLAN.md). Independent
controlling-PTY checks verify keyboard and mouse disclosure with Python silent and
preserved expansion through patches. Native closed/group/detail screenshots are
captured with a silent Python fixture. A 1 MB fully rendered output still incurs a
large first-wrap cost; viewport-only wrapping is not implemented. These checks do
not establish zero latency or live-provider timing on every terminal.

The MCP Settings page lists both scopes; each server section shows Status (with the error),
Transport, Command or URL, Defined in, Tools, Tool filters and Ignored keys, an invalid entry
shows only Status/Error/Defined in, and whole-file problems are callouts under the scope
heading. It exposes persistent On/Off and Search/All choices
through host commands with optimistic hashes and JSONC-preserving edits. The
MCP inspector displays config diagnostics, including failures with zero servers.

## Toasts (2026-10 overhaul, step 1)

Shell notices are dismissible toasts, not a transcript line. `ShellActions.toast(text,
level, key=, body=, action=)` queues one on a bounded deque (20) and every snapshot
carries the list as `toasts` (`{id, level, title, body, key, action}`); ids are
millisecond-seeded and monotonic, so the client shows each id once even across a
Python restart. `shell.notice = "..."` still works (it sets the legacy string and
raises a toast whose level comes from `notice_level`); `shell.flash(text, level)` is
the explicit form, and exceptions are `error`. A `Disconnected…` notice stays a
banner and disables the composer; the reconnect message is a success toast.
Identical text within 5 s is not repeated, so a refresh loop cannot spam. Projection
failures (`could not be shown`) remain in the transcript as durable context; shell
notices no longer produce the `Error:` line or the `notice` block.

Rust (`render/toasts.rs`, drawing and layout from the `nexus-widgets` kit) owns the
timers and hit rectangles like hover state: info 4 s, success 3 s, warning 8 s,
error 12 s, paused while the pointer is on a toast, same-key toasts merge (`×2`), at
most three visible plus `+N more`. They float at the top-right of the conversation
area over everything (dialogs included) and never reflow it; the countdown hairline is
deliberately faint. Click `×` to dismiss, click the action to run its operation.
`Ctrl+X x` dismisses all, `Ctrl+X a` runs the newest toast's action (`Ctrl+X t` stays "cycle reasoning effort"). Esc does not
dismiss toasts (it already means stop/close). Every toast is also a `client · toast`
line in the Logs tab (warnings and errors always, info and success with the routine
entries), so a dismissed toast is never lost. Not done yet: the notifications list
(`Ctrl+X n` is already `/new`; a different key is needed), toasts for the remaining
direct `shell.notice` call sites beyond the classifier, and the composer, activity
and dictation indicators are intentionally unchanged.
Covered by `tests/test_ratatui_toasts.py`, `tests/test_ratatui_pty_toasts.py` (real
controlling PTY, full-redraw absence checks) and `render/toasts.rs` unit tests.
Not verified against a live provider.

## Context header always shows its contents (2026-10 overhaul, step 2)

The header that opens every conversation has one mode. Besides the tools, skills and
MCP inventories, the System prompt and AGENTS.md rows now show a one-line preview
under their heading (the first non-empty line, clipped with an ellipsis; nothing is drawn when the
block is empty).
The preview opens the same host dialog as the heading (`context_show`), so the full
text stays one click away. Skills and MCP counts are one total of enabled entries
(not project/global). Not done: strike-through for disabled tools (the host header
projection filters disabled tools out of the inventory instead of listing them).
Covered by `render/context.rs` tests and `tests/test_ratatui_workflows.py`.

## One sessions surface (2026-10 overhaul, step 3)

`/session`, `/sessions`, `Ctrl+O`, `Ctrl+B` and `Ctrl+X b` open the same thing: the
left sessions sidebar, with keyboard focus in it. There is no separate Sessions
dialog. `/sessions` (no argument) refreshes the list and bumps the snapshot field
`sessions_request`; the client opens and focuses the sidebar when it sees a new value.
The client does not wait for that: a submitted `/sessions` or `/session` (no
argument) and the top-bar toggle open the sidebar locally at once, and the host's
refresh lands later (the list is also polled every 3 s).
`/sessions <id>` still switches directly. `Ctrl+B` opens and focuses it; with focus
already inside it hides it again.

With focus: `↑ ↓ Home End PgUp PgDn` move the selection (a raised row with a bar),
`Enter` opens the session, typing or `/` filters (title, id, workspace, group;
the first match is selected, and `Enter` while typing opens it), `Esc` clears typed
filter text and otherwise closes the sidebar, `Ctrl+C` closes it at once (drawer, or
the docked sidebar's preference, as `Ctrl+B` does), a click elsewhere leaves. Without
focus, `Esc` in the composer also closes a visible sessions sidebar before it counts
toward the double-`Esc` cancel. Keys that do not apply (Ctrl
combinations) still reach the normal handlers.

Below 90 columns the sidebar is a **drawer** (up to 40 columns) over the
conversation, never a column, and it opens only on request: it is a local flag, not
the saved `sessions_sidebar` preference (which defaults on), so a narrow window does
not cover the transcript by itself. Choosing a session or `Esc` closes it.
Covered by `render.rs` tests (regions, selection, shared filter), `tests/test_ratatui_toasts.py`
(`/sessions` request) and `tests/test_ratatui_pty_sessions.py` (real PTY, docked and
drawer). Not done yet: row actions (rename, fork, archive, multi-select) and
`Load more` paging; they need host commands and are tracked in the plan.

## One-page Settings (2026-10 overhaul, step 4)

Every Settings area is one page, not a menu with pages inside. Python builds a typed
page (`ui_support/settings_page.py`: headings, notes, rows with a control, tabs,
ordered lists, collapsible sections, buttons, tables, progress, callouts) and sends
it as the snapshot field `settings_page`; the Rust client (`settings_page/`) renders it
with the `rust/widgets` kit and owns focus, scroll, select popups, text editing and
section open state. A change is one operation sent back with `value` (and, for an
ordered list, `action` and `index`); the host applies it through the same host commands
as before and **rebuilds the page after every operation**, so what is shown is what the
host reports. The page lives and dies with the Settings panel (`panel_title`), so a
stale page is never drawn over another dialog. Text from the host is control-safe and
bounded; an unknown block type is shown as a visible note, never dropped.

Areas: Appearance, Layout, Keyboard (read-only), Providers, Models, Voice & speech,
Agents, Tools, MCP servers, Skills. `/settings` opens on Appearance. **Scope** (the
Global/Project control and per-row badges) appears only on Tools and Skills, the
per-file areas that can differ per project; MCP servers has no scope tab and shows
`GLOBAL · ~/.nexus/mcp.json` then `PROJECT · <project>/.agents/mcp.json` one after another
(each: servers as sections of labelled rows, then that scope's file rows); everything else is always
global and shows no scope. Models holds the default chain, the session-title model,
the Low/Medium/High **tabs** (each an ordered list: `Alt+↑↓` reorders, `Delete`
removes, `Add model…` opens the one allowed picker), the subagent ceiling and the
catalogue. Providers is one section per provider (connected first), with API key,
sign-in code and device code handled in place in masked fields. Voice and speech are one
page; downloads always ask first. Agents, Tools, Skills and MCP list their files with
Edit/New/Reset through the existing editor pages. Session titles moved into Models and
Speech into Voice & speech (their nav entries are gone).

Keys on a page: `↑↓` move, `←→` change a segmented control, stepper or tab (Left on a
plain row moves to the area list), `Space`/`Enter` toggle or open, `Ctrl+PgUp/PgDn`
switch tab from anywhere, `Esc` closes a popup or edit first and then Settings; the
mouse clicks any control and the wheel scrolls. Drill-ins that remain: file and agent
editors, model pickers, and confirmations; they stack on the page and `Esc` returns to it.
Removed: the old per-area menu pages, the `tier_*`/`models_default*`/`title_*`/`provider_*`
/`voice_*`/`speech_*` operations and the legacy Sessions-style Settings home.
Not done: inline agent detail (an agent still opens the agent editor page), settings
search, and scope on Models. Covered by `settings_page/` unit tests (model, input,
draw), `tests/test_ratatui_pty_settings.py` (real PTY: render, toggle, segmented, popup,
tab, reorder, area list, Escape, focus across a host rebuild) and the Python side by `tests/test_ratatui_settings_*.py` and the ported journeys. Not verified
against a live provider or a real daemon.

## Keyboard polish (2026-10 overhaul, step 5)

`F6` moves keyboard focus between the composer and the sessions sidebar (opening the
sidebar or drawer if needed). On a Settings page `?` opens Settings → Keyboard (the page
`/hotkeys` opens), and `Alt+1…9` jumps to the n-th area (`Ctrl+digit` is not delivered by
terminals). The Keyboard page lists every shortcut from the one table the app uses.
Covered by `tests/test_ratatui_pty_settings.py` and `settings_page/input.rs` tests. Not
built: a hint bar under the composer (the composer is intentionally unchanged), type-ahead
in lists, and a `?` sheet per region.

## Settings fixes after first use (2026-10)

Four defects found by using the Settings pages, each now covered by a test that fails without
its fix:

- **Nothing on a page worked.** The bridge runs only operations a snapshot offered, matched
  exactly. A control sends its offered operation plus a `value` (`action` and `index` for an
  ordered list), so every control was silently refused (the tier tabs, toggles, selects).
  `ui_support/settings_page.accepts()` now accepts an offered operation completed with a valid
  client field: the value must be an offered option, in range, or of the right type; anything
  else, another area's operation, or a stale page is refused. `prototype.operation_allowed()` is
  the gate. Tests: `tests/test_settings_page_operations.py` (every operation the client can build
  from every area's real page is accepted, forged ones are not) and the end-to-end tests below.
- **Wheel scrolling stopped early.** The scroll container re-followed the focused row on every
  frame, undoing a wheel scroll. It now follows focus only when focus changes (kit `Scroll`).
- **Settings was only ~36 columns wide** beside both sidebars, because it was sized from the
  transcript; tables and labels clipped. Settings now spans the whole window (`panel_host`).
  Tables also drop or shrink fixed columns before squeezing their main column.
- **First run called a removed menu** (`Workflows.providers`). It now opens Settings on
  Providers, whose page carries the "Choose default model" action.

`tests/test_ratatui_e2e_settings.py` runs the real Python bridge, the real native binary and a
real host (scripted model) under a pseudo-terminal, driven only by keystrokes and read through a
small screen emulator: Models opens from the host, a tier tab changes the tier, a toggle saves and
the page shows it, and the Keyboard page scrolls past its second section by wheel and by keys.
