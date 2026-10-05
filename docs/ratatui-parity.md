# Ratatui implementation and parity ledger

Session titles setting saves preserve the parent navigation history. Toggling
automatic titles or choosing a model returns to the existing Session titles page;
Escape still returns to its parent rather than losing the Settings stack. Both
paths have Python workflow regression coverage; native PTY verification remains
pending in `plans/SETTINGS_REVAMP_PLAN.md`.

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

Context-header inspection uses a large inset modal spanning the available
conversation area, rather than the compact picker dialog. The System prompt
uses the shared header projection to exclude the separately displayed AGENTS.md
when the inspected parts reconstruct the complete prompt (clipped/incomplete
previews retain their literal text rather than silently dropping context).
Tools open with their families expanded. Each tool, skill and MCP server has a
right-aligned ON/OFF switch: click it or press Space to change the session
selection, and click the label or press Enter to inspect definitions/details.
Switches show LOCKED after the first turn and on read-only child pages; the host
still enforces that lock. These controls change session context, not config files.
Covered by native workflow, Rust layout/hit-target and controlling-PTY tests.
Live-provider and real-desktop visual verification are not claimed.

Tools and reasoning share activity groups interleaved with visible agent replies.
Each reply closes the preceding group; task/subagent cards remain separate.
Totals describe the work (`Ran 2 commands · Edited 2 files · Read 2 files ·
Thought 3 times`) rather than listing tool names or a separate Explored row.
Older/completed groups collapse by default. The trailing group in an active turn
previews its latest five items, announcing earlier hidden items. Click/Enter
expands the full group; another toggle explicitly collapses it even while live.
Member toggles retain all labelled parameters, results, reasoning and diffs;
`/verbose` reveals everything. Group identity follows its first durable member.
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
name followed by its dim `provider/model` ref, and the sort, favorite and
refresh keys sit on the bottom row (`panel_hint`). The `Search` box matches the
label and the `provider/model` detail, so a provider name such as `open` finds
OpenCode Go (the browser and desktop clients already did this; the native filter
previously matched the label only). Not verified visually in a
real terminal; the `Free` price tag and the "Connect an integration" action from
the reference are not ported.

The composer card has a heavy `▎` rail in the agent colour with a sliver of
background before the card, one padding row above the editor and one below the
controls; the workspace line sits under the card on the plain background.

`nexus chat` launches the most recently modified native executable among the one
beside the interpreter, `rust/tui/target/{release,debug}` and `PATH`, so a fresh
`cargo build` is used on the next launch; `NEXUS_TUI_BINARY` overrides this.

The workspace/branch/worktree/status bar uses the black conversation background,
with its path aligned to the composer agent label. It sits below the composer controls and
directly above the activity meter. Session tabs remain at the top when the
sessions sidebar is hidden; the workspace bar's details and update click targets
move with it.

User message cards have one blank row of panel-colored padding above and below
their content, including collapsed turns and messages with attachments. Padding
retains the card's left rail and turn-toggle click target.

Agent replies have one additional column of left padding throughout, including
tool counts, expanded tool details, and nested diffs. Wrapping reserves that
column; nested content receives the padding only once.

The composer border and agent name share the active agent's context-header
color, including configured identity colors. One blank line precedes the
System prompt header, matching the spacing between context blocks.

Parallel tool calls keep the same text column as standalone calls. Their `┌│└`
markers occupy the cell immediately before that column; the snapshot sends
`batch_glyph` separately from tool text, including subagent metrics.
Verified with Rust column tests, Python projection/PTY tests and native
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

The details sidebar (SESSION, MODIFIED FILES, MCP SERVERS)
and context header come from the toolkit-free `ui_support/details.py` and
`ui_support/context_header.py`; the tool row, turn footer and agent label text
come from `ui_support/timeline.py`, which the native client uses.

`tests/playwright_ratatui_check.py` captures reference, permission, picker,
panel and light-theme screens for both clients in `artifacts/ratatui-parity/`.
Not yet compared: file-row expansion in the details sidebar, the MCP refresh
control, the task/subagent card, and voice, worktree and settings screens.
Streamed snapshots are sent compactly, identical ones are skipped, and Rust
parses only the newest of a queued backlog unless an older one carries a
one-shot composer effect.

The composer grows with its wrapped content from the 9-row resting layout up to
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

The sessions sidebar follows `SessionSidebar`: a `+ New session`
button, `SESSIONS N`, day/project headings and two-line cards (status glyph and
title; "working now", "needs input", "finished" or a message count, then age).
The current session has a left bar; a background session that advanced since it
was last viewed reads "finished". Status words come from
`ui_support/session_status.py`, shared by native clients. An `Archived · N` row under the list (counted by the poll, `+` when more than 200)
opens the archived sessions menu. A `Filter sessions` box (click it; type; Enter keeps, Escape clears) narrows the
cards by title, id, project or heading and shows `SESSIONS n of m`. Per-card
delete is not in the native sidebar (use the right-click session actions).

Assistant Markdown (`rust/tui/src/markdown.rs`) follows the `.timeline-assistant`
rules: headings coloured by level (accent, purple, success, warning), inline code
using only the warning foreground colour (no separate background highlight),
fences and quotes on the panel colour (fences show
their language label, quotes a `▌` bar), nested ordered/bullet lists with
hanging indent, task markers, strikethrough and column-aligned tables. HTML stays
literal and link targets stay visible. Fence syntax highlighting is not done.

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
Only a subagent row opens a page. On that page the top bar shows the child's
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
`plans/SETTINGS_REVAMP_PLAN.md` §3.4 is only partly built (name/value/status/scope
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

The transcript shows a thin scrollbar on its right edge when it overflows, and
typing returns the view to the live end.

The runtime row's usage meter carries extras (`price ↑ at N` for tiered
models and the live `Thinking · …` summary). Queued, steering and interrupt
messages show above the editor input-queue preview (three rows plus
`+N more queued`); the composer grows to fit them. A release notice
(`<version> available: <command>`) replaces the working directory in the footer.
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
preview at the captured position, preserving typed suffix text; Escape discards.
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
the group heading. Only the latest active group auto-previews five members.
Members reveal labelled parameters/results and independently folded output;
`/verbose` reveals all details. User cards put their chevron in the left margin (column 0) and
text in column 5, the common transcript column. Assistant footers have no preceding blank row.

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
[the responsiveness ledger](../plans/TUI_LOCAL_INTERACTION_PLAN.md). Independent
controlling-PTY checks verify keyboard and mouse disclosure with Python silent and
preserved expansion through patches. Native closed/group/detail screenshots are
captured with a silent Python fixture. A 1 MB fully rendered output still incurs a
large first-wrap cost; viewport-only wrapping is not implemented. These checks do
not establish zero latency or live-provider timing on every terminal.
