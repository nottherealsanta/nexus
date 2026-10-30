# nexus-tui

The Ratatui terminal client for Nexus Core (Rust). It speaks only the Core HTTP
API (`/api/*` plus the `/api/event` stream, see `../docs/api.md`) and never
reads Nexus state files, SQLite or provider credentials. The one file it owns in
the Nexus home is its own preferences file, `tui.json` (see Preferences below). Old Nexus's client in
`~/repos/nexus/rust/tui` is reference.

**Status (PLAN.md Steps 12.3 and most of 12.4):** the keyboard-first shell is
usable against a real Core: workspace header, both sidebars, session view, waiting
card, composer, command palette, the settings modal and unified alerts, with an
explicit focus model, the manifest's Esc chain, mouse and keyboard parity, and
terminal hygiene. The status footer has been removed; key hints are available
in the palette and F1, and stop appears in the composer. Existing sessions
show their branch and linked-worktree marker from Core below the composer; a
project with no session shows its own branch. The composer is a soft card
(half-row `▄`/`▀` edges in the panel colour; `─` rules in mono and ASCII) whose
control row shows the agent in its colour, the catalog model name (or a pending
model change), effort, provider, and on the right the `╱` meter with
`82.0K · 41%`, explained on hover.
The first row is a band on the chrome surface (`surface.sidebar`, shared
with both sidebars; no rules between them, a `│` edge only in ANSI-16 and
mono). It holds the left toggle, the Sessions / Files / Projects icons over the
left sidebar (a column on the rail, the drawer's first row when narrow;
hovering shows the label), the session tabs, the run status at the centre's
right edge, the notification count and the right toggle. Sidebars are 34 / 36
cells from 160 columns and 28 / 30 below. `paint::scrim` dims a buffer under a
modal (see the modal recipe below). The left sidebar (D03) opens with a `⌕ Search sessions` row (`ctrl+k`, it opens the
switcher) and a scope icon (`▣` for the current project, `◎` for all projects;
click to toggle without a notification). It shows an UPPERCASE project header, `⌥ branch  ~/…/dir`
sub-groups for linked worktrees, and two-line rows (mark slot, title, time;
`⌥ branch`, `2 ↳`) ending with `+ New session`; `Settings` is pinned at the bottom. Drag its inner edge to resize it (20–60 columns);
dragging below 14 columns collapses it to a rail of one letter per session,
with search on top and `+` below, and hovering a letter shows the session's
title and place. Settings → Sidebar switches full / rail / hidden. The project
strip under the card shows the project name (bold), its path (muted),
`⌥ branch` and `checkout` (muted) or `worktree` (bold violet); `ctrl+p commands`
sits muted at the right while idle. The card has no rail and no status band.
Rows, top to bottom: the live row (while busy: `◐ Working · 12s · ↓4.1k`, with
Working shimmering), the top edge, the input at three cells in, a blank row, the
control row, the bottom edge and the strip. The control row sheds effort, then
the provider, then the meter as the column narrows; while busy its right side
reads `esc stop` instead of the meter. The focus bar at the card's left edge is
drawn only in ANSI-16 and mono. Pending inputs appear in a padded card above the
composer, marked `Q` for queued messages or `S` for steers; a held queue is
marked `Q held`.
The header holds session tabs (`×`, `+`, `alt+x`, `alt+[`, `alt+]`): the viewed tab is bold on the page surface, the others muted on the band, a child session shows `root › child` with clickable earlier crumbs. `alt+d`
(or Settings → Timeline detail) cycles the timeline between low (one line per
activity group), medium (each tool on a collapsed line, the default) and high
(every tool expanded, long output capped). Drag the left sidebar's edge to resize it.
The starting context is a list of `◆` sections (DESIGN_PLAN D10, `paint::context_rows`, one
renderer for the timeline and the new-session view): System prompt, AGENTS.md, Tools, Skills,
MCP and Child agents or Workspace tools when present. Tool, skill and MCP rows show their
on/off state; click a row or focus the timeline and use ↑/↓ then Enter/Space to switch it.
Tools use up to four columns as space allows, both before and after a message.
Locked session rows are dimmed and say `new session`. Each header keeps its estimated count
(`~2,140 tok`, characters ÷ 4) and meta, a text row gives the total and the share of the model's
window, and there is no bar. The instruction file is part of the system prompt, so the total
counts the prompt and the tool schemas only. Previews clip with `…`. After the first turn the
whole section folds to `▸ Context · 4 items · ~2.9k tokens`. The new-session view shows the same
sections under a header block (`New session in <project>`, agent, model, effort, directory and
branch, then the `Tab`/`ctrl+m`/`ctrl+e`/`ctrl+d` hints); it loads project skills and enabled MCP
servers from Core and sends draft switches with session creation. Its prompt preview is built
from the first line of the agent's prompt body, the project's `AGENTS.md` when the TUI can read
it, and the selected agent's tool list or the default tools. Counts read `~N` with "estimated"
on hover. The turn footer adds the catalog price when the model has one.

