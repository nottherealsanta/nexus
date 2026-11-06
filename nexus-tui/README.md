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

**Open inspector** now opens the native inspector modal, shared with NSH,
instead of a browser. Views: `1` Timing (Gantt with parallel descendants), `2`
Conversation, `3` Steps, `4` Requests, `5` Raw. The header shows the selected
session's lifetime input/cached/uncached tokens and cache-hit percentage;
unreported usage and cache writes say unknown. `r` refreshes the snapshot,
`n`/`p` page older/newer, `+`/`-` zoom, `h`/`l` pan, `j`/`k` select a timing
lane, Enter toggles its retained JSON, `c` copies, arrows/Page Up/Page Down
scroll, Esc closes without changing the session or draft.
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
project with no session shows its own branch. The composer is a soft card
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
available consonant, then a numeric suffix; the first character stays centred. The rail has one empty row above its projects. Running or recently active projects
(within one hour) are lighter gray. Each tile has a counter in each corner:
attention (purple) and unread (blue) on top, running (green) and idle (white) below,
counting root sessions, with archived sessions left out and subagents counted under
their root. Counts above nine display `*`, and a project with no counts shows a
three-cell muted rule. Hovering shows the full project name and `N attention`, `N unread`, `N running` and `N idle` right beside the rail, over the sidebar when it is open. A `+`
ends the list and adds a project; Settings sits at the rail's foot while the sidebar
is closed. A right-click opens the project's actions. Activating a project restores
its open tabs and last selected session or new-session view in this client; pressing
the project already open toggles its Sessions sidebar. On first
selection it falls back to its newest unarchived root session or an empty new-session
view. New-session composer text and images are kept separately per project, for the
client process. The left sidebar (D03) lists the current project only (`ctrl+k` opens
the switcher). Its archive icon above the search shortcut includes archived sessions alongside active sessions when enabled.
You can type and send in an archived session: **Resume archived session** warns
that a large uncached context may be slower and cost more, and that code kernels
restart fresh. Cancel keeps the draft; Resume unarchives the whole tree before
sending. Explicit Unarchive shows the same warning without sending a message.
The list has `⌥ branch  ~/…/dir`
sub-groups for linked worktrees, and two-line rows (mark slot, title, time;
`⌥ branch`, `2 ↳`) ending with `+ New session`, whose label starts on the session titles' text edge; `Settings` is pinned at the bottom.
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
composer; their text starts where the input starts, with a highlighted `Q` for a
queued message or `S` for a steer one cell to its left. A held queue says `held`
after the message.
The header holds session tabs (`×`, `+`, `alt+x`, `alt+[`, `alt+]`) in one fixed-width cell for every tab, root or New session, so the strip keeps its rhythm and a long title ellipsises; the viewed tab is bold on the page surface, the others muted on the band. A viewed child's strip under the header shows its path as equal parent tabs, a rail, then the root's other sessions as equal tabs with state glyph and agent, the viewed child selected. `alt+d`
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
Consecutive diagnostics (a provider retry sequence) form one tight stack with
delays rounded to tenths.

The timeline is complete for one session: turn headers and footers, folding,
collapsed reasoning, old-Nexus CommonMark text (supported Mermaid diagrams draw
as shared text rows or the shared PNG, with a clickable label to copy their
source; unsupported diagrams show the source), tool-specific expansion (diffs, output
caps, raw arguments/results), child cards, context notices, diagnostics, and the
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
right-aligned and coloured (`M` warning, `A` and `?` success, `D` error). Enter opens a
read-only file view: the header row has `‹ Back`, the `~` path (the
basename bold), `e edit`, the Markdown `p source`/`p preview` control and `lines 1-38 of 38`
muted at the right, a blank row follows, then
right-aligned line numbers and a `│` gutter; Esc or Back returns to the timeline. It opens
in a centred modal over the dimmed page (the page keeps its layout); a click outside closes it, and long lines wrap.
Markdown files (`.md`, `.markdown`) open as rendered prose; the header's `p source` control
shows the numbered source, and `p` switches back to the preview. Find returns to the source first.
When a session needs approval or an answer, its header shows `⚠ N waiting`.
Resizing keeps the open file, search and scroll. The selected file can be copied as an absolute
path, inserted as `@path`, or opened in `$VISUAL`/`$EDITOR` with the terminal suspended. In the
file view `/` opens the search on the bottom row (always reserved, so the text does not move):
`/ query  3 of 12   n next · N prev · esc end`. Matches are on the selection band and the
current one is reversed; typing filters, Enter or `n` goes to the next match and `N` to the
previous one, and the first Esc ends the search while the next closes the file. Core serves
whole files up to 1 MiB and has no offsets, so the header gives the exact range shown. The
read-only view has no syntax highlighting yet; the vendored editor is wired next.

