# Ratatui implementation and parity ledger

The composer border and agent name share the active agent's context-header
color, including configured identity colors. One blank line precedes the
System prompt header, matching the spacing between context blocks.

Parallel tool calls keep the same text column as standalone calls. Their `┌│└`
markers occupy the cell immediately before that column; the snapshot sends
`batch_glyph` separately from tool text, including subagent metrics.
Verified with Rust column tests, Python projection/PTY tests and native/Textual
browser screenshots. The web browser suite is not verified: its existing
`wait_for_function` fails against the page's Content Security Policy before
reaching tool rows.

The native replacement is developed in the separate `feat/ratatui-prototype`
worktree. `nexus chat` launches it by default (`--renderer ratatui`); Textual is the fallback when
the native executable is missing and stays a runtime dependency until the migration
gates below are met. This is an implementation ledger,
not a claim of verified feature or visual parity.

| Area | Implemented | Remaining verification or work |
| --- | --- | --- |
| Host/reducer | Shared session controller, replay, continuous follow, bounded automatic reconnect, cross-project routing | Reconnect and project switching under real daemon churn |
| Transcript | Textual-ordered turns (prompt card with `▼ … #N`, thought, `◆` agent label, reply, tool rows with batch gutters, right-aligned footer) with Textual's collapsed margins computed in Python (`gap`) and drawn by `rust/tui/src/transcript.rs`; word-boundary wrapping; split diffs, child-agent pages | Task/subagent card header, inline diff line numbers, long-history performance |
| Context | Header opens the transcript as labelled chips with token estimates, shared with Textual through `ui_support/context_header.py`; clickable sections, extension toggles and locks, context/activity meter, context-usage modal and Markdown AGENTS.md | Side-by-side check of a populated header |
| Composer | Grapheme editing, selection, undo/redo, multiline movement, persisted history, paste, completion requests via shared `ui_support/completion.py` (visible commands sorted, `@` files limit 30, `/model` `/agent` `/effort` `/theme` `/export` `/voice` `/sessions` `/attach` arguments, case-insensitive prefix) | Word wrapping at cursor; completion menu now supports keyboard and mouse selection |
| Submission | Queue/steer/interrupt, returned queue restoration, failed draft recovery, keyboard negotiation, Ctrl+X leader | Supported terminal matrix and race checks |
| Prompts | Durable nested permission and question projection, disabled decisions, free text, arbitration feedback | Mouse choice focus and multiple-question journey checks |
| Navigation | Searchable pickers, tabs with open/close clicks, sidebars, cross-project sessions, archive/trash/undo, model favorites/recents; `/model` picker uses shared `ui_support/model_choice.py` (freshness filter, Favorites/Recent/Recently-updated order, atomic model+effort with preselected effort); fuzzy filtering while typing is still the Rust substring filter | Focus navigation, visual group/last-tab checks |
| Inspection | Context/tools/tasks/extensions/usage/diff/archive/export panels, explicit desktop clipboard copy | Export destination handling verification |
| Attachments | Host preparation, eight-item limit, numbered references, previews (converted documents open their preview as in Textual), clipboard images, individual removal keeping numbers (a new attachment never reuses a removed number), session-change guard while converting | Verified against the real host (`tests/test_ratatui_journeys.py`): png, txt, md, pdf and docx fixtures, labels sent with enqueue, failed conversion not attached. Not verified: rendering of submitted attachment chips (owned by `timeline.py`), clipboard on a real desktop |
| Settings/providers | Scope/category editor, autosave/hash conflicts/reset/delete, default agent/model setup, login/key/code flows | Verified against the real host facade (`tests/test_ratatui_journeys.py`): nested Back stack (editor returns to its category list, lists refresh after new/delete/reset), hash conflict keeps the draft, host validation error keeps the draft, built-ins cannot be deleted and an edited agent resets to built-in, category reset lists the files it trashes, starter templates for agents/skills/mcp, API-key, Claude code, device-code, cancel/resume/logout flows, setup default selection. Not verified: Voice and Appearance/Layout reset, the default-agent/fallback-model form fields of Textual's agent editor (native edits the raw file), real browser/OAuth sign-in, Rust rendering of any of these pages |
| Voice | Bounded capture, partial previews, final-only insertion, cancellation, preparation consent | Hardware/audio runtime verification |
| Worktrees/Git | Review pages (cursor advances by whole 128 KiB diff pages, identity/digest pinned across pages), exact digest acknowledgement, integration/discard, explicit force discard, host confirmation tokens bound to child/review/digest | Verified against real `git init` worktrees through the real facade (`tests/test_ratatui_journeys.py`): multi-page review, acknowledge, integrate, cancel, clean discard, force discard, stale/forged token refusal, list after discard. Host bugs found and fixed: review digests were redacted to `***` by the facade and the list failed after a discard. Not verified: Textual's `cursor + len(diff)` paging still differs from the native page-unit cursor; concurrent clients mutating the same child |
| Preferences | Compatible saved themes, sidebars, preview, model favorites and recents | Full theme visual checks |
| Distribution | Locked Rust build, native executable in wheel/sdist, clean macOS arm64 wheel install | Linux/macOS/Windows wheel matrix, CI, installer compatibility |

