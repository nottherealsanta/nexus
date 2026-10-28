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
control row shows the agent in its colour, the catalog model name and provider,
a pending model change, effort, and on the right the `╱` meter with
`82.0K · 41%`, explained on hover.
The first row is a band on the chrome surface (`surface.sidebar`, shared
with both sidebars; no rules between them, a `│` edge only in ANSI-16 and
mono). It holds the left toggle, the Sessions / Files / Projects icons over the
left sidebar (a column on the rail, the drawer's first row when narrow;
hovering shows the label), the session tabs, the run status at the centre's
right edge, the notification count and the right toggle. Sidebars are 34 / 36
cells from 160 columns and 28 / 30 below. `paint::scrim` dims a buffer under a
modal. The left sidebar (D03) opens with a `⌕ Search sessions` row (`ctrl+k`, it opens the
switcher) and shows an UPPERCASE project header, `⌥ branch  ~/…/dir` sub-groups for
linked worktrees, and two-line rows (mark slot, title, time; agent in its colour,
`· ⌥ branch`, `2 ↳`) ending with `+ New session`; `Settings` is pinned at the bottom. Drag its inner edge to resize it (20–60 columns);
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
drawn only in ANSI-16 and mono.
The header holds session tabs (`×`, `+`, `alt+x`, `alt+[`, `alt+]`): the viewed tab is bold on the page surface, the others muted on the band, a child session shows `root › child` with clickable earlier crumbs. `alt+d`
(or Settings → Timeline detail) cycles the timeline between low (one line per
activity group), medium (each tool on a collapsed line, the default) and high
(every tool expanded, long output capped). Drag the left sidebar's edge to resize it.
The starting-context section lists the system prompt, tools, instructions,
agent and child agents with estimated tokens (characters ÷ 4), detail rows, a
total and the share of the model's window; the turn footer adds the catalog
price when the model has one.

Settings is a centred modal over the dimmed session: type to search, ↑/↓ to
move, ←/→ to change a value, Enter to act, Esc to clear the search and then
close. Rows the TUI cannot change yet are muted and say why below the list.
Consecutive diagnostics (a provider retry sequence) form one tight stack with
delays rounded to tenths.

The timeline is complete for one session: turn headers and footers, folding,
collapsed reasoning, old-Nexus CommonMark text (supported Mermaid diagrams retain their source below), tool-specific expansion (diffs, output
caps, raw arguments/results), child cards, context notices, diagnostics, and the
frozen starting-context section. Selecting a session loads its full stored
history (all messages, the retained diagnostics that carry duration and usage,
and the snapshot) after the bounded snapshot view. Model, effort and agent
pickers, session export and a directory change are wired.

### Markdown and diffs (DESIGN_PLAN.md D08, D09)

`markdown.rs` draws assistant text. Headings are bold: H1 and H2 in
`text.primary` (H1 with a muted `─` underline as wide as its text), H3 and
below in `text.secondary`. Inline code is `text.primary` on `surface.panel`,
padded by one space, never accent. A fence is a `surface.panel` block across
the reading column: a header row with the language (`code` when absent) in
`text.muted`, the code inset by 2, one padding row; there is no rule and no
rail. A focused or hovered fence shows `copy` at the right end of its header
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

The Files tab is a lazy tree: folders expand one level at a time, Git status
letters are right-aligned, ignored entries are muted and can be hidden, and a
root row switches between the project checkout and the open session's worktree
when there is more than one. Enter opens a read-only, line-numbered file view
with a `~` path; Esc returns to the timeline. The selected file can be copied as
an absolute path, inserted as `@path`, or opened in `$VISUAL`/`$EDITOR` with the
terminal suspended. In the file view `/` opens a search line: typing filters
and highlights matches, Enter or `n` goes to the next match and `N` to the
previous one, and the first Esc ends the search while the next closes the file.
Core serves whole files up to 1 MiB and has no offsets, so the bottom notice
gives the exact range shown (`lines 1-40 of 300`). The file view has no syntax
highlighting yet.

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
row with no end status: an interrupted turn adds `/continue` (`enter continue`
with an empty composer), a failed turn puts its first error line (`✕`) above.
Durations read `0.4s`, `4.2s`, `1m 05s`, `1h 02m`. Activity groups fold to
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
Reasoning reads `▸ Thought for 2.1s · preview`, and `▾ Thinking… 2.4s` with its
last five rows while it streams. The header status reads `◐ working 12s`,
`◆ waiting for you` or `✕ offline`.

