# nexus-nsh

The shared NSH client for Nexus Core: web, macOS and iOS. It speaks only the Core
HTTP API. The web build goes through the host proxy (`scripts/serve.mjs` adds the
bearer token; the app itself never sees a credential). A native build talks to
Core directly: on macOS it finds a local Core in `~/.nexus/daemon.json` and reads
its token, so there is no token or pairing code to enter; iOS connects to a Core
on the network by address. Both remember an address once chosen. It
reads the same shared interface data as the TUI:
`../interface/manifest.toml` for commands, keys, `when` rules and settings order,
and `../interface/theme.toml` for colours, roles and glyphs. Both are converted
into typed modules by `scripts/gen.py`; no id, label, colour or key is copied by
hand.

The app itself is TSX (`App.tsx`, `src/`); `package.json` is tooling only.

The timeline lets the NSH virtual list preserve the visible row when measurements
or splices change content above it; the app does not apply a second height
correction. Following the latest message and restoring a session's position remain
app-controlled. Deferred follow commands are scoped to the session that queued them.

**Open inspector** stays inside NSH, using the same authenticated transport as the
session (Inspector API v2), and does the same as the Rust TUI with buttons, inputs and the
same keys. Six views: Timing (one shared-axis Gantt, lanes grouped by session, a fixed ruler,
zoom/pan/Fit, first-byte markers only where recorded, "end unknown" spans), Conversation,
Steps, Requests, Raw and Questions. Rows show Core's `summary`; pressing one expands its exact
`detail` JSON, and "Load full record" fetches `/inspect/record` when it was clipped. Older/Newer
use cursors; the Sessions chips (or `[`/`]`) inspect another session of the tree; the search box,
Kind/Status buttons and event-type box are filters sent to Core. A state line shows Live, Paused
or Disconnected (offline data says "not current"); the modal coalesces session stream changes and checks `/inspect/head`
while open, refetching only on change, and Pause live/Refresh are always there. Selection,
expansion, scroll, zoom and filters are kept per session and view. The cache panel labels the
session lifetime and the displayed page separately, with coverage, the hit-rate denominator,
per-model rows and unknown writes; `omitted` and notes are listed. Keys are the `inspector.*`
commands in `interface/manifest.toml` (the same bare keys as the TUI). The Sessions tree is a wrapping one-line list like the TUI's, and `j`/`k` keep the selected row in view.
See [the inspector guide](../docs/inspector.md).

The Rust TUI is the visual reference for the shared NSH app. NSH uses Monaspace
Argon for interface text and prose and a full-width reading column with a fixed 24 px
outer gutter and 8 px inner gutters. Desktop and web controls use bundled,
theme-tinted SVGs, including sidebar tabs and the Settings row alongside their
labels. Open and closed sidebar indicators use distinct icons; timeline status
and animated running marks retain their shared TUI meanings. Rounded surfaces,
hover, focus and pressed states are screen-specific presentation, not different
placement or behavior. Manager consent uses a translucent solid surface without
backdrop blur so that GPUI can render it too. Icon-only buttons retain accessible
names and tooltips.
The session search icon, field and shortcut are laid out side by side, without
absolute overlays. Phone touch targets and mobile navigation remain unchanged.
Narrow desktop/web headers show one flexible title instead of a fixed-width tab
strip and add button, matching the Rust TUI's narrow header. Session switching and
creation remain available through the sidebar and shortcuts.

## Workspace terminals

Desktop and web expose Core-owned terminals in a bottom dock toggled by **<n> shell** in the activity strip; the separate **kernel** indicator has no count. Tabs show each terminal's name and status; **+** creates one and **×** closes it. The xterm.js screen is embedded in a WebView and is not part of the session or model context. Closing the dock detaches; closing a tab terminates its terminal, with confirmation if running. Opening a terminal attempts to take control; a read-only terminal offers **Take control**. A discarded-history notice means replay is lossy. Output cannot write the clipboard: select text and use **Copy**.

Keyboard routing assumes the focused WebView receives keys after NSH's keymap has been overridden with pass-through bindings. The web renderer uses a sandboxed iframe; the macOS host has a native key monitor that consumes matching shortcuts before dispatch, so simply ignoring shortcut callbacks is not sufficient. Ctrl+] moves focus to the tabs. Browser and OS reserved shortcuts remain platform-owned. Native focus and input routing require a real macOS verification.

## Native hosts

The native crate and CLI use NSH 0.4.0. The experimental `nexus gpui` host loads
`capabilities.json` alongside `bundle.js`, including the filesystem grants needed
for local Core discovery. The AppKit `nexus macos` launcher remains separate.
Neither native launcher starts Core automatically.

The NSH terminal target is retired. Use `nexus tui` for the maintained Rust terminal
client. Its former launcher, rendering branches and captures are preserved in the
Git tag `archive/nsh-terminal-backend`.

A new session's first message appears immediately below its context preview as
`You · Sending…`. The chosen settings stay visible until Core's message and history
arrive; there is no intermediate loading/default-header flash. This is local
submission state, not model context. Failures restore the draft in the new-session
view or, if creation succeeded, the created session. Prompt submission starts
without waiting for the session-history read.