Python owns the host contract and canonical reducer. Rust owns the terminal and
receives versioned private JSONL presentation snapshots. Crossterm reads keys
from `/dev/tty`; stdin/stdout are bridge pipes. The setuptools-rust binary build
keeps the existing packaging backend. No Textual import is required by the
native runtime.

Tool inspection shows every labelled, redacted, control-safe parameter and
output. Session generations invalidate stale menus and forms. Worktree mutation
confirmation tokens come from the host and are replayed only after an explicit
selection. Closing the client does not cancel daemon work.

Verification includes native editor/schema/layout tests, Python workflow and
live/replay tests, a controlling-PTY submit/permission/form/quit/restoration
check, and the offline suite (4,841 passed, 312 skipped, four timing-sensitive
checks excluded). These counts describe the checked revision, not later changes.

`PYTHONPATH=. python tests/playwright_ratatui_check.py` captures both actual
terminal clients with the same recorded events, browser size and font, then
checks draft input and narrow resizing. It uses a development-only PTY adapter
for the existing browser terminal server. Screenshots live in ignored
`artifacts/ratatui-parity/`. The first comparison exposed ordering, tool density
and composer framing differences; those have been adjusted. Full visual parity
is not verified.

Completion gates: finish the remaining interactions, verify the supported
terminal and wheel matrix, compare representative permission/settings/voice/
worktree screens, measure long-history and streaming performance, then switch
`nexus chat` to native and remove Textual from runtime dependencies. The old
client currently remains available for reference checks.

Native Python projection now reuses sanitized output for unchanged canonical
turn objects. The cache resets across sessions and is bounded to 4,096 entries
and 8 MiB of serialized presentation data. Expansion, collapse and verbosity
changes invalidate presentation reuse. A local synthetic 1,000-turn check
measured approximately 164 ms cold and 0.6 ms cached; streaming IPC and complete
terminal rendering performance still need measurement.


## Visual matching (2026-10-02)

The native dark theme uses explicit black for the conversation and dialogs;
other color roles follow `ui/tui/theme.py`, and the light theme remains available.
The four-row top bar contains tabs, a divider, workspace/status and a bottom rule.
Sessions retain two-line cards with no blank row between cards; sidebar toggle
hit targets span three columns and the first two top-bar rows. User messages have
a one-cell left inset, thin blue rules, and muted turn numbers on the card's own
background. The composer uses the same thin rule, a runtime row, and a blank row
below it. Its bottommost row shows labelled context usage/limit and activity, with a
spinner and the Textual meter's moving segment while running, context fraction
and price-tier marks while idle; clicking that row opens context usage. The repository
breadcrumb appears only at the top.

Presentation snapshots declare `panel_layout` (`modal`, `drawer`, `page`),
`panel_format` (`plain`, `markdown`) and `panel_loading`. Context meter data
(`context_used`, `context_window`, `context_marks`) and context notes are derived
from the shared Textual context helpers. Old snapshots without a
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
Context usage shows shared Textual accounting and the assembled request in
labelled groups; unavailable previews still open with observed usage and an error.

