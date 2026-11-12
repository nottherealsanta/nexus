# nexus-tui

The Ratatui terminal client for Nexus Core (Rust). It speaks only the Core HTTP
API (`/api/*` plus the `/api/event` stream, see `../docs/api.md`) and never
reads Nexus state files, SQLite or provider credentials. The one file it owns in
the Nexus home is its own preferences file, `tui.json` (see Preferences below). Old Nexus's client in
`~/repos/nexus-old/rust/tui` is reference.

This client is also the visual reference for `nexus nsh tui`, which runs the shared
NSH TSX app rather than this Rust implementation. NSH now uses the shared Unicode
sidebar/fold/tool glyphs, a monospace interface and compact full-width reading
gutters on terminal, web and native targets. This does not change the Rust client's
existing layout or controls.

**Terminal** in the command palette opens the Core terminal panel, and
``Ctrl+` `` toggles it from anywhere, including while the terminal screen owns
the keys. ``Ctrl+Shift+` `` / `Ctrl+~` spell the same physical key. The toggle
is never sent to the shell; hiding the dock detaches and returns focus to the
composer. Use Up/Down and Enter to create a terminal or attach to a listed one.
On an attached terminal, `c` takes control and `r` releases it; Enter on the
selected row focuses its screen. Ctrl+] returns to the list and Esc from the
list detaches.
Input is sent only while this attachment has control. Terminal applications receive
mouse events when they request them; hold Shift to select text instead.
The compact dock toolbar offers new, rename, close, take/release control, copy and
hide; hover a control for its label. The dock holds only the user's terminals; the
agent's running background shells have their own `N shell` indicator, which opens the Shells modal.
In the Shells modal, ↑/↓ select a shell and `s` (or its `[stop]` / **Stop** action) stops a running one (`DELETE /api/bash/{id}`).
Closed resources are hidden and opening the dock prefers a running terminal.
Exit immediately disables input and updates the tab; a late control acknowledgment
cannot make an ended terminal writable again. `--dump-frame` also runs the real
terminal attachment and stream, so dock captures include the actual VT screen.

**Attach selected output** (`[Attach]`) freezes the rendered terminal selection and
previews the exact payload before **Add to draft**. Use Up/Down or PageUp/PageDown
to read the entire preview. It includes resource/project/workspace provenance, a
stream-sequence watermark, an untrusted-data warning and exact UTF-8 clipping counts
(16 KiB selection cap). Actual source session/turn/tool/job IDs are retained when
recorded, never guessed. Missing earlier terminal history is disclosed separately.
Confirmation appends without replacing selected draft text. Cancel is inert; normal composer send alone commits it into model context. Live
output cannot mutate it, and a changed/read-only destination draft fails visibly.

**Open inspector** opens the native inspector modal (Inspector API v2), shared with NSH,
instead of a browser. Views: `1` Timing (one shared-axis Gantt, lanes grouped by
session, a fixed ruler), `2` Conversation, `3` Steps, `4` Requests, `5` Raw, `6`
Questions. Lists show each record's `summary`; Enter expands the exact `detail`
JSON and `e` loads the full record when it was clipped. Rows and session-tree entries are clickable (click a row to select and expand it). `n`/`p` page to older/newer
records by cursor (never page numbers); `[`/`]` inspect another session of the
tree without leaving the chat; `/` searches, `f`/`s` cycle the kind/status filter,
`t` filters event types, `x` clears. `w` pauses live updates, `r` refreshes. The
state line says `Live`, `Paused` or `Disconnected` (disconnected data says it is
not current). While open the modal coalesces session stream changes, checks `/inspect/head`, and refetches
only when the head or generation moved; nothing runs while it is closed. Selection,
expanded rows, zoom/pan, filters and the cursor stack are kept per session and view
across refreshes and view switches. `+`/`-` zoom, `h`/`l` pan, `z` fits the selected
operation (again: the whole range), `c` copies the row (or the view), `i` shows the
cache notes. The cache panel labels the session lifetime and the displayed page
separately, with coverage ("n of m requests reported usage"), the hit-rate
denominator and a per-model breakdown; cache writes say unknown. `omitted` and
notes (what was left out and why) show below it. Esc closes without changing the
session or draft. All keys are the `inspector.*` commands in `interface/manifest.toml`.
See [the inspector guide](../docs/inspector.md).

A new session's first message appears immediately below its context preview as
`You · Sending…`, without waiting for MCP startup. The chosen agent/model/tool
settings remain visible until Core's message and starting history arrive, avoiding
a default-header flash. Pending text is local submission state, not model context;
failed creation restores the new-session draft, and failed submission after creation
restores it in the created session. Creation opens the tab before prompt submission.