Final dictation adds the editable note `[Voice transcript — may contain errors.]`
at the top of the composer, once per message (including mixed typed/dictated
drafts). The same note stays in the sent context and model input. Empty or
cancelled dictation does not mark a draft; OS keyboard dictation cannot be
identified automatically. See [Voice](../docs/voice.md#transcript-provenance).

On iOS and native phones, Sessions, Manager and Settings are bottom tabs. Sessions offers the
project picker, project/all-project scope, search and archive filter; selecting a
session opens the conversation full screen, with a back control to the list. The
conversation shows status, user messages and agent replies, with compact cards for
approvals and questions. Desktop workspace surfaces are unavailable on phones.
The Manager tab supports typed interaction; native voice capture remains unverified.
Subtle dividers separate the touch-sized session rows, which show the branch or
project and relative activity time without covering the title. The search field is
dismissed before opening a session. Desktop keeps the sidebar-based navigation.

**Status (PLAN.md Step 12.5, in progress):** the shell, session list and filters,
workspace session tabs, new-session view, timeline, waiting card, composer,
settings navigation, alerts, notifications and connection states are usable
against a real Core. Tabs show running, unread, failed and idle state; they
switch, rename and close without deleting sessions. Each project has its own open
tabs and last viewed session or new-session screen, held locally while the client runs.
Startup shows the launch splash (`src/ui/splash.tsx`): the `_/` logo in white, a 30-segment
milestone bar (`daemon`, `workspace`, `providers`, `session`, `ready`), its percentage and the
next step. It fades into the first live frame and is dismissed when a start fails.
The timeline has starting
context, Markdown (prose uses the 16/24 default in Geist; fenced code is a panel card with the language and
a Copy button and padded code; inline code is Monaspace at 12.5/20 on the panel), and three detail levels: finished activity groups collapse in low and medium,
while high expands their tools. Running groups show the latest five activities in medium (with earlier items announced), and waiting groups stay open. Each user
message is the turn's header: a full-width raised card (`#n · time` on the right,
with long messages previewed in six lines and expandable as sent,
Copy and Fold on hover) that folds to one line. Image previews and inline markers
remain visible without repeating generated attachment labels below the text; previews sit side by side in bordered squares and wrap as needed. A sticky copy of the current
user card sits under the header with `↑ N lines of this turn above`, pushed up by
the next card and clicked to scroll back; it pins only bands whose height has been
measured, so a newly added message cannot make it jump or blink. A subdued
`interactive.scrollbar` bar appears at the timeline's right edge while the reader
scrolls, then fades out after about a second; Settings → Appearance → Scroll bar turns
it off. Its scroll offset and
measured row heights are keyed by session, so a root's band cannot place the sticky
header over a child. Turn footers are a right-aligned meta row (`models · tools · 24.5Ki · 570o · 90r · 58%c · elapsed · finished`) with no
status word: a failed turn shows its first error line above it, and an
interrupted one adds `/continue`. A turn with no usage says `usage unknown`. A running turn has no footer; it appears only
once the turn is done, and the live thought stays inside the active activity
group. Settled history is rebuilt only on structural changes; text and reasoning
deltas update a coalesced live tail, and wire-only events do not redraw, so a long
session does not re-walk every message and tool detail per token. A session's first message is pinned to the top once it settles (as in the TUI): the list's bottom padding is a spacer (`pinSpacer`, view height less what the message and reply take) and the offset is the message row's top, re-applied as rows measure; the pin ends when the reader moves the view, and only the first send pins. A session opens at its latest message the first time and at its saved offset afterwards; it follows new content until the reader scrolls up (any upward scroll leaves follow at once, so streamed content never pulls the reader back), which shows the jump pill, and jumping or scrolling back to the end follows again. Height changes above the viewport keep the first visible row anchored. Durations read `0.4s`, `4.2s`, `1m 05s` and
`1h 02m`. The finish time is omitted when unavailable or while the turn is
running. Activity groups are 28 px rows with the TUI's content: counts in first-seen
order (edits count the files they touch), `+N −M` beside the summary from
edits that were neither failed nor denied, and `· N denied`; a failed member keeps its `✕`, first error line and a muted `failed`,
and the folded heading never counts failures. A live group reads `Working` with a shimmer, and a group that has just finished
flashes its chevron the accent, unless animations are off. Members hang on a tree line with verb
icons (the status glyph replaces the icon while running, failed, denied or waiting) and read
`Shell  make  exit 2 · 1.2s`: the argument comes from the same field per tool as in the TUI, a failed
call adds its first error line, and read, edit, write, code and ask carry their summary. An open
member's detail sits behind the tree line with `raw` on its row: `$ command` rows, then the last 8 output rows
behind `↑ N earlier lines · enter show all` (a press, Enter or `o` restores them), while a diff or the raw view keeps a head and
a tail, 100 lines each or 30 and 20 at high detail. Thoughts show `Thought · preview`, with the body behind a rail, and no time;
a thought still arriving stays open and keeps its last 10 lines; a thought with no tool beside it is a single row, never a group.
Clipping is announced and the control restores the whole output. In the context card, the system prompt and instruction file open as rendered Markdown; Tools, Skills and MCP open a list of entries with their descriptions, and opening an entry shows a tool's schema, a skill's full text or a server's facts. The group's chevron starts on the text edge shared by the user message and the reply. A child session is one row, `Scout <prompt>` with `Background`, then its model and time, and a failure or a wait below; pressing it opens the child. A notice is one row, `context · … · show details`, whose text opens in a dialog.

The app sets a 16 px default with 24 px leading; the shared kit's title, body and
code roles use that size, and hierarchy comes from weight and colour rather than
size (some timeline chrome still sets a smaller size explicitly). Monaspace Argon
carries interface text, prose, inline code, fenced code, paths and tool arguments.
NSH imports the bundled faces so the packaged
app registers them before rendering. Child cards label background calls,
distinguish failed and running status, and allow the full tool result to be
expanded without navigating away from the parent session.

Selecting a session loads, after the bounded snapshot view, every message, the
frozen starting context and the turn summaries with the session's spend ledger
(`GET /api/session/{id}/turn`: the newest 200, then `before=` pages to the oldest,
at most 25 pages; more are announced). It never reads the event log. Footers
(models, duration, tokens and cache share), tool timings, retry and error rows,
the dock's activity bar and the Working row's output count read the
summaries; a user turn's footer covers every Core turn that starts in its message
range, so continuations and notice turns join the turn they follow. A row marks
`floor.seq`: turns above it have no details (pruned, or older than turn records),
and a failed `/turn` read is announced while the messages still load. Unknown
usage reads `tokens unknown` (or `+ unknown` beside a partial total) and is never
counted as zero. Live events of the selected session since the last
`turn.summary` are kept as the running turn's scratch (deltas fold into the
streaming text and are never kept; a `wire_request` keeps only its model). The
scratch fills in the running turn, joining a summary loaded mid-turn by step, and
`turn.summary` replaces that turn's summary and drops the scratch. Diagnostics
that exist only as live events (directory moves, child notices, cancelled waits)
show while the session stays open; their messages carry them after a reload.