The details sidebar (SESSION, MODIFIED FILES, MCP SERVERS)
and context header come from the toolkit-free `ui_support/details.py` and
`ui_support/context_header.py`; the tool row, turn footer and agent label text
come from `ui_support/timeline.py`, which the Textual widgets now call too.

`tests/playwright_ratatui_check.py` captures reference, permission, picker,
panel and light-theme screens for both clients in `artifacts/ratatui-parity/`.
Not yet compared: file-row expansion in the details sidebar, the MCP refresh
control, the task/subagent card, and voice, worktree and settings screens.
Streamed snapshots are sent compactly, identical ones are skipped, and Rust
parses only the newest of a queued backlog unless an older one carries a
one-shot composer effect.

The composer grows with its wrapped content from the 9-row resting layout up to
Textual's `max-height: 22` editor rows, reserving four transcript rows when space permits
(`render::composer_height`; mouse hit-testing uses the same height).

Running tool rows animate: Python puts the private-use slot `U+E000` where the
spinner glyph goes, and Rust draws the current braille frame there and redraws
at ~8 Hz only while some block holds a slot. Cached wrapped rows keep the slot,
so animation costs no re-wrap and sends no snapshots.

Completion is requested as you type, not only on Tab: when the token at the
cursor starts with `/` or `@`, or the draft is a slash command with an argument,
Rust sends one `complete` action after a 120 ms pause (the same trigger as
Textual's `refresh_completion`). Tab still forces a request. Escape hides the
list for that token until it changes. As-you-type argument requests cover every slash command; supported choices are
provided by the shared completion helper.
Enter on a standalone `/command` runs the highlighted command (Textual's rule);
on an argument that is already complete it submits the draft. Completion and choice menus span the transcript width without borders.

Empty sessions show the same grey tips as Textual's `EmptyHints`
(`ui_support/hints.pick_hints`, seeded by session id): Python sends a `hints`
block (`keys\ttext` rows padded to equal widths) and Rust centres it. Typing
blanks the rows without moving the layout; the first turn removes the block.

Modified-file rows in the details sidebar expand on click: Rust sends
`file_toggle` with the path, Python keeps `open_files` (bounded to 256) and then
sends that file's diff lines (`ui_support/details.diff_preview_lines`, headers
removed, 60 lines then a clipping notice); Rust colours `+`/`-`/`@@` rows. The
sidebar scrolls with the wheel. Keyboard expansion is not implemented yet.

The sessions sidebar follows Textual's `SessionSidebar`: a `+ New session`
button, `SESSIONS N`, day/project headings and two-line cards (status glyph and
title; "working now", "needs input", "finished" or a message count, then age).
The current session has a left bar; a background session that advanced since it
was last viewed reads "finished". Status words come from
`ui_support/session_status.py`, shared with Textual. An `Archived · N` row under the list (counted by the poll, `+` when more than 200)
opens the archived sessions menu. A `Filter sessions` box (click it; type; Enter keeps, Escape clears) narrows the
cards by title, id, project or heading and shows `SESSIONS n of m`. Per-card
delete is not in the native sidebar (use the right-click session actions).

Assistant Markdown (`rust/tui/src/markdown.rs`) follows the `.timeline-assistant`
rules: headings coloured by level (accent, purple, success, warning), inline code
on the raised background, fences and quotes on the panel colour (fences show
their language label, quotes a `▌` bar), nested ordered/bullet lists with
hanging indent, task markers, strikethrough and column-aligned tables. HTML stays
literal and link targets stay visible. Fence syntax highlighting is not done.

Inline file diffs under Edit and Patch rows follow textual-diff-view's split
layout: `path (+a, -r)`, real file line numbers, removed lines tinted red on the
left and added lines green on the right (a removal and an addition pair up on one
row), long lines wrapped inside their column, hunks separated by `⋯`, and a
clipping row after 400 rows. Python sends the rows
(`ui_support/timeline.diff_split_rows`); the old before/after text is gone.
Syntax highlighting inside diffs is not done.

The tool details panel is toned like Textual's `ToolDetailsScreen`: bold section
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
tools, a tool opens its description and schema, and `Edit tools…` opens Settings.
Settings pages show each area's help line (`ui_support/settings_help.py`, shared with
Textual); Appearance and Layout use Textual's labels and end with `Reset to default`.
Settings has Textual's two-pane shape: a left list of areas (GENERAL: Appearance,
Layout, Keyboard, Workspace; CONFIGURE: Providers, Voice, Agents, Tools, MCP servers,
Skills, Hooks, Config, Soul) beside the current page, with the scope path and help
above the list. Left/Right or a click switch areas (not while editing a file); Escape
still steps back and then closes. An agent opens as form rows (`Model`, `Fallback 1..8`, `+ Add fallback`, `× Clear`),
each saved at once through the host with the same hash check as the editor
(`ui_support/agent_frontmatter`); models are chosen from the grouped model list, and
`Edit prompt file…` opens the raw file. The other pages are still menus.