`/learn <source>` asks whether to install into this project or every project, previews the
source, then either installs an unambiguous set of new skills or opens a checklist. The checklist
uses ↑/↓, Space, `a` (all), `n` (none), `i` (invert), Enter (install) and Esc (cancel); ticking
an installed skill replaces it. Successful installs are available in new sessions. Settings →
MCP controls whether all tool definitions go into context and the 1–1000 name-listing limit.

Every overlay (palette and pickers, Settings, confirms, help, notifications) is one
modal recipe (`render.rs` `modal`, DESIGN_PLAN D13): the scrim over the whole frame, then
a borderless `surface.raised` block, 2 cells of padding left and right and 1 row top and
bottom, within 2 cells of the frame edge and at most 70% of its height. The title is the
first row inside with `esc` on the right, key hints the last row, the selected row is on
the selection band with the `▌` focus bar. Confirms default to `[ Cancel ]`; `[ Delete ]`
is in `status.error`. The `.` row menu stays an unscrimmed popover beside its row.

Settings is that modal, 88 cells wide: type to search, ↑/↓ to
move, ←/→ to change a value, Enter to act, Esc to clear the search and then
close. Sections have UPPERCASE muted headings. Rows the TUI cannot change yet are
muted and say why on the footer line above the key hints.
Consecutive diagnostics (a provider retry sequence) form one tight stack with
delays rounded to tenths.

The timeline is complete for one session: turn headers and footers, folding,
collapsed reasoning, old-Nexus CommonMark text (supported Mermaid diagrams show a clickable label to copy their source; unsupported diagrams show the source), tool-specific expansion (diffs, output
caps, raw arguments/results), child cards, context notices, diagnostics, and the
frozen starting-context section. Selecting a session loads its full stored
history after the bounded snapshot view: all messages, the starting-context
snapshot and its turn summaries, fetched together. The event log is not read.

**Turn details come from turn summaries** (`GET /api/session/{id}/turn`, Core
API 1.2; TURN_SUMMARY_PLAN.md). The TUI reads every page of them on open (200 a
page, at most 10 pages) and keeps them in `SelectedView::turns`:

| Shown | From |
| --- | --- |
| Turn footer: models, effort, tokens, cache %, duration | the turn's requests (`model`, `effort`, `usage`), `finished` |
| Tool timing and failure | `tools[]` |
| Retry stack and the turn's error row | `requests[].attempts`, `error` |
| Composer context and the sidebar's Used | the newest request with usage (`usage.input`) |
| Header "(next call)" model | the newest request's `model` |
| Sidebar Tokens and Cost | the session ledger (`usage`), which survives pruned turns; a `Since <date>` row when it began after the session |

A stored turn covers the messages with `seq` after its `start_seq`; a prompt's
timeline turn gathers every stored turn that starts inside it (continuations,
notice turns). Usage Core does not know is shown as `usage unknown`, never as
zero. Messages below `floor.seq` (pruned turns, or history from before turn
records) have no summary: the row "the turns above have no recorded details"
says so where they end. A failed `/turn` read is announced at the top.

Live events still arrive over SSE. The running turn's events are kept as
scratch (`SelectedView::scratch`): deltas are folded into the streaming text,
raw wire traffic is dropped, and a `wire_request` keeps only its model. A
`turn.summary` event replaces the turn's summary (and its share of the ledger)
and clears the scratch. After a dropped stream resumes from its cursor, the
supervisor also re-reads `/turn?after=<newest closed turn>` (following
`more_after`). Model, effort and agent
pickers, session export and a directory change are wired.

### Markdown and diffs (DESIGN_PLAN.md D08, D09)

`markdown.rs` draws assistant text. Headings are bold: H1 and H2 in
`text.primary` (H1 with a muted `─` underline as wide as its text), H3 and
below in `text.secondary`. Inline code is `text.primary` on `surface.panel`,
padded by one space, never accent. A fence has no background, framed as in old Nexus: a `╭─ language` header and a `╰─`
footer in `text.muted`, each code line behind a muted `│ ` and drawn in
`status.warning` (ASCII: `+-` and `| `). A focused or hovered fence shows `copy` at the right end of its header
(a click copies the source). Lists use `•` and `◦`; block quotes sit behind a
`│` in `border.strong` with `text.secondary` text; links are `status.info` and
underlined, followed by `(url)` unless the target is a file path; task items
show `[✓]` (success) or `[ ]`. Mermaid is unchanged. Not done yet: OSC 8
hyperlinks (the buffer has no cell for escape sequences) and `path:line` links
that open the file view.

