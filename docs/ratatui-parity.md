# Ratatui implementation and parity ledger

The native replacement is developed in the separate `feat/ratatui-prototype`
worktree. `nexus chat --renderer ratatui` launches it; Textual remains the default
until the migration gates below are met. This is an implementation ledger,
not a claim of verified feature or visual parity.

| Area | Implemented | Remaining verification or work |
| --- | --- | --- |
| Host/reducer | Shared session controller, replay, continuous follow, bounded automatic reconnect, cross-project routing | Reconnect and project switching under real daemon churn |
| Transcript | Textual-ordered turns (prompt card with `▼ … #N`, thought, `◆` agent label, reply, tool rows with batch gutters, right-aligned footer) with Textual's collapsed margins computed in Python (`gap`) and drawn by `rust/tui/src/transcript.rs`; word-boundary wrapping; split diffs, child-agent pages | Task/subagent card header, inline diff line numbers, long-history performance |
| Context | Header opens the transcript as labelled chips with token estimates, shared with Textual through `ui_support/context_header.py`; clickable sections, extension toggles and locks, context meter | Side-by-side check of a populated header |
| Composer | Grapheme editing, selection, undo/redo, multiline movement, persisted history, paste, completion requests via shared `ui_support/completion.py` (visible commands sorted, `@` files limit 30, `/model` `/agent` `/effort` `/theme` `/export` `/voice` `/sessions` `/attach` arguments, case-insensitive prefix) | Selectable completion menu, word wrapping at cursor |
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

The native palette mirrors `ui/tui/theme.py` (dark and light). Layout follows
the Textual shell: a three-row top bar (tabs with `+` and `▐` at the right,
workspace and status, rule), docked sessions and 40-column details sidebars on
the panel colour, a tinted composer box with a blue left bar, runtime row
(agent, model, provider, effort) and a bottom row (workspace, context usage,
`ctrl+p commands`), and approvals/questions as a panel above the composer with
the transcript still visible. Pickers and panels use the dialog colour with an
accent title and rule. The details sidebar (SESSION, MODIFIED FILES, MCP SERVERS)
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

The composer grows with its wrapped content from the 8-row resting layout up to
Textual's `max-height: 22` editor rows, always leaving the transcript four rows
(`render::composer_height`; mouse hit-testing uses the same height).

Running tool rows animate: Python puts the private-use slot `U+E000` where the
spinner glyph goes, and Rust draws the current braille frame there and redraws
at ~8 Hz only while some block holds a slot. Cached wrapped rows keep the slot,
so animation costs no re-wrap and sends no snapshots.

Completion is requested as you type, not only on Tab: when the token at the
cursor starts with `/` or `@`, or the draft is a slash command with an argument,
Rust sends one `complete` action after a 120 ms pause (the same trigger as
Textual's `refresh_completion`). Tab still forces a request. Escape hides the
list for that token until it changes. As-you-type argument lists exist only for
`/model` and `/agent` (Textual's set); Tab completes any command's arguments.
Enter on a standalone `/command` runs the highlighted command (Textual's rule);
on an argument that is already complete it submits the draft. The popup styling is still approximate.

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
opens the archived sessions menu. The filter box and per-card delete are not in
the native sidebar yet.

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

The Tools dialog (click the Tools chip) mirrors `ToolsModal`: one row per tool
family and MCP server with its tool names and token estimate; a row expands to its
tools, a tool opens its description and schema, and `Edit tools…` opens Settings.
Settings pages show each area's help line (`ui_support/settings_help.py`, shared with
Textual); Appearance and Layout use Textual's labels and end with `Reset to default`.
The two-pane full-page Settings layout is not built: Settings is still a stack of
menus.

Keyboard focus over the transcript: with an empty draft, Tab focuses the last
clickable block (tool row, thought, prompt card, agent card, diff); Up/Down, Tab,
Shift+Tab or j/k move, Enter or Space open what a click would, and Escape or any
other key returns to the composer. The focused block gets the raised background
and the view scrolls to keep it visible (`render::targets`; the PTY test drives it).

The Logs drawer (Ctrl+E) docks on the right, 36 columns wide like Textual's
`#logs-drawer`, with a `Logs … ctrl+e ×` title bar and a strong left border; the
transcript and sidebars shrink to make room (`Regions.logs`). Below 100 columns it
falls back to the lower half of the transcript.

Context chips say "unavailable while a turn is running" instead of failing when the
host has no preview. Rows are menu lines, so there are no swatch colours or aligned
token column yet.

The tab row follows `SessionTabs`: sessions toggle, one tab per open session
(status glyph, title up to 24 columns, `×`; the current tab on the panel colour),
`+`, details toggle. Overflow scrolls the leftmost tabs out so the current one
stays visible. Drawing and mouse hit-testing both use `render::tab_cells`; the
toggles and `×` are clickable. The current tab reads "working" while its turn runs.