Keyboard focus over the transcript: with an empty draft, Tab focuses the last
clickable block (tool row, thought, prompt card, agent card, diff); Up/Down, Tab,
Shift+Tab or j/k move, Enter or Space open what a click would, and Escape or any
other key returns to the composer. The focused block gets the raised background
and the view scrolls to keep it visible (`render::targets`; the PTY test drives it).

The Logs drawer (Ctrl+E) docks on the right, 36 columns wide like Textual's
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

The runtime row's usage meter carries Textual's extras (`price ↑ at N` for tiered
models and the live `Thinking · …` summary). Queued, steering and interrupt
messages show above the editor like Textual's input-queue preview (three rows plus
`+N more queued`); the composer grows to fit them. A release notice
(`<version> available: <command>`) replaces the working directory in the footer.
There is no separate connection-status row or activity progress bar: disconnects
and errors appear as labelled notices in the transcript.

The model picker lists models under group headings (Favorites, Recent, then by
provider/date) like Textual; headings are display-only, so selection and favourites
count models. Ctrl+S toggles `Updated ↓` and `Name A–Z`. Fuzzy ranking stays in
Python (`model_choice`), and the Rust filter is a substring match on the label.

Labelled panels read as dim `label: ` then value. `/usage` renders per-provider
limit bars coloured by tone (`ui_support/usage.usage_lines`, the same wording as
Textual's modal). `/settings` opens on Appearance, like Textual.

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

Rust rendering/layout tests, native Python regressions, Textual usage tests,
layering/docs checks and Ruff passed. The controlling-PTY check covers modal
mouse selection, outside dismissal, refresh keys, preserved drafts and the
context-row click. Browser captures under `artifacts/ratatui-parity/` use the
newly built debug binary explicitly (an installed binary can otherwise be stale).
Reviewed native wide/narrow layout, agent drawer, Markdown, cached usage with
spinner, and completion colors. This does not establish complete Textual parity.

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
Native, Textual and web transcripts display overload retry attempts and delays.

Verification for the subagent page: native and Textual terminal screenshots were
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
were inspected. Textual/web menu placement has not been changed or verified.

### Remembered model effort

Model picks reuse the last explicit effort for that provider/model (including
Default) without another effort prompt. `/effort` changes the remembered choice;
preferences survive daemon restarts (see [models.md](models.md)).

## Native redesign (2026-10-02)

`plans/RATATUI_REDESIGN_PLAN.md` supersedes the earlier native composer and
sidebar layout descriptions. This presentation is native-only; shared grouping
helpers do not change the Textual renderer.

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

Consecutive tools form stable native groups, interrupted by visible messages,
thoughts or task/subagent boundaries. Groups report count, status and failures.
Members reveal labelled parameters/results and independently folded output;
`/verbose` reveals all details. User cards put their chevron in column 2 and
text in column 4. Assistant footers have no preceding blank row.

The live bridge uses schema 2 suffix patches (`blocks_from`) and block revisions;
schema 1 full snapshots remain readable. Patches are applied in order and cannot
be dropped like full snapshots. Rust caches wrapped block parts and draws only
the visible indexed range. Optional `NEXUS_TUI_TRACE=1` records bounded latency
samples and presents percentiles in Logs; `NEXUS_TUI_TRACE_FILE` selects the
exit report. Trace files contain timings, not conversation bodies. See the plan
ledger for measured limits, including the remaining streaming CPU budget gap.