`diff.rs` is the one diff renderer, for edit, write and patch tool details and
for the Git diff view. It parses `git diff` text, `apply_patch` text, an
old/new pair (no line numbers: the position is unknown) or new file content
(numbered from 1). A column of 110 cells or more gets the split view: old
number and text, a `│` rule in `border.strong`, new number and text; removed
lines tint the left with `diff.del_bg`, added lines tint the right with
`diff.add_bg`, and the empty side of an add or delete is hatched with `╱` in
`border.default` (`/` in ASCII). Below 110 cells the unified view shows the new
line numbers, then `+` or `-`, then the text on the tint. Tinted text is
primary, context text is muted, long lines wrap inside their column (wide CJK
characters included), hunks are separated by a muted `⋯ @@ … @@` row, and a
diff with several files gets a `path  +a −r` header per file.

The Files tab (DESIGN_PLAN D16) is a lazy tree under a root header: the checkout's name in
capitals (bold) and `⌥ branch` muted at the right. With several checkouts the header shows `▾`
and a click or `files.root_toggle` switches between the project and the open session's
worktree. Folders expand one level at a time, each level has a `│` guide in `border.default`,
folders are bold, ignored entries are muted and can be hidden, and Git status letters are
right-aligned and coloured (`M` warning, `A` and `?` success, `D` error). Enter opens a
read-only file view on the page surface: the header row has the `~` path (the basename bold)
and `lines 1-38 of 38` muted at the right, a blank row follows, then right-aligned line numbers
and a `│` gutter; Esc returns to the timeline. The selected file can be copied as an absolute
path, inserted as `@path`, or opened in `$VISUAL`/`$EDITOR` with the terminal suspended. In the
file view `/` opens the search on the bottom row (always reserved, so the text does not move):
`/ query  3 of 12   n next · N prev · esc end`. Matches are on the selection band and the
current one is reversed; typing filters, Enter or `n` goes to the next match and `N` to the
previous one, and the first Esc ends the search while the next closes the file. Core serves
whole files up to 1 MiB and has no offsets, so the header gives the exact range shown. The
file view has no syntax highlighting yet.

**Editor (K10).** `e` in the file view starts the editor over a `Composer` buffer (cursor at
the top, `ctrl+s` saves, `ctrl+z`/`ctrl+y` undo and redo, Tab inserts four spaces); the header
says `editing`, `● modified` or `changed on disk · reload`. Save is `PUT …/file` with the
revision the file was read at; a stale write keeps the text. `editor.reload` (palette) re-reads,
asking first when dirty; Esc leaves a clean editor and asks before dropping changes;
`files.changed` reloads a clean editor and warns a dirty one. Opening another session or a Git
diff is refused while the text is unsaved.