**Editor (K10).** `e` in the file view starts the editor over a `Composer` buffer (cursor at
the top, `ctrl+s` saves, `ctrl+z`/`ctrl+y` undo and redo, Tab inserts four spaces); the header
says `editing`, `● modified` or `changed on disk · reload`. Save is `PUT …/file` with the
revision the file was read at; a stale write keeps the text. `editor.reload` (palette) re-reads,
asking first when dirty; Esc leaves a clean editor and asks before dropping changes;
`files.changed` reloads a clean editor and warns a dirty one. Opening another session or a Git
diff is refused while the text is unsaved. `vendor/ratatui-code-editor` is checked in for the
step that replaces this buffer; see [development.md](../docs/development.md#vendored-crates).

**K5-K11 client parts.** The session switcher (`ctrl+k`) leads each session with the same status
glyph as its tab (running spinner, attention/error dot or idle dot), then the title and age, and also
lists sessions whose user messages match (Core's `/api/search`) after the title matches, with
the snippet in the hint column. A
session opens with its newest 200 messages; Enter on `older messages are not loaded` (or the
palette's Load older turns) reads the page before the oldest. A running session loads every older page on its own, and pages already read stay cached across snapshot resyncs. The approval card offers `y` allow
once, `a` allow for the session, `n` deny, `d` deny with the composer's text as the note.
Choosing a model or agent for an existing session first asks `…/switch-cost` and confirms when
the cache would be lost. `/usage` shows provider usage (`GET /api/usage`): every connected provider, with plan
windows where it reports them. The modal hides xAI prepaid credits and Copilot chat, completions and AI-credit
details; other providers' corresponding rows are unchanged. Core stores the latest snapshot and refreshes it every 10 minutes by default;
Settings → Context changes the interval, and `r` refreshes it on demand. Esc closes the modal without waiting. A tool result's images
are named under its output (`▣ image name · mime · artifact id`); a terminal cannot show them.

Responsive layout (D17, `layout::Breakpoint`): from 160 columns both sidebars are docked at 34
and 36 cells; at 120-159 they are 28 and 30; at 100-119 the left stays docked and the right is
closed until opened (the toggle docks it; a saved preference wins); below 100 both are drawers
of 34 and 36 cells over the page, which `paint::scrim` dims (a click on the scrim closes the
drawer). Under 100 the header keeps the two toggles, the run status and the bell, and the tab
strip gives way to a breadcrumb of the viewed session cut from the left (`… › child`). The subagent strip under the tabs draws every tab at one width of 10 to 30 columns (the widest title, plus a child's agent, +2); when the row is too full it first thins tabs (the oldest ancestors from the left, with a `…` marker, then the children farthest from the viewed one) and finally narrows the cells.

States (D18): launch shows the splash (`nexus-tui/src/splash.rs`): the `_/` logo in
`text.primary` (white in the dark theme), a 30-segment milestone bar, its percentage and the
next step. The milestones are `daemon`, `workspace`, `providers`, `session` and `ready`; the
bar follows them, while the ambient dots and intro particles run on the clock; the logo is
painted from the first frame, before the terminal capability probes.
When the stream is live the logo flashes and a `▓▒░` curtain wipes into the first frame; with
animations off the logo and bar are static and the frame follows with a cut. A failed start
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
Members lead with a verb glyph (`→ ← $ * λ ? ↳ ↗`, ASCII `> < $ * L ? ^ ^`,
from `paint::verb_glyph` and `[glyphs.verb]` in `theme.toml`) that the status
glyph replaces while running, failed, denied or waiting. Opening a member shows
the old box (`┌ │ ╰` in `border.strong`): the command's first two rows, the last
eight output rows behind `↑ N earlier lines · enter show all` (a diff or the raw
view keeps a head and a tail), and a `raw` toggle on the head row. `o`, or Enter
on the open tool, shows all; `r` raw and `i` inspector work on a focused tool.
Reasoning reads `▸ Thought · preview` (or `◇ Thought` within an activity tree,
ASCII `o Thought`; no duration, see Interface notes) and
stays open with its last ten wrapped lines while it streams, with a count of
earlier lines hidden. Once finished, opening it shows the full text. The header status reads
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
`git show e438b61:TUI_PERFORMANCE_PLAN.md`; remaining work in [../PLAN.md](../PLAN.md)).

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
The card also answers `ask` questions: `Alt+W` focuses it and the question wraps to at
most two lines. Every page has a custom answer field in the card; typing in it answers in
your own words or adds context to a selection, and each question keeps its own text. A
number key (1-9, 0 for the tenth) or Up/Down picks an option, clicking an option picks
it, and `Alt+A` moves between the option list and the answer field. Enter locks the
answer and moves to the next question; the last question leads to a review page whose
rail is `text.primary` (white in the dark theme) and which lists every question with its
answer, where Enter sends the reply. `Alt+Left`/`Alt+Right` move between the questions
and review, while Left/Right move the caret in the answer field (or page when the option
list has focus and the field is empty). Space ticks options of a
multi-select question, whose card uses the violet `interactive.multi` rail and ticks.
The reply is sent once, as a keyed `POST …/question/{id}` (`Ctrl+Enter` submits from any
page). Tab and Shift+Tab switch between parallel asks, each of which keeps its own
answers. Esc keeps following the manifest chain, so while the turn is busy it interrupts,
which cancels the question. Settings opens in a centered
modal using old Nexus's panel geometry and a searchable root/drill list. Press
`/` to filter settings; Enter opens a section, and Esc returns to the root
before closing the panel. Existing Core-backed controls retain their own focus
and keyboard handling.

Keys come from the manifest (`F1` lists them; the palette shows each command's
key). Beyond the manifest: `F6`/`Shift+F6` cycle focus, `Ctrl+J` inserts a
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
  turn; consecutive completions group, and expanding shows the full context in
  order, including any output clipping notice.
- Clipboard images: **Ctrl+V** in the composer in a local macOS or Linux
  terminal (macOS reads PNG/TIFF through AppKit; Linux uses `wl-paste` or
  `xclip`; SSH clipboard reads are refused). Pasting inserts `[Image A]`,
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
  location strip keeps the current path and branch, a centred highlighted count
  of live kernels and running background shells (click it for their details),
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
  (sessions), Ctrl+L (right sidebar), Ctrl+T (cycle effort), Ctrl+R (reconnect);
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
The dropdown's **Voice: on/off** toggle defaults on: opening Manager or switching Voice on starts Live immediately.
Switching Voice off stops only audio. The input has a solid inset surface; debug navigation does not start audio. Enter sends text; with Voice off, the first typed request starts a control
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