Session views are cached. The views of the last 8 sessions left behind stay in
`Model.cached` and keep taking their sessions' events, so switching back shows the
timeline at once while its history reloads and merges in (held messages and older
pages stay; a held turn still running gives way to a fetched closed one). A resync
keeps cached views of sessions that still exist and drops their scratch. Context
reads (`GET …/context`) run one at a time per session; requests made meanwhile
coalesce into one more read.

The waiting card (D14) is docked above the composer in the timeline's reading column: a 2 px accent
edge whose border stops before the card's corners curve (purple for a multi-select question), `◆ Allow bash?` (`Nexus asks` for a question), `waiting on you · 0:42` counted from
when the client first saw the request, the parameter table (at most six rows), for edit,
write and patch the unified diff capped at 12 rows with `N more rows`, and a key-chip
choice list: allow once, allow for session (`scope: "session"`), deny, and deny with a note
(the note is the composer's text; empty, it says so). Questions show radios or checkboxes and a free-text field;
`⌘↵` submits. Alerts (D15): toasts are 360 px, top-right under the header (and the banner),
oldest first, three at most and then `+N more`, with no lifetime hairline. A toast keeps its
full text on the record; clicking it, or pressing Enter on it, or clicking an entry of the
notifications drawer opens it in the modal dialog (`c` copies, Esc closes). The banner is a
one-line strip on the reading column (it stacks when narrow), where only the glyph carries the level's colour, and the jump-to-latest pill (`↓ 3 new · ⌘↓`) sits above the composer, right-aligned
with its card. Settings › Notifications › Notification sounds (on by default) plays a rising
Web Audio cue when a root session and all its subagents have finished, and a higher cue when any session needs
input; `NEXUS_COMPLETION_SOUNDS=off` silences both.

The chrome follows D02. The 44 px header band and both sidebars sit on `bg-sidebar`
(the chrome surface); a `border.default` hairline runs under the band and between each
sidebar (or its rail) and the page. The centre is `bg-background` (the canvas). The
header is three slots that match the sidebars (the resizable left sidebar, the centre, 300 px when the right
sidebar is open). Below the band the 44 px right rail (Session, and Git with its changed-file
count) opens the panel on demand and pushes the page; the panel starts closed. Controls, cards,
the current-project selection, session rows, search fields, prose tables and modals are all
square, like the TUI's cell grid; only circular status dots, spinners, image remove controls
and meters keep their shape. A box rule runs a hairline along a box's straight
sides; a filled box needs no border. Drag the right edge of the docked left sidebar to resize it between 180 and 480 px (initially 264 px). Drag the file panel's left edge to resize it from 360 px up to what leaves the timeline 480 px. The reading column fills the available centre width. The new-session starting-context card uses the same full-width reading column, insets and card as the timeline's starting context, so the layout does not move when the session starts; the timeline shows its starting-context sections directly without a summary header.
Over the docked left sidebar its left slot holds the 32 px toggle and
the Sessions and Files icons (they follow the sidebar's open state; narrow, they
are in the drawer with labels). The session tabs and the subagent strip use one
fixed tab box (208 × 28 px, 176 × 40 narrow), so a root, a child, a parent and
New session all keep the same width and a long title ellipsises inside its tab.
New session's label starts on the same text edge as the session titles.
The viewed tab has the square selection fill with primary text, the others are muted
with a hover fill. Their × shows on hover (and on the viewed tab). Tabs are root
sessions only: opening a subagent keeps its root's tab in view. Under the header
a subagent strip shows the path above the viewed child as parent tabs (the last
is the back control), a divider rail, then the root's other sessions as equal
tabs with their state glyph and agent (no state word), with the viewed child selected. As in the TUI, every strip tab has one width of 10 to 30 terminal columns (`src/strip-layout.ts`, mapped to points by the text cell size); when the row is too full, the oldest ancestors drop from the left with a `…` marker, then the children farthest from the viewed one, and last the cells narrow. The strip never scrolls. The run status sits at the
centre's right edge; the bell and right toggle are at the far right. The context timeline
uses the full available centre width with fixed 8 px horizontal padding at every
viewport. Every card shares the column's left edge, and assistant prose plus
activity and thought rows start at the cards' 8 px text inset.
The waiting card sits in the timeline's reading column, and the docked composer's content
uses the same 16 px inset, so a draft's first character lines up with the first character of
the user message above it.
Glyphs and status
tones come from `interface/theme.toml` (`glyphs.status`, `glyphs.attention`). The
`border.strong` token is emitted as `border-strong` (`--border-strong`) for the decorative
split rules and tool boxes.

The composer is a dock at the foot of the centre column, edge to edge on the chrome
surface, with its input, controls and location strip in the timeline's reading column.
The agent (in its colour at regular weight), model with its muted provider, and effort are compact selectors;
the input starts on the user band's 16 px text edge, and the mic and send buttons sit
beside it on the input's bottom edge, with the dock padded tighter. The selector row
ends with context usage. The location strip is its own row under the controls at every width: the
muted home-shortened path and branch on the left, a centred highlighted count of live
kernels and running background shells (it opens their details), a linked worktree
adding `⑂`, and the context reading (`82k · 41%`) on the right. The activity line along the dock's foot sits on the text edges. While a turn runs it is a
soft pulse in the agent's colour, a fifth of the width, gliding back and forth. At rest it stays
hidden until context reaches 80% (warning) or 95% (error). Draft context is marked as an estimate.
While a session runs, a Working row at the top of the dock shimmers the word and shows the elapsed
time and output tokens (`Working · 12s · ↓4.1k`) with Stop. Typing `/` opens a completion
block above the dock (commands, with their key hints); `@` completes project paths from
Core's file listing. Tab accepts, Enter runs a slash command, Esc closes. Ctrl+X arms the
manifest's leader aliases for three seconds: then M, N, O/B, G, S, L, T, R, V,
Shift+N or ? runs model, new session, sessions, agent, settings, sidebar, effort,
reconnect, dictation, notifications or shortcuts. Before a session exists, the agent/model/effort pickers and slash commands
set draft creation fields. Timeline detail is a saved NSH preference. The
agent, model and effort pickers carry the TUI's search field (fuzzy filtering),
↑↓ navigation and Enter to choose. The model picker names each model, puts its
provider and `current` at the row's end, and shows the highlighted model's ref,
context and efforts beneath the list.
The starting-context section lists the prompt, tools, instructions, skills and MCP
servers with estimates and the catalog window share. Its text uses a consistent
16 px size, compact section counts, gray status dots and a three-column list of
all tools. Disabled entries remain marked and counted. The system prompt row omits the agent name, and the instruction file
has no inline preview (its detail remains available by clicking its title). Skills and servers have
per-session switches. Skills remain inline at 12 or fewer; above 12 only the clickable
`Skills [N]` heading remains. At any count the heading opens a scrollable two-column
card picker with description excerpts and separate enable/disable switches. A card name
or an inline skill name opens a second Markdown modal. Cards are borderless dark gray,
with a lighter focused card. The detail modal shows the full description,
complete `SKILL.md`, and other file/folder names. Tab reaches names and switches independently;
Enter activates the focused control. Esc returns to the picker with its scroll and focus
preserved, then closes it. Entries not available at session start are dimmed and say
“new session”. Before a session exists, NSH loads the project skills and enabled
MCP servers into the draft and sends its `disabled_tools` with session creation.
`/learn` previews a source, installs an unambiguous requested skill directly,
and otherwise offers a keyboard-operable checklist with overwrite counts. MCP
Settings edits the global full-definitions mode and listed-tools limit through
Core config. Turn footers
include catalog-priced cost when prices are available. The right sidebar takes
turns and tool usage from the messages, calls, token totals and catalog-priced
cost from Core's spend ledger (per model, plus live usage it has not counted
yet; it is re-read after each `turn.summary`), and the context bar from the
newest request with known usage. A ledger that began counting well after the
first message is labelled `Totals since …`, and requests that reported no usage
are counted as unknown. It also counts descendant sessions. Right-click or Shift+F10 opens the
manifest-driven session/project row menu, including confirmed project removal,
and Projects, opened by its command, has a trailing `+ Add project` row. A 48 px project rail (NSH only; the TUI keeps its five cells) stays beside the sidebar whether it is open or
closed, except on phones and below 500 px, which keep the project picker in the Sessions tab. A border separates it from the sidebar, and the sidebar's project-session and Files tabs keep their icons. It shows projects in Core's saved order (drag a tile to reorder, or Alt+Up / Alt+Down and the row menu's Move project up/down; Esc or dropping outside the rail cancels; a press without travel still selects), without inactive projects, with stable uppercase initials in creation order (moving a project never changes a label)
(collisions use the next available consonant, then a numeric suffix; the first character stays centred). Projects with running sessions or session activity
within the last hour are lighter gray; passive projects are dimmed further and idle ones are darker. The row menu sets a project's status (current, passive, inactive; the names are the user's, 2026-11-06). An inactive project leaves the rail and is restored by pressing it in the Projects tab. Remove project asks first, only unregisters the project (its sessions are kept and return if the folder is added again; files are never touched) and is refused while a session runs. The selected project, and the selected session row in the sidebar, share one active
treatment: a lighter block (`interactive.active`) with a left bar (`interactive.active_bar`)
drawn over the row, so nothing shifts. The selected session's title is `text.highlight`
(white in dark mode) and never bold. Each project has a counter in each corner: attention (violet) and unread
(info) on top, running (success) and idle (primary) below, with any count past
nine shown as `*`. A project with
no counts shows a muted rule below its initials instead. Hover or
focus spells out all four counts with the project name;
clicking restores the project's last selected session in this client, or its newest
unarchived root session, or the new-session view when empty; pressing the project already open toggles its Sessions sidebar. The switcher (`⌘K`) leads each session row with its state glyph (running spinner, attention/error/unread dot or idle dot), then the title, with the age or message match at the right. A `+` under the projects adds one, and Settings sits at the rail's foot while the sidebar is closed. The sidebar lists the current project's sessions only and ends in `+ New session`. Its archive icon above the search shortcut includes archived sessions alongside active sessions when enabled.
You can type and send in an archived session: **Resume archived session** warns
that a large uncached context may be slower and cost more, and that code kernels
restart fresh. Cancel keeps the draft; Resume unarchives the whole tree before
sending. Explicit Unarchive shows the same warning without sending a message.
Projects also shows the selected
project's `.mcp.json` and `.agents/mcp.json` servers. Enabled, valid entries start
automatically with a new session, and the panel warns that their commands run then
without asking; reading or refreshing it never starts a server. Each row shows its
current status and the non-secret facts Core reports: transport, command name,
argument count, working directory, environment or HTTP header names, and a
credential-free URL. Disabled and invalid entries say so, with the reason. A name
defined in both files shows the duplicate warning, naming the winning `.mcp.json`
definition.
Modals share three width classes with the TUI: small 500 px, medium 600 px and large
740 px, clamped to the window gutters. They are centred in both directions and capped
at 70% of window height; scrollable bodies reserve space for their title, controls and
footer, including in short windows. Titles have no duplicate Esc chip; dismissal hints
live in `ModalFooter`. See `interface/design-language.md` for the shared rules.
Settings is the same square modal as the TUI's, with a compact section rail on the left
and the selected section on the right. The same search field filters both. Providers and Agents are
rendered in the right pane: each provider row opens a detail modal with one
button per sign-in method (browser or device), a clickable link, a copyable
code, key entry, connect/disconnect and catalog refresh; the default agent and effort
with a line per subagent that opens its model and fallback lists (Esc returns
from that detail); each model row has an effort stepper (`‹ effort low ›`, or Right after pressing it) writing
`agents.efforts`. Tools keeps one permission choice, labelled with the tools it
governs, and one clickable tool grid. Desktop session rows place the status mark before the title with a gap. Session rows show the project in Global scope and the checkout branch (or linked worktree name) in Project scope, instead of the agent; an unknown branch is left blank. Worktree data is loaded for branch/location display (and read again when the
session view's `directory` differs from the one it was read for, as after a move), while directory
and worktree actions remain disabled with a reason. Kernel state is the remaining
session-info item not reported by Core.

**Keyboard flow from an empty composer (PLAN 15.4).** Only while the composer is focused and empty: Left opens the
session sidebar and gives it the keys (the composer is blurred; Up/Down move a session cursor, Enter opens the session); Left again focuses the project list (Up/Down move a
cursor bar among the projects, Enter opens one, restoring an inactive project first); Left again opens the **projects
overview** (also the palette command "Projects overview"). Esc or Right steps back out in reverse order, whenever the Projects tab holds the keys (also when opened by its command). With text in the
composer Left moves the cursor as usual. The overview (`src/ui/projects-overview.tsx`, rows from `overviewRows`) lists
the projects in their saved order with a `├`/`└` tree of each project's active root sessions and their state (`running`, `needs input`, `failed`, `finished`, `idle`), a per-project
`N archived` toggle (`a` or Enter; collapsed), passive projects dimmed, and inactive projects last under `Inactive`,
restored with `r` (`1`/`2`/`3` set current, passive, inactive). The project picker (`project.switch`, `cmd+shift+k`) lists
every project, inactive ones marked, and choosing an inactive one restores it, as in the TUI. Tests: `keyboard-flow.test.tsx`.

The Files tab is the TUI's expanding tree: one Core listing per directory read, folders expand and collapse in place
with the shared fold glyphs and two cells of indent per depth, `.git` is never listed, and a file's Git letter sits at
the row's end. Expanded folders keep their state across tab switches and reset when the project changes; the pane's
header names the project and the checkout branch. Files open in a centred modal over the dimmed page, with long lines wrapped, using `Editor` and `DiffView` from `nsh/base`;
below 1100 px it fills the window. Markdown files (`.md`, `.markdown`) open as rendered prose, and the header's
Source control shows the numbered source, returning to the preview on a second press. `cmd+f`
opens a find bar (plain text, a case toggle, previous/next and the match count) after returning to the source;
Esc closes the bar before the panel. Links in assistant prose, Markdown previews,
new-session prose and context details open in the browser.

Still missing (shown disabled with their reason, never as a control that appears
to work): the file actions and live watcher, the Git workflow, worktree controls,
steering/queueing, handoff and one-turn sessions, inspector wire controls,
additional settings beyond the wired defaults and MCP context options,
sign-in over the API and dictation. Those need Core increments (PLAN.md Steps
11a/12a) or the remaining 12.4 client work.

## Build, run, test

If the repo launcher is installed (`docs/development.md`, “Shortcuts”), run
`nexus web` from anywhere to build, serve and open the app. Otherwise:

```sh
npm ci                 # playwright, for the browser e2e harness
npm run gen            # interface/*.toml -> gen/theme.json, src/gen/*.ts
npm run check          # gen + nsh type check
npm run build          # packaged web app in dist/Nexus

npm run serve          # the web app on 127.0.0.1:4820, proxying /api to Core (127.0.0.1:4747)
NEXUS10_HOME=/tmp/nx npm run serve -- --port 4821   # another home and port
```

The web build, tests and `nexus dev web` use `scripts/nsh-web.mjs` to locate
the WebAssembly-capable `llvm-ar` needed by zstd. It respects explicit archiver
settings, then uses `llvm-ar` on PATH or in the active Rust toolchain. If none is
installed, run `rustup component add llvm-tools`, or set
`AR_wasm32_unknown_unknown` to its absolute path. Apple's system `ar` is unsuitable.

For web development, `nexus dev web` starts the hot-reloading host and API proxy
and opens the app. Ctrl-C stops both web servers, leaving Core running. Without
the launcher, run `node scripts/nsh-web.mjs dev App.tsx --target web --port 8790` and, in a
second terminal, `npm run serve -- --upstream http://127.0.0.1:8790`. Open the
proxy at `http://127.0.0.1:4820` so API requests still get daemon authentication.
The NSH development host reloads source changes; the packaged `npm run serve`
alone serves a static build.

Tests are headless interaction tests against a fake network (`App.test.tsx`).
They open the real app in a browser target (the headless engine cannot resolve
the app's relative `/api/...` URLs):

```sh
nsh test --target web                                # uses an installed Chrome/Chromium
nsh test --target web --browser "/path/to/Chromium"  # explicit browser
npm run test:host                                    # host proxy hardening (node --test)
```

`nsh test scroll-anchor-native.test.tsx --target macos` (or `--target gpui` / `web`)
checks the platform List's visible-marker anchoring for row growth and prepends.
The default headless engine does not match these hosts for row growth: after an
initial offset of 150 it reports no offset correction and an 80-pixel marker shift.
These fixture tests do not exercise Core history loading or the packaged app.

The full-app browser coordinate regression uses a fake Core and a separate web
development host (no daemon):

```sh
NEXUS10_HOME=/tmp/nexus-scroll-home node scripts/nsh-web.mjs dev App.tsx --target web --port 18791
node e2e/timeline-anchor.mjs --url http://127.0.0.1:18791
node e2e/timeline-anchor.mjs --url http://127.0.0.1:18791 --more
node e2e/timeline-anchor.mjs --url http://127.0.0.1:18791 --move
```

It measures a message before and after the older page arrives, including when
the reader moves while it is pending. Add `--browser /path/to/Chrome` if needed.

Visual/interaction harness (a real `nexus daemon start` and a scripted Core,
then Playwright at 390x844, 1024x768 and 1440x900 in dark and light). The scripted
seed covers all scenarios: a multi-step turn with thoughts, tools, a patch, a failed
read, a child session and an image; a live `think` turn; approval and question waits;
a failed tool; and five projects for the rail's colliding initials and counters. The
same seeded home can open in the TUI with `NEXUS10_HOME=<home> --dump-frame ...`:

```sh
e2e/run.sh /tmp/nsh-e2e /tmp/nsh-shots                 # up, shots, down
e2e/up.sh /tmp/nsh-e2e                                 # leave the hosts running
node e2e/shots.mjs --out DIR --scripted http://127.0.0.1:4820 \
  --real http://127.0.0.1:4821 --ids /tmp/nsh-e2e/ids.json --only 1440x900-dark
e2e/down.sh /tmp/nsh-e2e
```

## Layout

| Path | Role |
| --- | --- |
| `App.tsx` | Composition and startup (prefs, sync, keyboard bindings). |
| `src/core/transport.ts` | HTTP (`Idempotency-Key`, `If-Match`), errors, keyed transport retries, SSE reader. On the web `apiBase()` is `document.baseURI`'s origin (the app realm has no real `location`), so every request and the host/start fetches are absolute; natively it is set from the Core address. |
| `src/core/api.ts` | Wire types (including the `/turn` page: `TurnSummary`, `RequestSummary`, `AttemptSummary`, `ToolSummary`, `UsageLedger`) and `/api/info` compatibility (accepts `"1.0"` or `{major, minor}`; the client is written against 1.2). |
| `src/core/reduce.ts` | Pure client model: snapshot, events, dedup by durable id, history merge (messages and summaries), running-turn scratch and `turn.summary`, cached views of recent sessions. |
| `src/core/turns.ts` | Pure turn views: requests and usage per turn (summaries plus the running scratch), the newest known usage, ledger totals, tool timings, the floor position. |
| `src/core/sync.ts` | Connection supervisor: handshake, snapshot, SSE resume, backoff, restart detection, history and `/turn` loading, ledger refresh, and each connection's `server.connected` stream id. A stream that ends without an error reopens quietly (the view stays live); the selected history is reloaded only for a genuinely new stream, a restart or a resync. |
| `src/core/view.ts` | Client view reporting: the current project, session and panel ids (`files` or `file`; NSH has no Git diff panel), keyed by the event stream that issued them, re-sent after every reconnect and retried on a capped backoff. |
| `src/store.ts` | Reactive view state: connection, model revision, drafts, overlays, alerts, prefs, the Files tree's directory listings and expanded set, and the view sent to Core. |
| `src/file-tree.ts` | Pure flattening of the Files tree (depth-first through expanded folders, `.git` skipped), shared with tests. |
| `src/strip-layout.ts` | Pure subagent strip layout, the TUI's: one 10..30 column cell, thinned and shrunk to fit. |
| `src/scroll-follow.ts` | Pure timeline follow decision (and the first-send pin's spacer and release): an upward scroll leaves follow, returning near the end resumes it. |
| `src/commands.ts` | The command registry from the manifest: availability terms, handlers, direct keys, the three-second Ctrl+X leader and Esc chain. |
| `src/ui/timeline.tsx` | Timeline blocks: context, user cards (turn headers), text, code fences, thoughts, groups and members, children, diagnostics (live, or from summaries), the floor row, footers, and the sticky user bar. |
| `src/ui/diff.tsx` | Diffs of edit, write and patch details: split at window width 1100 px and up, unified below, built from the tool detail lines (so the detail cap still applies). The empty side of an add or remove is a flat fill: NSH views cannot draw the hatch. |
| `src/ui/connect.tsx` | The native Connect screen: the Core address, saved in NSH Storage (and reopened from the header's Core button). |
| `src/ui/splash.tsx` | The launch splash: logo, five milestone stages, 30-segment bar and the fade into the first live frame. |
| `src/ui/*` | Header, sidebars, the right panel and its icon rail, the docked composer (completion, Working row, location strip, activity bar) and waiting card, settings, overlays, alerts. |
| `src/gen/*` | Generated from `interface/`; never edit. |

## System tray

On desktop (macOS, Windows, Linux) NSH shows a tray icon through `nsh/desktop`'s `Tray` (`installTray` in `App.tsx`,
content in `src/tray-logic.ts`). Its menu lists each non-inactive project as a submenu of its root sessions with their
status; choosing one focuses the window and opens it. The icon gains a dot while any session waits on an approval or
question, and the tooltip says so. Web, mobile and the TUI ignore it (a client-specific difference, see the porting ledger).
No global shortcut is registered.

## Platform notes

- **Web.** Served same-origin by `scripts/serve.mjs`; the host proxies `/api`
  with the token from `daemon.json` and can start Core. Callers cannot read
  `daemon.json` or spawn a daemon from the browser, so the host owns both.
  It accepts only origin-form request targets, forwards path and query to the
  daemon's own origin, and answers malformed input (an `Origin: null`, a bad
  percent escape) with 4xx instead of failing.
- **macOS and iOS.** The same app, run by NSH's AppKit and UIKit hosts:
  `npm run macos` (or `npm run macos:open` for hot reload) and `npm run ios`
  (iOS simulator), `npm run ios:catalyst` (an iPhone-sized Mac window) or
  `npm run ios:device` (a device build; add `--sign "Apple Development: …" --profile App.mobileprovision` for a signed `.ipa`).
  `nsh package App.tsx --target macos` writes a `.app`; `--target ios-simulator`
  a simulator `.app`, and `--target ios` an `.ipa`.
  App Transport Security blocks plain HTTP to a network address by default, so
  `scripts/native-info.mjs` adds `NSAllowsArbitraryLoads` (which covers LAN,
  `.local` and Tailscale IP addresses) and, on iOS,
  `NSLocalNetworkUsageDescription`, then ad-hoc signs the app again; the
  scripts above run it for you. A device `.ipa` is signed before it can be
  patched: package without `--sign`, run the script on the `.app`, then sign with
  your identity and profile. `NSAllowsArbitraryLoads` permits plain HTTP to any
  address, which is right for a self-installed client but needs justification for
  App Store review; prefer an `https://` Core address (for example through
  `tailscale serve`) for distributed builds. A simulator runtime is installed with
  `xcodebuild -downloadPlatform iOS`; the Catalyst host (`npm run ios:catalyst`)
  shows the iPhone layout without one.
- **Connecting natively.** A native build has no host process. On macOS it reads
  `~/.nexus/daemon.json` (the same 0600 file the TUI uses) and connects to the
  local Core with that token automatically; `~/.nexus10` is read until the home is
  migrated. There is no Connect screen or Core button: without a local Core the
  app starts offline and reconnects when `127.0.0.1:4747` (or the address in
  `daemon.json`) appears. iOS opens the Connect screen and enters the Mac's LAN
  address; start Core there with
  `NEXUS_HOST=0.0.0.0 nexus serve` (or `nexus daemon start` with `NEXUS_HOST` set),
  because a non-loopback bind serves `/api/*` without a token. Only do this on a
  trusted LAN or Tailscale/private network. `apiBase()`/`setApiBase()` in
  `src/core/transport.ts` are the single place the origin changes; `localCore()`
  there reads the daemon file, and `apiUrl()` prefixes URLs the platform loads
  itself (artifact images).
- **Native services.** `native/` registers `Microphone` and `Mermaid` in
  `nexus/native`. `Mermaid.render(source, width)` compiles through the shared
  `nexus-mermaid` crate on every target. `Mermaid.renderText` returns the shared
  pie/Gantt/timeline rows, and `Mermaid.renderPng` rasterizes with the bundled
  font on every target, including web, because NSH's own SVG backend deliberately
  draws no text. The timeline's `MermaidFence` draws the text rows or the PNG,
  falls back to the source, and copies the exact source from
  `◇ mermaid · copy source`; `src/mermaid.ts` caches by `(source, width, appearance)`
  and looks the native module up with `nativeModule("nexus/native")` when it is
  first needed, so importing the timeline for a pure helper does not load
  `nexus/native` outside the app bundle. `src/dictation.ts` does the same for
  `Microphone` and treats a missing module as a client without capture. The Apple native bridge records 16 kHz mono WAV for dictation and Manager pipeline voice on macOS and iOS; packaged apps need `NSMicrophoneUsageDescription` (set by `scripts/native-info.mjs`). Device permission, recording and playback on macOS remain unverified. `src/mermaid-logic.ts` holds the
  closed-fence scan and tone classes.
- **Type size.** Shared interface text defaults to 14 px on web and macOS and 16 px on iOS. Explicitly sized text may differ.
- **Safe areas and touch.** The header pads by `safeArea().top`, the composer dock and
  the sidebar footer by `safeArea().bottom`, so the status bar and home indicator
  never cover chrome. Phone and tablet idioms (`platform.idiom`) keep hover-only
  actions visible (the user card's Copy/Fold, tab close, session row menu) and
  use finger-sized targets; session rows are 56 px and icon buttons 44 px below
  820 px. `nsh.json` grants every network host, since the address is the user's,
  and reads `~/.nexus` / `~/.nexus10` for the local daemon file.

## Install on an iPhone

A physical iPhone needs a signed development build; a simulator does not (use
`npm run ios`). The order matters: `nsh package` signs the app, and the
local-network keys must be in `Info.plist` *before* that, so package without
`--sign`, patch, then sign.

1. **Make Core reachable.** On the Mac:

   ```sh
   nexus daemon stop
   NEXUS_HOST=0.0.0.0 nexus daemon start
   curl http://<mac-ip>:4747/api/info      # JSON, no token
   ```

   `<mac-ip>` is the LAN address (`ipconfig getifaddr en0`) or the Tailscale
   address (`100.x.y.z`). Check the same URL from the iPhone's Safari first;
   if it does not load, the problem is the network, not the app.

2. **Find the signing assets.** `security find-identity -v -p codesigning` lists
   the identity. The development provisioning profile must include the iPhone's
   UDID and the app's bundle id. Xcode keeps its profiles here; the loop prints
   each profile's App ID:

   ```sh
   for p in ~/Library/Developer/Xcode/UserData/Provisioning\ Profiles/*.mobileprovision; do
     security cms -D -i "$p" > /tmp/profile.plist
     printf '%s | %s | %s\n' "$(plutil -extract Name raw /tmp/profile.plist)" \
       "$(plutil -extract Entitlements.application-identifier raw /tmp/profile.plist)" "$p"
   done
   ```

   Pick the one whose App ID is `TEAMID.dev.nexus10.nsh` (or a wildcard). If
   none matches, create one, or build with `--id` set to the App ID an existing
   profile covers.

3. **Package and patch.** From `nexus-nsh/`:

   ```sh
   npm run gen
   nsh package App.tsx --target ios --out .nsh/out-ios [--id com.example.app]
   node scripts/native-info.mjs .nsh/out-ios/Nexus.app
   ```

   `--id` overrides `nsh.json`'s `dev.nexus10.nsh` when the profile covers a
   different App ID. The app is unsigned here and must not be installed yet.

4. **Sign it.** Substitute the profile, identity and team from step 2:

   ```sh
   PROFILE="$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles/<UUID>.mobileprovision"
   security cms -D -i "$PROFILE" > /tmp/profile.plist
   plutil -extract Entitlements xml1 -o /tmp/entitlements.plist /tmp/profile.plist
   cp "$PROFILE" .nsh/out-ios/Nexus.app/embedded.mobileprovision
   codesign --force --deep --sign "Apple Development: Name (TEAMID)" \
     --entitlements /tmp/entitlements.plist --timestamp=none .nsh/out-ios/Nexus.app
   ```

   `codesign -dv --verbose=2 .nsh/out-ios/Nexus.app` should show that identity,
   not `adhoc`.

5. **Install and launch.**

   ```sh
   xcrun devicectl list devices
   xcrun devicectl device install app --device <UDID> .nsh/out-ios/Nexus.app
   xcrun devicectl device process launch --device <UDID> dev.nexus10.nsh
   ```

   Xcode's **Window → Devices and Simulators → +** installs the same `.app`.

6. **Trust it once on the phone.** **Settings → Privacy & Security → Developer
   Mode → on** (restart), and **Settings → General → VPN & Device Management →
   your Apple Development identity → Trust**. The app must be unlocked to launch
   from `devicectl`.

7. **Connect.** Enter `http://<mac-ip>:4747` (not `localhost`; that is the phone)
   on the Connect screen and tap **Connect**. Allow **Local Network** when iOS
   asks. The address is stored; the header's **Core** button reopens the screen.

   If it says Core is not reachable: check the Safari test from step 1, the
   Local Network switch for Nexus, and that Core is still bound to `0.0.0.0`
   (a daemon restart without `NEXUS_HOST` goes back to loopback + token). A
   development profile expires; re-sign and reinstall when it does. The Connect
   screen now probes the address before saving it and prints the failure, so
   tap **Core**, re-enter the address and read the message. An App Transport
   Security message means the build was patched after signing (redo steps 3–4
   in order); a plain timeout means the phone cannot route to the Mac.

## Images and context notes

Paste images into the composer using the platform paste shortcut. The draft
shows a square thumbnail for each image above its input, with the muted filename
above the thumbnail and an individual Remove image button. Image-only messages
are supported; after sending, the same square thumbnail sits above the user
message text, and failed sends keep the draft. Clicking a composer thumbnail opens
the drawing modal: drag to draw, `1`-`4` or the swatches pick a colour, `u` undoes,
`x` clears, `enter` or Done merges the marks into the image through `nexus-annotate`,
and `esc` cancels. Only the marked-up image is sent. Clicking a thumbnail in a sent
message opens a read-only full-size preview (`esc` closes it); the sticky user header
never shows an image. PNG, JPEG, GIF and WebP are supported, with up to eight
images and 8 MiB combined. Bytes are stored by Core, so reload and continuation
keep the same image context.

A large text paste (at least 500 characters, regardless of line count) collapses into one
`[Pasted ~N tokens]` marker at the caret; shorter multiline pastes stay inline. The
large-paste composer preview has a **Show** control that opens the existing full-text modal.
Sending sends the marker and the full text separately: Core
stores the text as a collapsed part, so the model reads it while the user
message shows a clickable chip above its text, with no inline preview in the
message. Clicking the chip opens the full pasted text in a modal. A failed send keeps the collapsed
draft.

A background shell completion (`→ Background Shell Done: …`) is a plain context
notice, without a user-message card or a new turn number.

The top header omits the busy indicator; activity stays with the conversation.

The system prompt opens in its detail view without an inline preview.

Settings reads and edits shared defaults, timeline detail, reduced-motion choice and disabled tools through Core's
`/api/config`, using the current file revision for each write. Configuration
errors are shown in Settings, and `config.changed` refreshes the view after an
external edit. Typing in the Settings search filters the section rail and the
rows; clearing it restores the selected section. The session context meter uses
Core's budget; `~` marks an
estimate.

## K5-K11 client parts (2026-10-29)

- **Search (K5).** The sessions list asks `GET /api/search` after 150 ms (two characters or
  more) and lists the sessions whose user messages match, under their title matches, with the
  matching snippet on the row's agent line. The search field retains its query when switching
  sidebar tabs, matching the active filter.
- **Older messages (K6).** A session opens with its newest 200 messages (`message?limit=200`);
  when more exist the timeline starts with `Older messages are not loaded` and a Load older
  messages button (`turn.load_older`, also in the palette) that reads the page before the oldest
  loaded message. A running session loads every older page by itself (so the row does not show
  there); pages already read stay cached across a resync. A failed page toasts and is not retried
  automatically; the button retries.
- **Approvals (K7).** See the waiting card above.
- **Cache warning (K9).** Choosing a model or agent for an existing session asks
  `…/switch-cost` first; when `cache_lost` it confirms with the reasons and the cached token count.
  A failed check never blocks the switch.
- **Editor (K10).** A project file opens read-only; Edit (`cmd+e`) makes it an editor over the
  text with the `revision` it was read at. Save (`cmd+s`) is `PUT …/file`; a stale write
  (`stale_file`) keeps the text and marks the file `changed on disk · reload`; Reload re-reads
  (asking first when dirty); `files.changed` reloads a clean editor and warns a dirty one. Esc
  asks before dropping changes. Not built: find (`editor.find`), syntax highlighting.
- **Provider usage (K11).** `/usage` (palette: Show provider usage) shows every connected provider from
  `GET /api/usage`, in a table of used gauge, percent left, reserve (for periods of at least seven days)
  and expiry. Window detail, Codex limit-reached and xAI per-product credit notes are hidden, as are xAI prepaid credits
  and Copilot chat, completions and AI-credit details. Core stores the latest snapshot and
  refreshes it every 10 minutes by default; Settings → Context changes the interval, and `r` refreshes it on demand.
  Scroll the table for further providers; Esc closes the modal without waiting.
- **Tool images (K8).** A tool row whose result has `media` stays folded and notes the image count; opening it shows the images
  (`/api/artifact/{id}`, same origin) under the output.

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

For modal geometry checks against a disposable visual host, run
`node e2e/modal-layout.mjs --url http://127.0.0.1:4820 --out /tmp/modal-shots`
(optionally `--browser /path/to/chrome`). It checks centred placement, width classes,
the height cap, visible footers and Esc at normal and short window sizes.

Settings sections have no gaps between rows. Selected sections and setting rows use a
neutral gray highlight across the entire row, including its value. A white left bar marks
the focused pane (dark in the light theme). Choice and switch rows are the same segmented
controls as the TUI; each option is a press target. Left returns focus to the section
heading without changing the setting. (The TUI, which is keyboard-first, binds ←/→ to the
focused segment instead; its Tab returns to the rail.)

Live microphone packets continue during silent output, with browser echo cancellation enabled. The shared silent-output regression scenario covers both clients.

Running responses use request-local streaming checkpoints: after subscribing,
the client reads `/turn`, seeds the running partial, and merges newer deltas by
request id and sequence. A completed part replaces its folded text; a saved
assistant message clears scratch. Token deltas have no durable cursor. Finished
session compaction leaves messages, starting context and turn summaries intact.


The Files command palette offers New file, New folder, Rename file or folder,
and Delete file or folder. Paths are relative to the displayed workspace root.
Delete is permanent and asks for confirmation; a folder's descendant count is
shown when Core can count it within its safety budget.

Desktop Files and editor views renew a 60-second workspace watcher lease while
visible and release it on close. Disk changes refresh the file tree; clean text
follows its disk revision, while unsaved edits remain intact with a changed-on-disk
notice. Native watcher failure shows **Polling every 3 seconds**. These global
resource events never enter a session's model context.