**K5-K11 client parts.** The session switcher (`ctrl+k`) also lists sessions whose user messages
match (Core's `/api/search`) after the title matches, with the snippet in the hint column. A
session opens with its newest 200 messages; Enter on `older messages are not loaded` (or the
palette's Load older turns) reads the page before the oldest. A running session loads every older page on its own, and pages already read stay cached across snapshot resyncs. The approval card offers `y` allow
once, `a` allow for the session, `n` deny, `d` deny with the composer's text as the note.
Choosing a model or agent for an existing session first asks `…/switch-cost` and confirms when
the cache would be lost. `/usage` shows plan limits (`GET /api/usage`). A tool result's images
are named under its output (`▣ image name · mime · artifact id`); a terminal cannot show them.

Responsive layout (D17, `layout::Breakpoint`): from 160 columns both sidebars are docked at 34
and 36 cells; at 120-159 they are 28 and 30; at 100-119 the left stays docked and the right is
closed until opened (the toggle docks it; a saved preference wins); below 100 both are drawers
of 34 and 36 cells over the page, which `paint::scrim` dims (a click on the scrim closes the
drawer). Under 100 the header keeps the two toggles, the run status and the bell, and the tab
strip gives way to a breadcrumb of the viewed session cut from the left (`… › child`).

States (D18): the connecting screen and the empty workspace show the splash logo
(`paint::LOGO`, five rows of `_/` strokes, accent, bold in mono, centred as one block) over the
title. An empty workspace reads `No projects yet` with `alt+3 add a project · ctrl+p commands`
(the manifest has no key for adding a project yet, so the hint names the Projects tab). A lost
stream or an unreachable Core shows `✕ offline` in the header, the banner `Disconnected from
Core · retrying in 4s` with retry, and the composer placeholder `Reconnecting to Core… your
draft is kept`; with nothing loaded the page says `Not connected to Core`. The banner's `r`
key of the mock-up is not bound (a bare letter would clash with typing), so it shows `retry`
only.

Alerts: a session that waits for approval or an answer is announced with a
terminal notification (OSC 9, or OSC 777 in foot, kitty, Ghostty and WezTerm),
only while it is out of view. Settings › Notifications › Terminal notifications
sets off, waiting only (the default) or waiting, done and errors (a finished
turn or a failed session too). Terminal focus is not tracked, so a session in
view never notifies.

The right sidebar (D12) has SESSION, CONTEXT and GIT sections: UPPERCASE bold muted
labels, a 9-cell key column and values truncated in the middle. CONTEXT shows the
`╱` meter with its percentage, `82k of 200k`, `12k in · 2k out` and Cost, each
reading `not reported` where Core gives nothing (PLAN.md Step 12a). The GIT section groups changed files into Staged, Unstaged and
Untracked from Core's `/api/session/{id}/git` (`staged`, `unstaged` and the
porcelain `status` code). A file changed in both the index and the worktree
appears in both groups. Click a row, or press Enter with the right sidebar
focused (`up`/`down` move), to open its diff from `/git/diff` in the centre
column, drawn by `diff.rs` (D09, see Markdown and diffs below): split at 110
cells and wider, unified below. A Staged row shows the staged diff, an Unstaged
row the unstaged one; Untracked rows show a notice because Core's diff has no
untracked contents. Esc returns to the timeline.

Timeline rhythm (DESIGN_PLAN.md D04-D07): the user message is a band across the
whole page width on the raised surface (rail `▌` at its edge, muted or the focus
colour, the fold marker inside, `#n · time` at the right, `copy` on hover or
focus). When its text has scrolled off the top, `sticky.rs` pins a copy under
the header (three rows at most, a `░` shade row with `↑ N lines of this turn
above`); the next band pushes it up and a click scrolls back. The turn footer is
the right-aligned `▪ Agent · models · effort · tools · duration · tokens · cost`
row with no end status or continue hint: a failed turn puts its first error line
(`✕`) above, and the usage units (`i`, `o`, `r`, `c`) are fainter than their
counts. Durations read `0.4s`, `4.2s`, `1m 05s`, `1h 02m`. Activity groups fold to
`Read 2 files, ran 1 command` with `+N −M` and `✕ N failed`; a live group is
`Working` (shimmer) over its newest five members and `… N earlier`, and its glyph
flashes the accent for six frames when it finishes (not with reduced motion).
Members lead with a verb glyph (`→ ← $ * λ ? ↳ ↗`, ASCII `> < $ * L ? ^ ^`,
from `paint::verb_glyph` and `[glyphs.verb]` in `theme.toml`) that the status
glyph replaces while running, failed, denied or waiting. Opening a member shows
the old box (`┌ │ ╰` in `border.strong`): the command's first two rows, the last
eight output rows behind `↑ N earlier lines · enter show all` (a diff or the raw
view keeps a head and a tail), and a `raw` toggle on the head row. `o`, or Enter
on the open tool, shows all; `r` raw and `i` inspector work on a focused tool.
Reasoning reads `▸ Thought · preview` (or `◇ Thought` within an activity tree,
ASCII `o Thought`; no duration, see Interface notes) and
stays open with its live text while it streams. The header status reads
`◆ waiting for you` or `✕ offline`; it has no busy state.

The right sidebar's Session
info totals turns, calls, tools by name, tokens, a context bar against the
model's window and the catalog cost. Right-click (or `m`) on a sidebar row opens
its action menu; the Projects tab ends with `+ Add project` (an inline path
field) and removal asks first. Settings shows the catalog size
and source and the loaded agents, and says why rows that need a Core setting
cannot change.

Still Step 12.4: the built-in editor, worktree management in the UI, `path:line`
links, settings editing beyond the wired sections, and steering/queueing
(blocked on Core, Step 12a). Unavailable commands are listed disabled with their
reason.

## Build, run, test

If the repo launcher is installed (`docs/development.md`, “Shortcuts”), run
`nexus` or `nexus tui` from anywhere. Otherwise:

```sh
cd nexus-tui
cargo build
cargo test
cargo clippy --all-targets -- -D warnings
cargo fmt --check

cargo run                  # the TUI (starts Core if needed)
cargo run -- --once        # no UI: print connection state + sessions once live, then exit
cargo run -- --dump-frame 80x24,120x36 --open first --out DIR
                           # no TTY: render the real shell against the real Core to text files
# also: --svg (write <name>.svg, real colours), --keys "ctrl+p down enter /mo"
# (press keys first, space separated), --name NAME (file stem)
tools/shots.sh ROOT OUT    # node-free: seeded Cores -> standard shots -> PNG (see ../docs/development.md)
tools/bench.sh ROOT [turns] [runs]
                           # seed a ~30 000-event session in ROOT/home, start its Core and run
                           # the release build's --bench 160x48: history load, first frame,
                           # RSS, frame time while scrolling (numbers in ../PLAN.md)
```

Display options (flags override the environment): `--dark | --light`, `--mono`
(also `NO_COLOR` or `TERM=dumb`), `--256 | --16` (otherwise `COLORTERM=truecolor`
means truecolor), `--ascii` (also `NEXUS10_ASCII=1` or a non-UTF-8 locale),
`--reduced-motion` (also `NEXUS10_REDUCED_MOTION=1`: spinners stay still and an
idle terminal is never redrawn).

Session rows in the left sidebar (D03) have two lines. Line 1 is the mark slot at
x=2 (waiting `◆`, error `✕`, running spinner, unread `●`, in that order of
precedence), the title at x=4 (bold when open, ending in `…` when clipped) and the
time of last activity right-aligned (`now`, `3m`, `4d`). Line 2 is the agent in its
colour, `·` and `⌥ branch` muted, with the child count (`2 ↳`) right-aligned. The
open session has the selection band on both lines; focus draws `▌` at x=0 on both.
The title or branch is truncated first, and on a very narrow sidebar the time or
count is dropped. The `.`/`m` row menu is a raised popover beside the row (Delete in
red, going to the usual confirmation); the rail's open item has the selection band.
The Git rows are `M path  +2 −3` (`M` warning, `A` and `?` success, `D` error); a
side that is 0 is left out. Set `NEXUS_PORT` and `NEXUS_PORT_EMPTY` to run the shots
on other ports than 4799/4798.

Preferences: the TUI keeps its own client preferences in `$NEXUS10_HOME/tui.json`
(mode 0600, written atomically through a temporary file and a rename). This is
not Nexus state: Core never reads it and the TUI still reads no other Nexus file.
It holds `ui.left_width`, `ui.left_rail`, `ui.left_open`, `ui.right_open` and
`theme.mode`, `theme.monochrome`, `theme.animations` (`false` is reduced
motion). They are loaded at startup and saved when one changes (the sidebar
edge drag is saved when the button is released). A missing or corrupt file is
ignored and replaced by the next save. Flags and the environment override the
file for the run and are not written back: `--dark|--light` over the saved mode,
`--mono|--256|--16` or `NO_COLOR` over monochrome, and `--reduced-motion` or
`NEXUS10_REDUCED_MOTION` over animations. `COLORFGBG` only picks the default, so
a saved mode wins over it.

Environment:

| Variable | Meaning |
| --- | --- |
| `NEXUS10_HOME` | Nexus home (default `~/.nexus10`). `~/.nexus` is refused. |
| `NEXUS_TUI_CORE_CMD` | Command that runs the Core CLI, default `nexus`. Used as `<cmd> daemon start`, for example `uv run --project ../nexus-core nexus`. |
| `NEXUS_TUI_NO_START` | If set, never start Core; report offline instead. |

Dependencies are pinned to exact versions in `Cargo.toml` (`=x.y.z`) and
`Cargo.lock` is checked in. Rust 1.85+.

Try it against a real isolated Core:

```sh
export NEXUS10_HOME=$(mktemp -d) NEXUS_PORT=4799   # any free port
chmod 700 $NEXUS10_HOME          # Core requires a private home
NEXUS_TUI_CORE_CMD="uv run --project ../nexus-core nexus" cargo run
```

## Voice dictation

`/voice`, `ctrl+d`, or the TUI's `ctrl+x v` shortcut starts dictation. The TUI
checks `GET /api/voice` first. If the STT model is absent, a confirmation names
its Hugging Face repository and approximate download size before the client
starts `POST /api/voice/stt/download`; Core's `voice.progress` events update the
download strip above the composer. Missing runtimes and unsupported platforms
show Core's status error instead of opening the microphone.

Capture uses the default input device through `cpal` on a dedicated thread.
Interleaved channels are downmixed and resampled to mono 16 kHz PCM16 WAV. The
start cue plays before capture opens and the stop cue after it closes. While
recording, a red `■` blinks left of the agent name (`#` in ASCII; steady under
reduced motion) and the partial transcript appears inside the composer at the
cursor where recording began, its changed words resolving over three 125 ms
ticks. Previews are requested only after at least 0.4 seconds of new audio, with
one transcription request in flight. Enter stops and sends; Esc stops and keeps
the words in the draft (Esc again while transcribing discards them); typing any
key stops the capture and the key still applies. The final transcript replaces
the preview at the position where recording began, ahead of anything typed
since. Capture is capped at 120 seconds.

Settings → **Voice and speech** reads both model statuses from Core, including
the state, Hugging Face repository, human-readable size, cache path and any
error. Downloads require an explicit confirmation showing the repository and
approximate size; it says the files come from Hugging Face and go into the
standard cache. `voice.progress` updates the relevant row while downloading.
Remove confirms that only that repository will be removed from the Hugging Face
cache. The modal refreshes its status when opened and after each download/remove
action; it never reads the cache directly.

## Module layout

Lower layers know nothing of higher ones.

| Module | Role |
| --- | --- |
| `transport` | HTTP (bearer auth, `Idempotency-Key`, `If-Match`, raw audio bodies, error classification, keyed transport retries) and SSE (incremental parser, `Last-Event-ID`, `EventStream`), `Backoff`. No Nexus types. |
| `api` | Typed responses and the endpoints the TUI needs; `/api/info` and the API-version check (`check_compat`); cursors; SSE `Frame` decoding; `RequestId`, `PendingMutation`, `Outcome` (done vs revision conflict). |
| `reduce` | Pure `Model` (projects, sessions, running turns, waits, streaming text, selected session's messages, turn summaries, spend ledger and the running turn's live events): `apply_snapshot`, `apply_history`, `apply_turns`, `apply_event` (`turn.summary` replaces the scratch), dedup by durable id, generation check. |
| `prefs` | `<home>/tui.json`: the TUI's own client preferences, load, atomic 0600 save, merge of changed fields. |
| `discovery` | `daemon.json` under the Nexus home, `/api/info` verification, `nexus daemon start` bootstrap, daemon log tail. |
| `sync` | The connection supervisor task: handshake, snapshot, SSE, reconnect with backoff, resync, restart detection. Emits `SyncMsg`, takes `Command`. |
| `state` | `ConnectionState`, `Banner`, `AppState` (model + connection + banners; `apply(SyncMsg)`). |
| `manifest`, `theme` | `interface/manifest.toml` and `interface/theme.toml` embedded with `include_str!` and parsed into typed structs: commands, keys, Esc chain, navigation, settings; colour tokens per mode and tier (truecolor, 256, ANSI-16, monochrome), glyph tables (Unicode/ASCII). No command list, key, reason, colour or glyph is copied by hand. |
| `completion` | Ranking (`rank_menu`, typo distance), acceptance text and the per-token reply cache for the composer's completion popup, copied from old Nexus. |
| `dictation` | Local `cpal` capture on its own thread, mono conversion, streaming resampling, PCM16 WAV encoding, cue tones, waveform levels and preview pacing. No device handle crosses threads. |
| `text`, `keys`, `composer`, `editor` | Display-width helpers, key names, and the multi-line composer. The grapheme editor and visual layout are adapted from old Nexus; composer movement and deletion respect grapheme boundaries. The composer uses the editor
for selection, undo/redo and collapsed large pastes; sent text expands every paste. |
| `ui`, `layout` | View-state types (focus, overlays, dialogs, alerts, mouse targets). `Regions::compute` is the one geometry function; the renderer registers every clickable rect in a `HitMap` as it draws it and the map is rebuilt each frame. |
| `app` | Focus model, `when`-term evaluation, command availability, the palette, the Esc chain, keyboard/mouse/paste handling. Produces `Effect`s (API call, microphone capture, supervisor command, clipboard, quit, suspend). |
| `actions` | Core actions (prompt, session changes, voice status/download/transcription, and others), each with its own `RequestId` where applicable; failed sends restore their draft. |
| `timeline` | The session timeline: blocks in model order (starting context, turns, user/assistant text, reasoning, tools and their diffs, notices, diagnostics, child cards) with fold state and the list of selectable blocks. Folding is view state only. |
| `paint` | Buffer-level drawing helpers: `scrim` (dim a buffer under a modal), `modal_rect` (the modal block's geometry), `meter`. |
| `render` | Ratatui drawing from the model, theme and manifest. Timeline blocks become hit targets as they are drawn. `render_text` renders to a text grid for tests and frame dumps. |
| `term` | Raw mode, alternate screen, mouse, bracketed paste, kitty keyboard flags; restored on error, panic (hook) and Ctrl+Z suspend/resume. |

Tests (111): unit tests live in the modules; `tests/timeline.rs` covers turn
grouping and footers, folding (including "all but last"), tool diffs and the raw
toggle, history merging, and keyboard/mouse block selection; `tests/sync.rs` runs the supervisor
against a scripted loopback mock Core (live, reconnect, cursor rejection,
overflow, restart, auth failure, API major/minor, offline, selection);
`tests/mutations.rs` covers keyed retry, replay and conflicts. Shared fixtures
are in `fixtures/` (see `fixtures/README.md`): captures from a real daemon and
shapes from the docs.

## Connection contract (as implemented)

- **Discovery.** Read `<home>/daemon.json` (`url`, `pid`, `token`), trust it only
  after `GET /api/info` answers with that token. If absent or stale, run
  `nexus daemon start` once per outage, then rediscover.
- **Handshake.** `/api/info` `api_version` (`"1.2"`, `"1"` or `{major, minor}`):
  same major goes; newer Core minor gives a banner; a different major halts in
  `Incompatible` naming which side to update, and no other request is sent until
  `daemon.json` changes. Core reports `{major, minor}`; `Unreported` remains a
  fallback for older daemons.
- **Snapshot, then stream.** `GET /api/snapshot` (with the selected session's
  bounded messages), then `GET /api/event` with `Last-Event-ID: <head_cursor>`.
  The cursor advances only after an event reached the app; reconnect resumes from
  it, dropping events at or below the applied id. A rejected cursor
  (`invalid_cursor`, `cursor_generation`, `cursor_ahead`, `history_unavailable`),
  `bus.overflow` or an undecodable event refetches the snapshot and resumes after
  its watermark. Gaps in ids are never treated as loss.
- **Restart.** Every reconnect rereads `daemon.json` and `/api/info`; a different
  pid or token forces a full snapshot (`ResyncCause::DaemonRestarted`).
- **States.** `Starting`, `Connecting`, `Live`, `Reconnecting`, `Offline`,
  `AuthFailed` (401; waits for a new token), `Incompatible`. Anything but `Live`
  is read-only: the last model stays visible.
- **Mutations.** One `RequestId` (an `Idempotency-Key`) per user action, reused
  for transport retries only (three attempts, same key; HTTP errors are never
  retried). A stale revision (`If-Match`) is `Outcome::Conflict { current_revision }`,
  not an error: re-read, ask the user, retry with a new key. `PendingMutation::retry_safe`
  checks the server's deduplication lifetime before retrying a lost response.

## The shell

Anchors (INTERFACE_PLAN.md): left sidebar toggle at the header's leading edge,
title and breadcrumbs centred, notification count and right toggle trailing;
the toggles stay when a sidebar is closed. Left tabs Sessions, Files, Projects
with Settings fixed at the bottom; right sidebar SESSION, CONTEXT and GIT
(the Git section shows its manifest reason when unavailable). Below 100 columns the sidebars become drawers
opened from the same toggles. The waiting card sits directly above the composer
(DESIGN_PLAN D14): a heavy accent rail, `Allow bash?`, `waiting on you · 0:42`, the
parameters (and, for edit, write and patch, the diff capped at 12 rows), and a choice
list with key chips (`y` allow once, `n` deny; Up/Down and Enter also choose; choices
Core does not offer show their reason). Toasts (D15) are top-right under the header, three
at a time with `+N more`; a click, or Enter on the focused toast (`F6` to it), opens
the full text in a modal (`c` copies, Esc closes), and the notifications modal opens the
same text for an entry. The `↓ N new · end` pill sits on the row above the composer card.
The card also answers `ask` questions: `Alt+W` focuses it, a number key (1-9, 0 for the
tenth) or Up/Down with Enter picks an option, Space or a number ticks options of a
multi-select question, and clicking an option picks it. A question without options,
or one that allows a note, takes text typed in the composer (typing from the
focused card moves there) and sent with Enter. Several questions go one at a time;
the reply is sent once, as a keyed `POST …/question/{id}`, after the last one
(`Ctrl+Enter` or the button confirms the current question). Esc keeps following
the manifest chain, so while the turn is busy it interrupts, which cancels the
question. Settings opens in a centered
modal using old Nexus's panel geometry and a searchable root/drill list. Press
`/` to filter settings; Enter opens a section, and Esc returns to the root
before closing the panel. Existing Core-backed controls retain their own focus
and keyboard handling.

Keys come from the manifest (`F1` lists them; the palette shows each command's
key). Beyond the manifest: `F6`/`Shift+F6` cycle focus, `Ctrl+J` inserts a
newline where `Shift+Enter` is not reported, `Ctrl+C` copies a composer selection or quits, `Ctrl+Z` undoes in the composer
(and suspends elsewhere), and `Ctrl+Y` redoes.
`/name` in the composer runs the command with that slash name (`/rename New
title`, `/continue`, `/new`, `/settings`...); an unknown `/word` is sent as text.

As-you-type completion: a borderless popup of up to ten rows, exactly as wide as
the composer card and flush with its top edge, sits above it on the raised
surface (the selected row has the selection band and focus bar; the last row
holds the key hints and `n of N`). Typing `/word` as the draft's first word ranks the manifest's
slash commands locally (prefix, then subsequence, then typo, then the rest, so
the height stays steady), each with its title as muted help and the reason when
it is off. A token starting with `@` asks Core for paths
(`GET /api/project/{id}/files/search`, directories end with `/`), keeps replies
cached per token, drops a reply for an old token and pads a short list from an
empty-query listing. Up/Down move, Tab or Enter accepts (`@path ` or `/name `;
a `dir/` gets no space so completion continues inside it), Enter on a lone
`/command` runs the highlighted one, a click accepts a row, and Esc hides the
popup for that token without reaching the Esc chain. Nothing completes while a
question, dialog or palette is open.

In the timeline, Up/Down (or j/k) move between blocks, Enter or Space opens or
closes the selected block (a child card opens the child), Left/Right close or
open it, PageUp/PageDown scroll, and `[`/`]` jump between turns. F1 lists the
manifest keys (`alt+z` fold all but last, `r` raw, `y` copy, `i` inspector).
An export is written to `<Nexus home>/exports/<session>-<scope>-<utc>.<md|json>`
and the success toast names the path.

**Esc** walks the manifest's chain once, top to bottom, and stops at the first
rule that holds: overlay, dialog, inline edit, narrow drawer, focused alert,
interrupt a running turn, (editor), back from a child, leave settings, refocus
the composer. Esc never clears composer text. Overlays record the focus and
scroll they replaced and give them back.

Read-only: anything but a live connection disables every mutating command with
"Read-only until Core is connected again"; the last view stays visible and the
draft is kept. A different API major shows a blocking dialog and nothing else
reacts except `Ctrl+Q`.

## Visual test harness

The real binary in a real PTY, shown by xterm.js with Monaspace Argon and
truecolor, photographed by headless Chromium. Needs Node 22, `uv` and the
built binary; nothing calls a provider.

```sh
cd nexus-tui && cargo build
cd tools/webterm && npm ci                    # pinned: xterm 5.5.0, node-pty 1.1.0, playwright 1.64.1
npx playwright install chromium               # once
ROOT=/tmp/nexus-shots
./seed.sh $ROOT                               # two isolated Cores (mode 700, ports 4799/4798 by default; override with NEXUS_PORT and NEXUS_PORT_EMPTY): seeded and empty
node shots.mjs --env $ROOT/env --out $ROOT/png            # all shots (add --only palette to filter)
./seed.sh $ROOT stop
```

`shots.mjs` captures 80x24, 120x36 and 160x48 in dark and light, monochrome,
ASCII and 256-colour variants, and the states: empty workspace, session with
messages, palette (plain, filtered, light, narrow), settings, confirm dialog,
toast, multi-line composer, drawers, closed sidebars, Projects and Files tabs,
and the offline banner (it stops the seeded daemon under a live TUI). Each PNG
has a `.txt` of the screen beside it. `npm run serve` serves one profile so the
TUI can be driven by hand in a browser.

## Interface notes

- Background shell completions (`→ Background Shell Done: <command>`, then the
  job id, status, exit code, retained output tail and the `bash_output` hint)
  stay model context but show as a one-line expandable notice, not a user card or
  turn; consecutive completions group, and expanding shows the full context in
  order, including any output clipping notice.
- Clipboard images: **Ctrl+V** in the composer in a local macOS or Linux
  terminal (macOS reads PNG/TIFF through AppKit; Linux uses `wl-paste` or
  `xclip`; SSH clipboard reads are refused). Pasting inserts `[Image A]`,
  `[Image B]`, … at the cursor; the attachment names appear on one row above the
  card (no previews); deleting a marker removes its attachment. Up to eight
  images, 8 MiB combined; image-only sends work and a failed send restores the
  images. Stored images show in the timeline as an `Image | name` line. Normal
  text paste is unchanged.
- The top header omits the busy spinner, working label and elapsed time.
- The model picker shows model names on the left and providers on the right;
  `m` toggles details below the picker (they follow the selection) and is
  reserved for that while the picker is open.
- The startup draft shows its estimated starting context under the same Context
  heading as an opened session. Expanded Context uses spaced label-and-count
  rows, with the tool inventory and full prompt contents in the section details;
  estimates use `~`, Skills and MCP are labelled unavailable until Core supports
  them, disabled tools are counted, and the system prompt opens in its detail
  view without an inline preview. The starting context stays expanded.
- The composer supports mouse and Shift selection, Alt/Control+Left and Right
  word movement (with Shift to select), Alt+B/F and Control+Home/End. It has one
  row of top padding above the input and a half-height bottom edge. Its status
  shows the latest reported input tokens, the percentage of any known
  pricing-tier boundary and of the context window; hovering explains them. The
  location strip keeps the current path and branch, and a rule below it shows
  context occupancy when idle (against the first pricing tier until reached,
  then the window); during a turn an agent-coloured segment moves along it
  (stationary with reduced motion, a heavier glyph in monochrome).
- User message bands and their sticky headers sit inside the timeline's
  gutters; image labels align with the message text; agent output is indented to
  the user text and wrapped Markdown stays within the timeline. While a turn is
  busy, three empty rows separate its latest content from the composer.
- Shell command and error previews are limited to 80 characters with `...`;
  expanding the tool shows everything. Error text is neutral and the failure
  glyph coloured. Only shell tools show an elapsed time; reasoning and activity
  groups do not (so there is no "Thought for Ns", although turn summaries carry
  `reasoning_seconds`).
- Old Nexus shortcuts: Ctrl+N (new session), Ctrl+G (agent), Ctrl+O/Ctrl+S
  (sessions), Ctrl+L (right sidebar), Ctrl+T (cycle effort), Ctrl+R (reconnect);
  Ctrl+X then M, N, O/B, G, S, L, T, R or ? opens model, new session, sessions,
  agent, settings, sidebar, effort, reconnect or shortcuts, and Ctrl+X Shift+N
  notifications. Shortcuts for capabilities Core lacks are omitted.

Settings reads the shared defaults, timeline detail, reduced-motion choice and disabled tools from Core's `/api/config`.
Use left/right or Enter to change a choice or tool switch; Core checks the file
revision before each write. Configuration errors and rejected writes appear as
warnings. A `config.changed` event refreshes Settings after an external edit.
The session context meter reads Core's context budget; `~` marks estimates.