The right sidebar's Session
info totals turns, calls, tools by name, tokens, a context bar against the
model's window and the catalog cost. Right-click (or `m`) on a sidebar row opens
its action menu; the Projects tab ends with `+ Add project` (an inline path
field) and removal asks first. Settings shows the default model, catalog size
and source and the loaded agents, and says why rows that need a Core setting
cannot change.

Still Step 12.4: the built-in editor, worktree
management in the UI, `path:line` links, settings editing beyond
theme/monochrome, and steering/queueing (blocked on Core, Step 12a). Unavailable commands are listed disabled with their reason.

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

## Module layout

Lower layers know nothing of higher ones.

| Module | Role |
| --- | --- |
| `transport` | HTTP (bearer auth, `Idempotency-Key`, `If-Match`, error classification, keyed transport retries) and SSE (incremental parser, `Last-Event-ID`, `EventStream`), `Backoff`. No Nexus types. |
| `api` | Typed responses and the endpoints the TUI needs; `/api/info` and the API-version check (`check_compat`); cursors; SSE `Frame` decoding; `RequestId`, `PendingMutation`, `Outcome` (done vs revision conflict). |
| `reduce` | Pure `Model` (projects, sessions, running turns, waits, streaming text, selected session's messages and every event): `apply_snapshot`, `apply_event`, dedup by durable id, generation check. |
| `prefs` | `<home>/tui.json`: the TUI's own client preferences, load, atomic 0600 save, merge of changed fields. |
| `discovery` | `daemon.json` under the Nexus home, `/api/info` verification, `nexus daemon start` bootstrap, daemon log tail. |
| `sync` | The connection supervisor task: handshake, snapshot, SSE, reconnect with backoff, resync, restart detection. Emits `SyncMsg`, takes `Command`. |
| `state` | `ConnectionState`, `Banner`, `AppState` (model + connection + banners; `apply(SyncMsg)`). |
| `manifest`, `theme` | `interface/manifest.toml` and `interface/theme.toml` embedded with `include_str!` and parsed into typed structs: commands, keys, Esc chain, navigation, settings; colour tokens per mode and tier (truecolor, 256, ANSI-16, monochrome), glyph tables (Unicode/ASCII). No command list, key, reason, colour or glyph is copied by hand. |
| `completion` | Ranking (`rank_menu`, typo distance), acceptance text and the per-token reply cache for the composer's completion popup, copied from old Nexus. |
| `text`, `keys`, `composer`, `editor` | Display-width helpers, key names, and the multi-line composer. The grapheme editor and visual layout are adapted from old Nexus; composer movement and deletion respect grapheme boundaries. The composer uses the editor
for selection, undo/redo and collapsed large pastes; sent text expands every paste. |
| `ui`, `layout` | View-state types (focus, overlays, dialogs, alerts, mouse targets). `Regions::compute` is the one geometry function; the renderer registers every clickable rect in a `HitMap` as it draws it and the map is rebuilt each frame. |
| `app` | Focus model, `when`-term evaluation, command availability, the palette, the Esc chain, keyboard/mouse/paste handling. Produces `Effect`s (API call, supervisor command, clipboard, quit, suspend). |
| `actions` | The mutations the shell makes (prompt, new session, continue, interrupt, archive, rename, delete, allow/deny), each with its own `RequestId`; a failed send hands the draft back. |
| `timeline` | The session timeline: blocks in model order (starting context, turns, user/assistant text, reasoning, tools and their diffs, notices, diagnostics, child cards) with fold state and the list of selectable blocks. Folding is view state only. |
| `paint` | Buffer-level drawing helpers: `scrim` (dim a buffer under a modal). |
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
opened from the same toggles. The waiting card sits directly above the composer.
It also answers `ask` questions: `Alt+W` focuses it, a number key (1-9, 0 for the
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