Final dictation adds the editable note `[Voice transcript — may contain errors.]`
at the top of the composer, once per message (including mixed typed/dictated
drafts). The same note stays in the sent context and model input. Empty or
cancelled dictation does not mark a draft; OS keyboard dictation cannot be
identified automatically. See [Voice](../docs/voice.md#transcript-provenance).

**Status (PLAN.md Steps 12.3 and most of 12.4):** the keyboard-first shell is
usable against a real Core: workspace header, both sidebars, session view, waiting
card, composer, command palette, the settings modal and unified alerts, with an
explicit focus model, the manifest's Esc chain, mouse and keyboard parity, and
terminal hygiene. The status footer has been removed; key hints are available
in the palette and F1, and stop appears in the composer. Existing sessions
show their branch and linked-worktree marker from Core below the composer; a
project with no session shows its own branch. The linked-worktree `⑂` (ASCII `*`)
icon is a button: hovering it says `Worktree session`, and clicking it opens the
compact **Worktree** modal with the full wrapped worktree path, branch and main
checkout path. Each value is copyable (OSC 52) with a `Copied` toast; Enter copies
the path, Esc closes. The composer is a soft card
(half-row `▄`/`▀` edges in the panel colour; `─` rules in mono and ASCII) whose
control row shows the agent in its colour, the catalog model name (or a pending
model change), provider, effort, and on the right the `╱` meter with
`82.0K · 41%`, explained on hover.
The first row is a band on the chrome surface (`surface.sidebar`, shared
with both sidebars). It holds the centred left toggle over the project rail, the
`[Sessions]` / `[Files]` tabs over the left sidebar, aligned with session titles (the drawer's first row when narrow), the
session tabs, the waiting status at the centre's right edge, the notification
count and the right toggle. Sidebars are 34 / 36 cells from 160 columns and 28 / 30
below. `paint::scrim` dims a buffer under a modal (see the modal recipe below). Every surface is a
rectangle on the cell grid; the TUI has no rounded corners, and only circular status marks and
meters are round. The project rail is five cells wide and always there, from 70 columns, whether
the sidebar is open or not; a border separates it from the open sidebar.
Projects keep creation order with uppercase initials; collisions use the next
available consonant, then a numeric suffix; the first character stays centred. The rail shows projects in Core's saved order without inactive ones. Drag a tile to reorder (the target tile is highlighted; Esc or dropping outside the rail cancels; a click without a drag still selects), or press `alt+up` / `alt+down` on a project; passive projects are dimmed, and the project picker (`project.switch`) lists inactive ones and restores the chosen one. Removing a project (confirmed) only unregisters it: Core keeps its sessions and files stay. The rail has one empty row above its projects. The selected project tile draws a lighter block (`interactive.active`) with a left `▏` bar (`interactive.active_bar`); the selected session row in the sidebar gets the same block and bar across both of its lines, with its title in `text.highlight` (white in dark mode) and not bold. Running or recently active projects
(within one hour) are lighter gray. Each tile has a counter in each corner:
attention (purple) and unread (blue) on top, running (green) and idle (white) below,
counting root sessions, with archived sessions left out and subagents counted under
their root. Counts above nine display `*`, and a project with no counts shows a
three-cell muted rule. Hovering shows the full project name and `N attention`, `N unread`, `N running` and `N idle` right beside the rail, over the sidebar when it is open. A `+`
Add project control and Settings are pinned together at the rail's foot, below the projects
(a right-click opens a project's actions). Activating a project restores
its open tabs and last selected session or new-session view in this client; pressing
the project already open toggles its Sessions sidebar. On first
selection it falls back to its newest unarchived root session or an empty new-session
view. New-session composer text and images are kept separately per project, for the
client process. The left sidebar (D03) lists the current project only (`ctrl+k` opens
the switcher). Its three-action footer is pinned below the session list (`+ New session`, then `Search sessions`,
then `Show archived`, also when the list is empty); the archive control includes archived sessions alongside active sessions when enabled.
You can type and send in an archived session: **Resume archived session** warns
that a large uncached context may be slower and cost more, and that code kernels
restart fresh. Cancel keeps the draft; Resume unarchives the whole tree before
sending. Explicit Unarchive shows the same warning without sending a message.
Archiving closes the session's tab and any archived descendant's tab in every
connected client, whoever archived it (this client, another client, the Manager
or Core's idle auto-archive); focus moves to the neighbouring tab, or the
new-session screen when none is left. `/archive` performs the same archive.
The pinned footer stays put as the list scrolls; the list has `⌥ branch  ~/…/dir`
sub-groups for linked worktrees, and two-line rows (mark slot, title, time;
`⌥ branch`, `2 ↳`); `Settings` stays at the rail's foot.
Drag its inner edge to resize it (20–60 columns); dragging below 14 columns closes it.
Settings → Sidebar switches full / rail. The project
strip under the card shows the project name (bold), its path (muted),
`⌥ branch` and `checkout` (muted) or `worktree` (bold violet); `ctrl+p commands`
sits muted at the right while idle. The card has no rail and no status band.
Rows, top to bottom: the live row (while busy: `◐ Working · 12s · ↓4.1k`, with
Working shimmering), the top edge, the input at three cells in, a blank row, the
control row, the bottom edge and the strip. The control row sheds effort, then
the provider, then the meter as the column narrows; while busy its right side
reads `esc stop` instead of the meter. The focus bar at the card's left edge is
drawn only in ANSI-16 and mono. Pending inputs appear in a padded card above the
composer; their text starts where the input starts, with a primary-colour,
normal-weight `Q` for a queued message or `S` for a steer one cell to its left. Alt+Enter (`ESC CR` in Ghostty, a modified Enter with the kitty protocol) while busy cancels the turn and sends the draft first (`composer.interrupt_send`); the busy placeholder lists `Enter steer · Tab queue · Alt+Enter interrupt`. A held queue says `held`
after the message. Hovering or selecting a **queued** row right-aligns `✎ ↑ ↓ ×`
(ASCII `e ^ v x`): edit restores it to the composer, up and down move it within
the queued group only (disabled on the first and last queued row), and remove asks
before deleting it. Steer rows keep the keyboard selection and the existing keys
but have no action icons. The keyboard path is unchanged: Enter edits, Backspace
removes, Shift+↑/↓ moves. Up from an empty composer selects the last pending row;
on a held queue, `n` or the shared **Send next** control releases the next input.
The header holds session tabs (`×`, `+`, `alt+x`, `alt+[`, `alt+]`) in one fixed-width cell for every tab, root or New session, so the strip keeps its rhythm and a long title ellipsises; hovering a title too wide for its tab scrolls it to its end inside its dedicated title region, so the status mark and the fixed `×` never move or get overwritten; reduced motion instead shows the full title in a tooltip. Below the tabs the page keeps exactly one heading row: a root session leaves it blank (the full title is already its tab), while a viewed subagent shows its title on the left and right-aligned `agent · model · provider · effort` metadata, shifted right by a full-height four-cell muted-grey back strip at the page's far left. The strip runs from the heading through the composer, centres its `←` both vertically and horizontally, and the whole strip is clickable; it and Left outside the composer, text fields, the editor and overlays return straight to the root. Esc still returns to the immediate parent, and a root session has no strip. There is no second subagent-tab row; child cards remain the navigation path to a subagent. The viewed tab is bold on the page surface, the others muted on the band. `alt+d`
(or Settings → Timeline detail) cycles the timeline between low (one line per
activity group), medium (each tool on a collapsed line, the default) and high
(every tool expanded, long output capped). Drag the left sidebar's edge to resize it.
Turn footer token counts use `K` for thousands and `M` for millions.
The starting context is a list of `◆` sections (DESIGN_PLAN D10, `paint::context_rows`, one
renderer for the timeline and the new-session view): System prompt, AGENTS.md, Tools, Skills,
MCP and Child agents or Workspace tools when present. Tool, skill and MCP rows show their
on/off state; click a row or focus the timeline and use ↑/↓ then Enter/Space to switch it.
Tools use up to four columns as space allows, both before and after a message.
Skills stay inline through 12 entries; above 12 only the clickable `Skills [N]` heading remains.
The heading opens a scrollable two-column card picker at any count. Names open a separate
Markdown detail view. Cards are borderless dark gray, with a lighter focused card.
The detail view shows the full description, complete `SKILL.md` and names of other files
and folders. Inline skill names also open that view. In the picker, arrows/Tab move,
Enter opens details and Space toggles; the marker and name have separate mouse targets.
Esc returns from details to the same picker card, then closes the picker.
Locked session rows are dimmed and say `new session`; in the new-session preview a locked row
names its state or configuration instead, because that view already is the next session. Each header keeps its estimated count
(`~2,140 tok`, characters ÷ 4) and meta, a text row gives the total and the share of the model's
window, and there is no bar. The instruction file is part of the system prompt, so the total
counts the prompt and the tool schemas only. Previews clip with `…`. After the first turn the
whole section folds to `▸ Context · 4 items · ~2.9k tokens`. The new-session view shows the same
sections under a header block (`New session in <project>`, agent, model, effort, directory and
branch, then the `Tab`/`ctrl+m`/`ctrl+e`/`ctrl+d` hints); it loads project skills, the global MCP
list and the project's actual MCP-file inventory from Core (`.mcp.json` and `.agents/mcp.json`;
enabled, disabled, failed and invalid entries, including a whole-file `(file)` error row; read
without starting a server). A name defined in both files shows the duplicate notice on the
server's details, naming the winning file, and the section meta says `.mcp.json` wins. The TUI
sends draft switches with session creation. Only servers in Core's draft plan count as model
context; a project row wins over a same-named global row, so a disabled global default never
hides the project's enabled entry. Enabled, valid project entries start automatically with the
new session and are shown on; disabled, failed and invalid rows are shown off and locked with
their state named. There is no per-project approval step (Core removed the approve/revoke
endpoints, fingerprints and `pending_approval` state): the file is trusted configuration, and
`"enabled": false` is the opt-out. Click a row (or the marker of a blocked row) for its details:
the file, state, error and warnings, the command name and argument count or sanitized URL,
working directory, environment and header names, tool filters and timeouts, and the exact action
to take. The TUI never starts a project server. Its prompt preview is built
from the first line of the agent's prompt body, the project's `AGENTS.md` when the TUI can read
it, and the selected agent's tool list or the default tools. Counts read `~N` with "estimated"
on hover. Finished-turn footers show model, tool count, input, output and reasoning
tokens, cached input percentage, elapsed time and recorded finish time. Click the
footer or select it in the timeline and press Enter for further details including effort. Missing usage is
marked unknown, and finish time is omitted when it was not recorded; elapsed
time based on the last available message is prefixed `~`.

**Keyboard flow from an empty composer (PLAN 15.4).** Only when the composer is empty (with text, Left moves the
cursor as always): Left opens the session sidebar and focuses it; Left again focuses the project list (Up/Down move
among the projects); Left again opens the **projects overview** (also the palette command `project.overview`). Esc or Right
steps back out in reverse order: the overview closes, then the project list returns to the session list, then the
composer takes the keys. The overview is a modal listing the projects in their saved order; under each a `├`/`└`
tree of only its active root sessions with their state (`running`, `needs input`, `finished`, `idle`), and a
`N archived` toggle (`a` or Enter; collapsed by default) that lists the archived ones. Passive projects are dimmed;
inactive projects are grouped at the end under `Inactive` and restored with `r` (`1`/`2`/`3` set current, passive,
inactive; Enter opens a session or a project's new-session view). NSH draws the same rows (`overviewRows` in
`nexus-nsh/src/project-rail.ts`). Tests: `tests/keyboard_flow.rs`.

`/learn <source>` asks whether to install into this project or every project, previews the
source, then either installs an unambiguous set of new skills or opens a checklist. The checklist
uses ↑/↓, Space, `a` (all), `n` (none), `i` (invert), Enter (install) and Esc (cancel); ticking
an installed skill replaces it. Successful installs are available in new sessions. Settings →
MCP controls whether all tool definitions go into context and the 1–1000 name-listing limit.

Every overlay (palette and pickers, Settings, confirms, help, notifications) is one
square modal recipe (`render.rs` `modal`, DESIGN_PLAN D13): the scrim over the whole frame, then
a borderless `surface.raised` block, 2 cells of padding left and right and 1 row top and
bottom, within 2 cells of the frame edge and at most 70% of its height. NSH draws the same
square recipe and the same two-pane Settings, with no rounded corners. The title is the
first row inside, key hints (including Esc) the last row, the selected row is on
the selection band with the `▌` focus bar. Confirms default to `[ Cancel ]`; `[ Delete ]`
is in `status.error`. The `.` row menu stays an unscrimmed popover beside its row.

Modal widths use three classes: small 60, medium 72 and large 88 cells, clamped to
the frame. All are centred, including the command palette. Model information stays
inside its picker above the footer. Long confirmations retain the existing height
fallback until their bodies can scroll. See `interface/design-language.md` for the
shared rules.

Settings is that modal, 88 cells wide, with a search field and two panes. The
left rail lists every section on consecutive rows; the right pane shows the selected section, a
provider list or an agent page. ↑/↓ stays inside the focused pane, Enter or →
leaves the rail, Tab swaps panes and Esc steps back to the rail and then closes.
Choice and switch rows draw the same segmented control as NSH; ←/→ move the
focused segment (wrapping at the ends, so Enter still cycles a switch). Typing
filters both the rail and the rows. Rows that cannot be changed are not shipped
rather than shown disabled.
`/login` opens Providers. Providers: Enter opens that provider's detail modal;
its connection actions live there, one row per method Core lists (`Sign in with
browser`, `Sign in with a device code`, the provider's CLI or local server). A
sign-in link and code are their own rows, so Enter opens the link (the URL is
underlined) and Enter on the code copies it. Agents: the default agent and effort, then one line per
subagent; Enter opens its pane with its Models (and their odds) and Fallbacks,
where Enter on `+ add …` picks a model, `d` removes one, `K`/`J` move a
fallback and Right on a model cycles its effort (`effort default`, then the levels the
catalog lists for it; a model with none listed shows no effort). Tools shows one permission choice and one tool grid; Right chooses a tool
and Enter toggles it.
Session outcomes appear in a non-dismissible strip immediately above the composer, not in the timeline or
footer. The latest error/interruption, retry or recovery wins, with a count of additional diagnostics. The strip
restores on revisit, clears on busy and offers `/continue` when applicable plus Inspect for the full HTTP/body/history.
Context notices and `via Manager` decisions remain in the timeline; operational metadata stays in the inspector.

`/btw <question>` opens a live read-only side-question modal, even while the parent is busy. It shares the parent's
settled cached prefix but never changes its context. Compact tool rows and a `N/10` budget accompany streamed
Markdown replies. `i` expands the inherited request prefix; `c` expands the full ordered side-run context, including
the marker, tool arguments/results and notices. Esc/close cancels a running side question, not the parent.
Checkpoints recover missed stream events after reconnect. Past-answer navigation is not yet offered.
`/continuation` is removed; `/continue` and `/origin` stay.

The timeline is complete for one session: turn headers and footers, folding,
collapsed reasoning, old-Nexus CommonMark text (supported Mermaid diagrams draw
as shared text rows or the shared PNG, with a clickable label to copy their
source; unsupported diagrams show the source), tool-specific expansion (diffs, output
caps, raw arguments/results), child cards, context notices, Manager decisions, and the
frozen starting-context section. Selecting a session loads its stored history
in the background while the event stream stays open: the newest messages, the
starting-context snapshot and its turn summaries, fetched together. The event
log is not read. A select never resyncs (no global snapshot, no stream reopen).

**Session views are cached.** The views of the last 8 sessions left behind stay
in `Model::cached` and keep taking their sessions' events, so switching back (a
tab, a child and its parent) shows the timeline at once; the history fetched on
select merges into it (held messages and older pages stay; a turn summary that
closed live wins over a fetch that read it running; turns older than the
fetched pages are not counted in the ledger twice). A resync keeps cached views
of sessions that still exist and drops their live scratch, as it does for the
selected view. Git and worktree reads run beside the stream, at launch too, so
they never hold up the next switch. Context reads (`GET …/context`, rebuilt by
Core on each `message.added`, `turn.summary` and `session.updated`) run beside it
one at a time, and requests made meanwhile coalesce into one more read per
session. `--bench WxH` also prints `switch_away_ms`,
`switch_back_first_frame_ms` and `switch_back_history_ms`.

**Turn details come from turn summaries** (`GET /api/session/{id}/turn`, Core
API 1.2; TURN_SUMMARY_PLAN.md). The TUI reads every page of them on open (200 a
page, at most 10 pages) and keeps them in `SelectedView::turns`:

| Shown | From |
| --- | --- |
| Turn footer and details: models, effort, tokens, cache %, duration, finish time | the turn's requests (`model`, `effort`, `usage`), `finished` |
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

`markdown.rs` draws assistant text, including the live scratch while it
streams: formatting appears as the deltas arrive, and an unclosed fence draws
as a code block until its closing line lands. These streaming frames are not
cached (every delta is a new body); the durable message is. Headings are bold: H1 and H2 in
`text.primary` (H1 with a muted `─` underline as wide as its text), H3 and
below in `text.secondary`. Inline code is `text.primary` on `surface.panel`,
padded by one space, never accent. A fence has no background, framed as in old Nexus: a `╭─ language` header and a `╰─`
footer in `text.muted`, each code line behind a muted `│ ` and drawn in
`status.warning` (ASCII: `+-` and `| `). A focused or hovered fence shows `copy` at the right end of its header
(a click copies the source). Lists use `•` and `◦`; block quotes sit behind a
`│` in `border.strong` with `text.secondary` text; links are `status.info` and
underlined, followed by `(url)` unless the target is a file path; task items
show `[✓]` (success) or `[ ]`. Mermaid uses the shared `nexus-mermaid` renderer: pie/Gantt/timeline
as text rows, other diagrams as the shared PNG through the terminal image protocol (half blocks
where none is available), and the source as the fallback. The box-art renderer stays here for
monochrome. Not done yet: OSC 8
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
right-aligned and coloured (`M` warning, `A` and `?` success, `D` error). Enter opens the
editable, syntax-highlighted source in a full-height modal, centred across the dimmed page. The header has
`‹ Back`, the path (basename bold), and `editing`, `● modified` or `changed on disk · reload`.
The vendored editor supplies line numbers (the cursor's line bright), quiet `▾`/`▸` fold marks and horizontal
scrolling for long lines; the wheel scrolls the text without moving the cursor, and a click or drag places it or selects;
NSH currently soft-wraps, so long-line wrapping parity remains an unresolved verification gate,
not an approved client difference. End reaches the insertion point after the final character. Markdown files (`.md`, `.markdown`)
open as source; `ctrl+shift+p` toggles rendered prose (Mermaid fences drawn as in the timeline) and Find returns to the source first.
When a session needs approval or an answer, its header shows `⚠ N waiting`.
The selected file can be copied as an absolute path, inserted as `@path`, or opened in
`$VISUAL`/`$EDITOR` with the terminal suspended. Core serves whole files up to 1 MiB.

The left Git tab (plan 40) shows the active worktree only (the open session's, else the main checkout): a
`folder · branch` line, then `N files changed · +A −D` and `S staged · U unstaged · T untracked`, then the changes
grouped by top-level folder (`src/  2`, `./` for root files), collapsed by default; Enter or a click expands a folder
to its file rows, which keep stage/unstage/discard and open the diff. With nothing changed it reads `No changes`.
There is no branch list (switch from the worktree picker). Line counts come from Core's per-change `added`/`removed`.

**Editor (K10).** The cursor starts at the top; `ctrl+s` saves and `ctrl+z`/`ctrl+y` undo and
redo. `ctrl+f` opens Find/Replace: Enter/Shift+Enter cycle matches, Tab switches between the
query and replacement, `alt+c` toggles case sensitivity, `ctrl+r` replaces one match and
`ctrl+shift+r` replaces all. Matches are highlighted. Esc ends Find first, then closes
the file, asking before discarding unsaved changes; Back and outside clicks use the same guard.
Save is `PUT …/file` with the revision the file was read at; a stale write keeps the text.
`editor.reload` (palette) re-reads, asking first when dirty; `files.changed` reloads a clean
editor and warns a dirty one. Opening another session or a Git diff is refused while the text
is unsaved. See [development.md](../docs/development.md#vendored-crates) for the editor crate.

**K5-K11 client parts.** The session switcher (`ctrl+k`) leads each session with the same status
glyph as its tab (running spinner, attention/error dot or idle dot), then the title and age, and also
lists sessions whose user messages match (Core's `/api/search`) after the title matches, with
the snippet in the hint column. `/search` opens the same switcher with the search field focused and
empty, or prefilled by `/search <text>`; typing filters immediately. A
session opens with its newest 200 messages; Enter on `older messages are not loaded` (or the
palette's Load older turns) reads the page before the oldest. A running session loads every older page on its own, and pages already read stay cached across snapshot resyncs. The approval card offers `y` allow
once, `a` allow for the session, `n` deny, `d` deny with the composer's text as the note.
Choosing a model or agent for an existing session first asks `…/switch-cost` and confirms when
the cache would be lost. `/usage` shows provider usage (`GET /api/usage`): every connected provider, with plan
windows where it reports them, in a table of ten-cell used gauge, percent left, reserve and expiry. Reserve is
available for windows of at least seven days with a future reset. The modal hides Codex limit-reached and xAI
per-product notes, window details, xAI prepaid credits and Copilot chat, completions and AI-credit details.
Core stores the latest snapshot and refreshes it every 10 minutes by default;
Settings → Context changes the interval, and `r` refreshes it on demand. Use ↑/↓ or Page Up/Down to scroll the table; Esc closes the modal without waiting. A tool result's images
are named under its output (`▣ image name · mime · artifact id`); a terminal cannot show them.

Responsive layout (D17, `layout::Breakpoint`): from 160 columns both sidebars are docked at 34
and 36 cells; at 120-159 they are 28 and 30; at 100-119 the left stays docked and the right is
closed until opened (the toggle docks it; a saved preference wins); below 100 both are drawers
of 34 and 36 cells over the page, which `paint::scrim` dims (a click on the scrim closes the
drawer). Under 100 the header keeps the two toggles, the run status and the bell, and the tab
strip gives way to a breadcrumb of the viewed session cut from the left (`… › child`). The old subagent-tab row is gone: a viewed child keeps the root tab at the top and uses the heading rail and its timeline child cards for navigation.

States (D18): launch shows the splash (`nexus-tui/src/splash.rs`): the `_/` logo in
`text.primary` (white in the dark theme). There is no progress bar, percentage or step name: the
ambient dots, intro particles and a light sweep across the logo run on the clock, so the launch
moves from the first frame (painted before the terminal capability probes); after 5 s of waiting one
quiet `Starting Nexus Core…` line shows. The milestones (`daemon` … `ready`) are tracked only to
know when `ready` is reached.
When the stream is live the logo flashes and a `▓▒░` curtain wipes into the first frame; with
animations off the logo is static and the frame follows with a cut. A failed start
dismisses the splash so the error is readable. After launch, the connecting screen and the
empty workspace show the same logo (`paint::LOGO`, five rows of `_/` strokes, accent, bold in
mono, centred as one block) over the title. An empty workspace reads `No projects yet` with
`alt+3 add a project · ctrl+p commands`
(the manifest has no key for adding a project yet, so the hint names the rail's `+`). A lost
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
view never notifies. Settings › Notifications › Notification sounds (on by
default) plays a rising cue when a root session and all its subagents have finished, and
a higher cue when one needs input; `NEXUS_COMPLETION_SOUNDS=off` silences both.

The right sidebar (D12) has SESSION and CONTEXT sections: UPPERCASE bold muted
labels, a 9-cell key column and values truncated in the middle. CONTEXT shows the
`╱` meter with its percentage, `82k of 200k`, `12k in · 2k out` and Cost, each
reading `not reported` where Core gives nothing (PLAN.md Step 12a).

Timeline rhythm (DESIGN_PLAN.md D04-D07): the user message is a band across the
whole page width on the raised surface (rail `▌` at its edge, muted or the focus
colour, the fold marker inside, `#n · time` at the right, `copy` on hover or
focus; only its left edge folds it, and a click on the text just selects). When
its text has scrolled off the top, `sticky.rs` pins a copy under
the header (three rows at most, a `░` shade row with `↑ N lines of this turn
above`); the next band pushes it up and a click scrolls back. The timeline keeps one
scroll per session and adopts it after a sync-driven selection, so a root's pinned
band never appears over a child. A session's first message is pinned: once Core confirms it, the timeline scrolls so that message's turn starts at the top and a spacer (blank rows, never drawn) lets the view reach it even while the reply is empty. The reply grows below without moving the view, the spacer shrinks as it grows, and the pin ends the moment the reader scrolls. Only the first send pins; later sends follow the end as before (`FirstPin` in `app.rs`, applied in `draw_timeline`). While the reader scrolls, a subdued
`interactive.scrollbar` bar shows in the timeline's right gutter and fades out after
about a second; Settings → Appearance → Scroll bar turns it off. It never takes
content space or shifts the layout. The turn footer is
the right-aligned `▪ Agent · models · effort · tools · duration · tokens · cost`
row with no end status or continue hint: a failed turn puts its first error line
(`✕`) above, and the usage units (`i`, `o`, `r`, `c`) are fainter than their
counts. Durations read `0.4s`, `4.2s`, `1m 05s`, `1h 02m`. Activity groups fold to
`3 thoughts, 50 commands, 8 tools, 9 reads` with `+N −M` and `✕ N failed`; a live group is
`Working` (shimmer) over its newest five members and `… N earlier`, and its glyph
flashes the accent for six frames when it finishes (not with reduced motion).
A parent's `fork` call (PLAN.md section 10, F6) is a minor task run like a tool
call, so it is a tool member, not a child card: `Fork ‹title›` in its activity
group, counted as `N forks`; open, it shows the task (as a command's rows) and
then the report the parent received, and an `╰ open fork ↗` row (its own
`Child` block, so Enter or a click opens the fork's session) once the fork's
session is known. As in NSH.
A fork (whose session JSON carries `fork: {parent_id, inherited}`) is labelled
`Fork` in the heading metadata. Its timeline folds the first `inherited`
messages, copied from the parent, into one group row at the top,
`▸ Forked from ‹parent title› · N messages` (the parent id when the parent is
unknown), folded by default and toggled like a group (Enter/Space, a click,
Left/Right). Open, the copied messages render as usual, three cells in under
the chevron; the `[Nexus fork]` task message after them is a normal user band.
A ZMX child (session JSON `worker`, [docs/external-workers.md](../docs/external-workers.md)) is labelled
`ZMX · Claude Code` or `ZMX · Antigravity` on its parent's child card and in its heading. Its timeline
opens with the ZMX card instead of the starting context: `context incomplete`, then `Needs attention`, `Denied by the CLI`,
`Run`, `Settings` and `Not applied` rows as the worker reports them (shared `interface/scenarios/zmx-*.json`). Claude Code's
own subagents show read-only inside the calling tool's card. As in NSH.
Members lead with a verb glyph (`→ ← $ * λ ? ↳ ↗`, ASCII `> < $ * L ? ^ ^`,
from `paint::verb_glyph` and `[glyphs.verb]` in `theme.toml`) that the status
glyph replaces while running, failed, denied or waiting. Opening a member shows
the old box (`┌ │ ╰` in `border.strong`): the command's first two rows, the last
eight output rows behind `↑ N earlier lines · enter show all` (a diff or the raw
view keeps a head and a tail), and a `raw` toggle on the head row. `o`, or Enter
on the open tool, shows all; `r` raw and `i` inspector work on a focused tool.
Reasoning reads `◇ Thought · preview` (its own icon where a group would show a
fold; within an activity tree the `◇` follows the tree glyph, ASCII `o Thought`;
no duration, see Interface notes) and strips raw Markdown emphasis from the
one-line preview. It stays open with its last ten wrapped lines while it streams,
with a count of earlier lines hidden. Once finished, opening it shows the full
text. A finished background subagent is one row, `⣿ Subagent done · Agent · title`
with `failed` or `interrupted` in the error colour; Enter opens a modal with the
agent, title, model, duration and the exact notice text the model received, and
`enter open session` opens the child. Other context notices keep
`context · … · show details`. The header status reads
`◆ waiting for you` or `✕ offline`; it has no busy state.

The right sidebar's Session
info totals turns, calls, tools by name, tokens, a context bar against the
model's window and the catalog cost. Right-click (or `m`) on a sidebar row opens
its action menu; the Projects view (`alt+3`) ends with `+ Add project` (an inline path
field) and removal asks first. Settings shows provider connections and
per-subagent model lists in the right pane. Every row it shows can change.

Still Step 12.4: the vendored code editor in the file panel, worktree management
in the UI, `path:line` links and steering/queueing
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
NEXUS10_REDUCED_MOTION=1 cargo run -- --dark --bench 160x48 --replay
                           # no Core: replay a deterministic 200-message / ~900-event stream
                           # through the real batch policy; prints burst and paced frame counts,
                           # timeline build time, cache hits/misses and chrome-only frames.
                           # NEXUS_TUI_REPLAY_MESSAGES and NEXUS_TUI_REPLAY_EVENTS set the fixture;
                           # NEXUS_TUI_REPLAY_TOOLS=1 adds long edit diffs and opens high detail.
```

Display options (flags override the environment): `--dark | --light`, `--mono`
(also `NO_COLOR` or `TERM=dumb`), `--256 | --16` (otherwise `COLORTERM=truecolor`
means truecolor), `--ascii` (also `NEXUS10_ASCII=1` or a non-UTF-8 locale),
`--reduced-motion` (also `NEXUS10_REDUCED_MOTION=1`: spinners stay still and an
idle terminal is never redrawn).

**The loop draws only when the model changed.** One sync message wakes the
select; up to 64 queued sync messages then join it within an 8 ms soft budget
(`src/frames.rs`). Ready input, a due tick (125 ms) and image/done/dictation
replies are barriers, so input is always rendered before the next input and
effects run in order between items. `wire_event` and `wire_response` still
advance the reducer's cursor but need no frame; every other event kind is
conservative. A finite burst's last visible state is drawn before the loop waits
again, and an invisible-only burst does not clear a pending draw.

**Rendered bodies are cached per app** (`timeline::LayoutCache`). Markdown
bodies (including fenced-code block offsets), wrapped plain text and unclipped
tool detail rows are keyed by their content, width and a resolved theme
fingerprint; focus, selection and clipping stay outside the cached value.
Entries are FIFO-evicted under a 32 MiB cap, and the replay reports hits,
misses and evictions. The built timeline itself is reused when only chrome
changes (typing, scrolling, idle ticks, toasts); `App::timeline_for` hashes the
model revision, viewed session, width, theme, focus/selection, fold/raw state,
detail and animation phase, and conservative sync-message revisions keep child
cards and waits from going stale. This is what keeps a 200-message draw near
1 ms, a 1,000-message draw near 5 ms and chrome-only frames near 0.4–0.5 ms in
the release build; `--bench ... --replay`
measures it without a Core or PTY (results in
`git show e438b61:TUI_PERFORMANCE_PLAN.md`; remaining work in [../plans/](../plans/README.md)).

Session rows in the left sidebar (D03) have two lines. Line 1 has a mark slot
(waiting `◆`, error `✕`, running spinner, unread `●`, in that order of
precedence), a space, the title (bold when open, ending in `…` when clipped) and the
time of last activity right-aligned (`now`, `3m`, `4d`). Line 2 is the agent in its
colour, `·` and `⌥ branch` muted, with the descendant-session count (`2 ↳`) right-aligned. The
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
It holds `ui.left_width`, `ui.left_open`, `ui.right_open` and
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
| `NEXUS10_HOME` | Nexus home (default `~/.nexus`). The default is refused until `nexus migrate` has marked it (old Nexus or a pre-release `~/.nexus10` is there). |
| `NEXUS_TUI_CORE_CMD` | Command that runs the Core CLI, default `nexus`. Used as `<cmd> daemon start`, for example `uv run --project ../nexus-core nexus`. |
| `NEXUS_TUI_NO_START` | If set, never start Core; report offline instead. |

Dependencies are pinned to exact versions in `Cargo.toml` (`=x.y.z`) and
`Cargo.lock` is checked in. Rust 1.97+: the shared `nexus-mermaid` crate's
`raster` feature is enabled here, and Ondine's compiler requires 1.97.

Try it against a real isolated Core:

```sh
export NEXUS10_HOME=$(mktemp -d) NEXUS_PORT=4799   # any free port
chmod 700 $NEXUS10_HOME          # Core requires a private home
NEXUS_TUI_CORE_CMD="uv run --project ../nexus-core nexus" cargo run
```

## Prompt history search

`Ctrl+R` (`composer.history_search`, the same command and key as NSH) opens the palette in a prompt-history mode: this project's earlier prompts from Core (the list Up/Down recall walks), newest first and each once, filtered as you type. Enter replaces the draft with the chosen prompt and focuses the composer; Esc leaves the draft alone. Reconnect to Core is on its leader key (`Ctrl+X` then `r`), the banner button and the palette; `Ctrl+R` no longer reconnects. Images appear as `[N images]` text, as they do in Up/Down recall.

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

Settings → **Tools** shows the **Web search service** (the local SearXNG behind
`web_search`: running, stopped, not set up or unavailable, with its address and
runtime) and a **Start web search** row, also in the palette as `search.start`.
Starting calls `POST /api/search/service/start` (Apple `container`, else Docker)
and waits until it answers. The status refreshes when Settings opens.

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
| `reduce` | Pure `Model` (projects, sessions, running turns, waits, streaming text, selected session's messages, turn summaries, spend ledger and the running turn's live events, plus cached views of recently viewed sessions): `apply_snapshot`, `apply_history`, `apply_turns`, `apply_event` (`turn.summary` replaces the scratch), dedup by durable id, generation check. |
| `prefs` | `<home>/tui.json`: the TUI's own client preferences, load, atomic 0600 save, merge of changed fields. |
| `discovery` | `daemon.json` under the Nexus home, `/api/info` verification, `nexus daemon start` bootstrap, daemon log tail. |
| `sync` | The connection supervisor task: handshake, snapshot, SSE, reconnect with backoff, resync, restart detection. Emits `SyncMsg`, takes `Command`. |
| `state` | `ConnectionState`, `Banner`, `AppState` (model + connection + banners; `apply(SyncMsg)`). |
| `manifest`, `theme` | `interface/manifest.toml` and `interface/theme.toml` embedded with `include_str!` and parsed into typed structs: commands, keys, Esc chain, navigation, settings; colour tokens per mode and tier (truecolor, 256, ANSI-16, monochrome), glyph tables (Unicode/ASCII). No command list, key, reason, colour or glyph is copied by hand. |
| `completion` | Ranking (`rank_menu`, typo distance), acceptance text and the per-token reply cache for the composer's completion popup, copied from old Nexus. |
| `dictation` | Local `cpal` capture on its own thread, mono conversion, streaming resampling, PCM16 WAV encoding, cue tones, waveform levels and preview pacing. No device handle crosses threads. |
| `text`, `keys`, `composer`, `editor` | Display-width helpers, key names, and the multi-line composer. The grapheme editor and visual layout are adapted from old Nexus; composer movement and deletion respect grapheme boundaries. The composer uses the editor
for selection, undo/redo and collapsed large pastes; a send keeps the marker and sends the full text separately, and the message renders the marker as a clickable chip. |
| `ui`, `layout` | View-state types (focus, overlays, dialogs, alerts, mouse targets). `Regions::compute` is the one geometry function; the renderer registers every clickable rect in a `HitMap` as it draws it and the map is rebuilt each frame. |
| `app` | Focus model, `when`-term evaluation, command availability, the palette, the Esc chain, keyboard/mouse/paste handling. Produces `Effect`s (API call, microphone capture, supervisor command, clipboard, quit, suspend). |
| `actions` | Core actions (prompt, session changes, voice status/download/transcription, and others), each with its own `RequestId` where applicable; failed sends restore their draft. |
| `timeline` | The session timeline: blocks in model order (starting context, turns, user/assistant text, reasoning, tools and their diffs, notices, diagnostics, child cards) with fold state and the list of selectable blocks. Folding is view state only. `LayoutCache` keeps rendered bodies, wrapped text and tool detail rows keyed by content, width and theme, with a bounded FIFO cache shared by every frame in one app. |
| `paint` | Buffer-level drawing helpers: `scrim` (dim a buffer under a modal), `modal_rect` (the modal block's geometry), `meter`. |
| `render` | Ratatui drawing from the model, theme and manifest. Timeline blocks become hit targets as they are drawn. `render_text` renders to a text grid for tests and frame dumps. |
| `term` | Raw mode, alternate screen, mouse, bracketed paste, kitty keyboard flags; restored on error, panic (hook) and Ctrl+Z suspend/resume. |
| `frames` | Batch policy for the interactive loop: tick interval, soft budget, message bound and the dirty/halt result of a batch. |

Tests: unit tests live in the modules; `tests/timeline.rs` covers turn
grouping and footers, folding (including "all but last"), tool diffs and the raw
toggle, history merging, and keyboard/mouse block selection; `tests/frames.rs`
drives the production batch policy with deterministic queues and compares
batched replay with per-event rendering (buffer, hit map, scroll and model);
`tests/sync.rs` runs the supervisor
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
- **View reporting.** Each `/api/event` connection carries its own ephemeral
  `stream_id` on `server.connected`. When the selected project, session or panel
  changes, and again after every reconnect, the supervisor sends
  `POST /api/client/view` with that id, a per-process `client_id` and the view:
  project and session ids plus a panel label (`files`, `file` or `diff`; none
  otherwise). It never sends paths, drafts, scroll position or screen contents.
  One report runs at a time in a background task, so event consumption never
  waits for it and a later view always supersedes an earlier one. A transient
  failure is quiet and retried after 1–30 s; a real view change sends at once.
  The stream id is a capability and is never logged, shown or printed by
  `Debug`; `stream_closed` drops the id and `client_mismatch` reopens the stream.
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
the toggles stay when a sidebar is closed. Left tabs Sessions, Files
with Settings fixed at the bottom; right sidebar SESSION and CONTEXT
(Git lives in the left sidebar's Git tab only). Below 100 columns the sidebars become drawers
opened from the same toggles. The waiting card sits directly above the composer
(DESIGN_PLAN D14): a heavy accent rail, `Allow bash?`, `waiting on you · 0:42`, the
parameters (and, for edit, write and patch, the diff capped at 12 rows), and a choice
list with key chips (`y` allow once, `n` deny; Up/Down and Enter also choose; choices
Core does not offer show their reason). Toasts (D15) are top-right under the header, three
at a time with `+N more`; a click, or Enter on the focused toast (`F6` to it), opens
the full text in a modal (`c` copies, Esc closes), and the notifications modal opens the
same text for an entry. The `↓ N new · end` pill sits on the row above the composer card.
The card also answers `ask` questions and takes the keys on arrival, so typing goes straight
into its answer. Questions, option labels and previews wrap fully, and Page Up/Page Down
scroll the card. Every page has a custom answer field in the card, shown as the answer
line (no filled selection background); typing in it answers in your own words or adds
context to a selection, and each question keeps its own text. A free-text question (or one
that allows free text) focuses the field immediately with a visible caret. A
number key (1-9, 0 for the tenth) or Up/Down picks an option, clicking an option picks
it, and `Alt+A` moves between the option list and the answer field. **With exactly one
question**, Enter on a single-choice option (or a click) sends the answer at once; a
multi-select question still needs its Enter to say "done", but also sends without a
review page. **Two or more questions** keep the step-through: Enter locks the answer and
moves to the next question, the last leads to a review page whose rail is
`text.primary` (white in the dark theme), and Enter there sends the reply.
`Alt+Left`/`Alt+Right` move between the questions and review, while Left/Right move the
caret in the answer field (or page when the option list has focus and the field is
empty). Space ticks options of a multi-select question, whose card uses the violet
`interactive.multi` rail and ticks. The reply is sent once, as a keyed
`POST …/question/{id}` (`Ctrl+Enter` submits from any page). While a question is pending
in the viewed session the composer is disabled and says `Answer the question above`: it
takes no typing, paste, send, steer or queue, and the draft already there is kept and
returns when the question is answered. Questions in other sessions do not lock it; Esc
still follows the manifest chain (while busy it interrupts, which cancels the question).
Tab and Shift+Tab switch between parallel asks, each of which keeps its own answers.
Settings opens in a centered
modal using old Nexus's panel geometry and a searchable root/drill list. Press
`/` to filter settings; Enter opens a section, and Esc returns to the root
before closing the panel. Existing Core-backed controls retain their own focus
and keyboard handling.

Keys come from the manifest (`F1` lists them; the palette shows each command's
key). `Shift+Tab` cycles the composer's eligible agents (primary agents, or subagents
in a child) and wraps; an existing session uses the same next-call switch and cache
warning as the agent picker. New-session draft selections of agent, model and effort
update silently, without a toast.
Beyond the manifest: `F6`/`Shift+F6` cycle focus, `Ctrl+J` inserts a
newline where `Shift+Enter` is not reported, `Ctrl+C` copies a composer
selection, or clears the draft and keeps the cleared text in Up history; twice
within two seconds quits (`Ctrl+Z` undoes in the composer and suspends
elsewhere, and `Ctrl+Y` redoes).
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
  turn. One completion shows `$` in place of the fold and drops the `→` prefix;
  consecutive completions stay one group with `▸`, and expanding shows the full
  context in order, including any output clipping notice.
- Clipboard paste: **Ctrl+V** in the composer in a local macOS or Linux
  terminal reads the clipboard once: an image if present (it wins when the
  clipboard holds both), otherwise text, otherwise the toast "The clipboard is
  empty". Text takes the bracketed-paste path, so long text collapses into the
  paste preview and a pending question card keeps its lock (up to 1 MiB; macOS
  uses AppKit for images and `pbpaste` for text, Linux uses `wl-paste` or
  `xclip`, and a missing tool is named in the error; over SSH the read is
  refused: use the terminal's paste shortcut). An image paste inserts `[Image A]`,
  `[Image B]`, … at the cursor. The composer shows a square thumbnail for each
  attachment above its input, with the muted name above the thumbnail; deleting
  a marker removes its attachment. Clicking a thumbnail opens the drawing modal:
  drag to draw, `1`-`4` or the swatches pick a colour, `u` undoes, `x` clears, `enter`
  or Done merges the marks into the image through `nexus-annotate`, and `esc` cancels.
  Only the marked-up image is sent. Ghostty run directly gets pixel-precise, live
  strokes (SGR 1016 mouse, kitty graphics tiles; `src/annotate.rs`). Elsewhere, and
  under a multiplexer, the pen follows cells and the picture redraws when a stroke ends. Up to eight images, 8 MiB combined; image-only
  sends work and a failed send restores the images. In the timeline the
  `[Image A]` marker stays ordinary message text (no accent colour) and the
  generated attachment labels are not repeated after the text. Attachments sit side by side above the message in bordered square thumbnails with their
  muted names above them; the sticky user header never shows them. Clicking a
  thumbnail opens a full-size preview (`ratatui-image`: kitty, iTerm2 or sixel
  where the terminal supports it, half-blocks otherwise).
- A large text paste (at least 500 characters, regardless of line count) collapses into one
  `[Pasted ~N tokens]` marker at the cursor, as in the old editor; pasting the
  same text again expands the marker in place. The composer shows a preview row
  for each collapsed paste, with `Show` opening the full text, as in NSH. Sending
  sends the marker and the full text separately: Core stores the text as a
  collapsed part, so the model reads it while the user message shows only the
  marker. In the message the marker is a clickable chip above the text, with no
  inline content; clicking it opens the full text in a modal. A failed send
  restores the marker. Smaller pastes stay inline.
- The top header omits the busy spinner, working label and elapsed time.
- The model picker shows model names on the left and providers on the right;
  `m` toggles details below the picker (they follow the selection) and is
  reserved for that while the picker is open.
- The startup draft shows its estimated starting context under the same Context
  heading as an opened session. Expanded Context uses spaced label-and-count
  rows, with the tool inventory and full prompt contents in the section details;
  estimates use `~`, Skills and MCP are labelled unavailable until Core supports
  them, disabled tools are counted, and the system prompt opens in its detail
  view without an inline preview. Draft tool switches remain visible when off
  and while Core recalculates the estimate, so they can be turned back on.
  The starting context stays expanded.
- The composer supports mouse and Shift selection, Alt/Control+Left and Right
  word movement (with Shift to select), Alt+B/F and Control+Home/End. It has one
  row of top padding above the input and a half-height bottom edge. Its status
  shows the latest reported input tokens, the percentage of any known
  pricing-tier boundary and of the context window; hovering explains them. The
  location strip keeps the current path and branch, centered gray indicators for a live
  kernel, running background shells and terminal shells when present. The kernel and
  background-shell indicators open separate detail lists; terminal shells open the dock,
  and a rule below it shows context occupancy when idle (against the first
  pricing tier until reached, then the window); during a turn an
  agent-coloured segment moves along it (stationary with reduced motion, a
  heavier glyph in monochrome).
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
  (sessions), Ctrl+L (right sidebar), Ctrl+T (cycle effort), Ctrl+R (prompt history search);
  Ctrl+X then M, N, O/B, G, S, L, T, R or ? opens model, new session, sessions,
  agent, settings, sidebar, effort, reconnect or shortcuts, and Ctrl+X Shift+N
  notifications. Shortcuts for capabilities Core lacks are omitted.

Settings reads the shared defaults, timeline detail, reduced-motion choice and disabled tools from Core's `/api/config`.
Use left/right or Enter to change a choice or tool switch; Core checks the file
revision before each write. Configuration errors are listed at the top of
Settings, and rejected writes appear as warnings. A `config.changed` event
refreshes Settings after an external edit.
The session context meter reads Core's context budget; `~` marks estimates.

## The Manager

The header's **Manager** label toggles a borderless, fully opaque notification dropdown: caption, recent model-context lines,
current tool/status, the Manager's card, **Tell the Manager…** input and **Open Manager session**. While it or a
call is open, a one-row field of braille cells replaces the label: three slow sine waves travel across ten cells,
lighting only a few dots. Idle, thinking, listening and speaking change the speed and brightness; idle and thinking are gray, listening is
drawn in `manager.listening` and speaking in `manager.speaking`, and their height follows the measured voice level.
Reduced motion freezes the field (still in the speaker colour) and ASCII terminals get one state glyph. Click the field to toggle the dropdown; an open
dropdown dims everything outside it. With the live voice on, hovering the field shows **Stop** and clicking it stops the
voice (`/api/manager/live/stop`) while the control call, backend session and typed field stay.
The dropdown has two voice buttons: **Connect** / **Disconnect** (connected by default: opening Manager or Connect starts Live immediately; Disconnect stops only audio) and **Mute** / **Unmute** (live voice only; keeps the connection, sends no microphone audio, wave gray and still, caption "Muted"; Disconnect clears it). The input has a solid inset surface; debug navigation does not start audio. Enter sends text; with the voice disconnected, the first typed request starts a control
call with audio disabled. Esc/outside click closes the dropdown while audio continues. Automatic cards do not steal
keyboard focus. With the dropdown closed, session keys work normally. The explicit stop shortcut or `/manager stop`
ends the control call. The debug button reuses the current persistent Manager session and follows episode rollover;
it shows canonical context inside Nexus with its composer disabled. The palette's Manager context view and the raw
inspector remain available. See [Manager](../docs/manager.md).

Live uses its own audio connection and does not require Dictation setup. The header indicator appears immediately with
**Connecting…** in the dropdown. Playback uses an adaptive jitter buffer (120 ms, growing by 60 ms per mid-speech underrun up to 300 ms for that call) and the speaking state holds through
350 ms pauses. Both clients draw the same braille field; real audio quality and talk-over interruption still need listening checks.

Working labels use the shared travelling shimmer: one pass every 1.5 seconds,
with a brighter highlight over secondary text. Reduced motion keeps the label static.

Settings sections have no gaps between rows. Selected sections and setting rows use a
neutral gray highlight across the entire row, including its value. A white left bar marks the focused pane (dark in the light theme);
←/→ move the focused segment; Tab returns focus to the section heading without
changing the setting. Shift+Right steps a value backward; Right advances it.

Live starts the microphone before reporting audio ready. Its echo guard follows audible playback and a short echo tail; continuous silent output does not mute capture. Expected speaker shutdown is quiet.

Running responses use request-local streaming checkpoints: after subscribing,
the client reads `/turn`, seeds the running partial, and merges newer deltas by
request id and sequence. A completed part replaces its folded text; a saved
assistant message clears scratch. Token deltas have no durable cursor. Finished
session compaction leaves messages, starting context and turn summaries intact.


The Files command palette offers New file, New folder, Rename file or folder,
and Delete file or folder. Paths are relative to the displayed workspace root.
Delete is permanent and asks for confirmation; a folder's descendant count is
shown when Core can count it within its safety budget.

Known text terminal types (`TERM=dumb`, `linux`, `vt100`, `ansi`) skip the image
capability query at launch; other terminals keep automatic protocol detection.

Desktop Files and editor views renew a 60-second workspace watcher lease while
visible and release it on close. Disk changes refresh the file tree; clean text
follows its disk revision, while unsaved edits remain intact with a changed-on-disk
notice. Native watcher failure shows **Polling every 3 seconds**. These global
resource events never enter a session's model context.
