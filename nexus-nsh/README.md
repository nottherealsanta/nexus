# nexus-nsh

The shared NSH client for Nexus Core: browser and desktop today, iOS after the
desktop interaction settles. It speaks only the Core HTTP API through the host
proxy (`scripts/serve.mjs` adds the bearer token; the app itself never sees a
credential) and reads the same shared interface data as the TUI:
`../interface/manifest.toml` for commands, keys, `when` rules and settings order,
and `../interface/theme.toml` for colours, roles and glyphs. Both are converted
into typed modules by `scripts/gen.py`; no id, label, colour or key is copied by
hand.

The app itself is TSX (`App.tsx`, `src/`); `package.json` is tooling only.

**Status (PLAN.md Step 12.5, in progress):** the shell, session list and filters,
workspace session tabs, new-session view, timeline, waiting card, composer,
settings navigation, alerts, notifications and connection states are usable
against a real Core. Tabs show running, unread, failed and idle state; they
switch, rename and close without deleting sessions. The timeline has starting
context, Markdown (prose 15/24 in Monaspace Neon; fenced code is a panel card with the language and
a Copy button and padded code; inline code is 13 px Monaspace on the panel), and three detail levels: finished activity groups collapse in low and medium,
while high expands their tools. Running groups show the latest five activities in medium (with earlier items announced), and waiting groups stay open. Each user
message is the turn's header: a full-width raised card (`#n · time` on the right,
with long messages previewed in six lines and expandable as sent,
Copy and Fold on hover) that folds to one line. A sticky copy of the current
user card sits under the header with `↑ N lines of this turn above`, pushed up by
the next card and clicked to scroll back. Turn footers are a right-aligned meta
row (`models · tools · input tokens · output tokens · elapsed · finished`) with no
status word: a failed turn shows its first error line above it, and an
interrupted one adds `/continue`. Durations read `0.4s`, `4.2s`, `1m 05s` and
`1h 02m`. Hovering over token counts shows reported counts, reasoning tokens and
cache share. The finish time is omitted when unavailable or while the turn is
running. Activity groups are 28 px rows with `+N −M` and failures when folded;
members are 24 px rows on a tree line with verb icons (the status glyph replaces
the icon while running, failed or waiting), and a member's output sits in a
320 px box with Show all. Thoughts show `Thought · preview`, with the body behind
a 2 px rule; a thought with no tool beside it is a single `Thought · preview` row, never a group. High-detail output keeps the first 30 and last 20 lines;
clipping is announced and the show-all control restores the whole inspection
output.

The app uses Monaspace Neon for its text, including chrome, prose and tool output;
inline code, fenced code and diff content use Monaspace Argon. NSH imports the
bundled regular, medium and bold faces so the packaged app registers them before
rendering. Child cards label background calls, distinguish failed and
running status, and allow the full tool result to be expanded without navigating
away from the parent session.

Selecting a session loads, after the bounded snapshot view, every message, the
frozen starting context and the turn summaries with the session's spend ledger
(`GET /api/session/{id}/turn`: the newest 200, then `before=` pages to the oldest,
at most 25 pages; more are announced). It never reads the event log. Footers
(models, duration, tokens and cache share), tool timings, retry and error rows,
the composer's context meter and the Working row's output count read the
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

The waiting card (D14) is docked above the composer in its 760 px column: a 3 px accent
rail, `◆ Allow bash?` (`Nexus asks` for a question), `waiting on you · 0:42` counted from
when the client first saw the request, the parameter table (at most six rows), for edit,
write and patch the unified diff capped at 12 rows with `N more rows`, and a key-chip
choice list: allow once, allow for session (`scope: "session"`), deny, and deny with a note
(the note is the composer's text; empty, it says so). Questions show radios or checkboxes and a free-text field;
`⌘↵` submits. Alerts (D15): toasts are 360 px, top-right under the header (and the banner),
oldest first, three at most and then `+N more`, with no lifetime hairline. A toast keeps its
full text on the record; clicking it, or pressing Enter on it, or clicking an entry of the
notifications drawer opens it in the modal dialog (`c` copies, Esc closes). The banner is a
36 px strip, and the jump-to-latest pill (`↓ 3 new · ⌘↓`) sits above the composer, right-aligned
with its card. Settings › Notifications › Notification sounds (on by default) plays a rising
Web Audio cue when a root session and all its subagents have finished, and a higher cue when any session needs
input; `NEXUS_COMPLETION_SOUNDS=off` silences both.

The chrome follows D02. The 44 px header band and both sidebars sit on `bg-sidebar`
(the chrome surface) with no edge rules; the centre is `bg-background` (the canvas). The
header is three slots that match the sidebars (the resizable left sidebar, the centre, 300 px when the right
sidebar is docked). Controls have square corners and cards use a subtle 2 px radius;
round indicators and meters retain their shape. Drag the right edge of the docked left sidebar to resize it between 180 and 480 px (initially 264 px). The new-session starting-context card uses the same full-width reading column, insets and card as the timeline's starting context, so the layout does not move when the session starts; the timeline shows its starting-context sections directly without a summary header.
Over the docked left sidebar its left slot holds the 32 px toggle and
the Sessions, Files and Projects icons (they follow the sidebar's open state; narrow, they
are in the drawer with labels). The session tabs are 28 px and square, aligned with
the end of the left sidebar: the viewed tab is on the canvas surface with primary text, the
others are muted with a hover fill. Their × shows on hover (and on the viewed tab). Tabs are
root sessions only: opening a subagent keeps its root's tab in view. Under the header a
subagent strip names the parent, offers the back button, repeats the root breadcrumb and lists
the root's children as focusable buttons with running, waiting, failed or completed state. The run status sits at the
centre's right edge; the bell and right toggle are at the far right. The context timeline
uses the full available centre width with responsive horizontal padding (24 px narrow,
48 px from 820 px, 64 px from 1200 px and 80 px from 1600 px). Every card shares the
column's left edge, and assistant prose plus activity and thought rows start at the
cards' 16 px text inset (activity rows keep a 4 px hover pad inside their 12 px indent).
Glyphs and status
tones come from `interface/theme.toml` (`glyphs.status`, `glyphs.attention`). The
`border.strong` token is emitted as `border-strong` (`--border-strong`) for the decorative
split rules and tool boxes.

The composer is a card on the panel surface (2 px radius, 1 px border that turns
to the focus colour) with compact selectors for agent (in its colour), model with
its muted provider, and effort. The agent label aligns with the message input;
the control row ends with dictation and send buttons.
While a session runs, a Working row above the card shows the elapsed time and
output tokens (`Working · 12s · ↓4.1k`) with Stop. Typing `/` opens a completion
block on the card's top edge (commands, with their key hints); `@` completes
project paths from Core's file listing. Tab accepts, Enter runs a slash command,
Esc closes. The location strip below the card shows the muted home-shortened path and
branch; a linked worktree adds `⑂`. On the right, the context meter shows an
80x4 px bar in the agent colour and `82k · 41%` when known (on narrow screens,
the count is shown without the bar). Draft context is marked as an estimate.
The strip has no project prefix, checkout label or commands
hint. Before a session exists, the agent/model/effort pickers and slash commands
set draft creation fields. Timeline detail is a saved NSH preference. The
starting-context section lists the prompt, tools, instructions, skills and MCP
servers with estimates and the catalog window share. Its text uses a consistent
13 px size, compact section counts, gray status dots and a three-column list of
all tools. Disabled entries remain marked and counted. The system prompt row omits the agent name, and the instruction file
has no inline preview (its detail remains available by clicking its title). Skills and servers have
per-session switches; entries not available at session start are dimmed and say
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
and Projects has a trailing `+ Add project` row. The collapsed sidebar shows projects
in creation order, with stable uppercase initials (collisions use the next available
consonant, then a numeric suffix). Projects with running sessions or session activity
within the last hour are lighter gray; inactive projects are darker. Selection has
a separate background and edge marker. Hover/focus shows the full name and activity;
clicking restores the project's last selected session in this client, or its newest
unarchived root session, or the new-session view when empty.
Projects also shows the selected
project's `.mcp.json` servers. Enabled, valid entries start automatically with a
new session, and the panel warns that their commands run then without asking;
reading or refreshing it never starts a server. Each row shows its current
status and the non-secret facts Core reports: transport, command name, argument
count, working directory, environment or HTTP header names, and a
credential-free URL. Disabled and invalid entries say so, with the reason.
Settings has Providers and
Agents pages (a drill-in with a back link; Esc returns): provider connections
with device codes, key entry and catalog refresh, and the default agent and effort with a line per subagent that opens its model
and fallback lists. Session rows show the project in Global scope and the checkout branch (or linked worktree name) in Project scope, instead of the agent; an unknown branch is left blank. Worktree data is loaded for branch/location display (and read again when the
session view's `directory` differs from the one it was read for, as after a move), while directory
and worktree actions remain disabled with a reason. Kernel state is the remaining
session-info item not reported by Core.

Still missing (shown disabled with their reason, never as a control that appears
to work): the complete files/editor and Git workflows, worktree controls,
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

For web development, `nexus dev web` starts the hot-reloading host and API proxy
and opens the app. Ctrl-C stops both web servers, leaving Core running. Without
the launcher, run `nsh dev App.tsx --target web --port 8790` and, in a
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

Visual/interaction harness (a real `nexus daemon start` and a scripted Core,
then Playwright at 390x844, 1024x768 and 1440x900 in dark and light):

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
| `src/core/transport.ts` | Same-origin HTTP (`Idempotency-Key`, `If-Match`), errors, keyed transport retries, SSE reader. |
| `src/core/api.ts` | Wire types (including the `/turn` page: `TurnSummary`, `RequestSummary`, `AttemptSummary`, `ToolSummary`, `UsageLedger`) and `/api/info` compatibility (accepts `"1.0"` or `{major, minor}`; the client is written against 1.2). |
| `src/core/reduce.ts` | Pure client model: snapshot, events, dedup by durable id, history merge (messages and summaries), running-turn scratch and `turn.summary`, cached views of recent sessions. |
| `src/core/turns.ts` | Pure turn views: requests and usage per turn (summaries plus the running scratch), the newest known usage, ledger totals, tool timings, the floor position. |
| `src/core/sync.ts` | Connection supervisor: handshake, snapshot, SSE resume, backoff, restart detection, history and `/turn` loading, ledger refresh. |
| `src/store.ts` | Reactive view state: connection, model revision, drafts, overlays, alerts, prefs. |
| `src/commands.ts` | The command registry from the manifest: availability terms, handlers, keys, Esc chain. |
| `src/ui/timeline.tsx` | Timeline blocks: context, user cards (turn headers), text, code fences, thoughts, groups and members, children, diagnostics (live, or from summaries), the floor row, footers, and the sticky user bar. |
| `src/ui/diff.tsx` | Diffs of edit, write and patch details: split at window width 1100 px and up, unified below, built from the tool detail lines (so the detail cap still applies). The empty side of an add or remove is a flat fill: NSH views cannot draw the hatch. |
| `src/ui/*` | Header, sidebars, right sidebar, composer (with completion, Working row and project strip) and waiting card, settings, overlays, alerts. |
| `src/gen/*` | Generated from `interface/`; never edit. |

## Platform notes

- **Web.** Served same-origin by `scripts/serve.mjs`; the host proxies `/api`
  with the token from `daemon.json` and can start Core. Callers cannot read
  `daemon.json` or spawn a daemon from the browser, so the host owns both.
  It accepts only origin-form request targets, forwards path and query to the
  daemon's own origin, and answers malformed input (an `Origin: null`, a bad
  percent escape) with 4xx instead of failing.
- **Desktop/iOS.** Use the same app and navigation; the remaining platform work
  is window chrome, native menus, clipboard/notifications/file pickers and
  remote-Core connection. `API_BASE` in `transport.ts` is the single place a
  desktop build points at an absolute origin.

## Images and context notes

Paste images into the composer using the platform paste shortcut. The draft
shows thumbnails, filenames and individual Remove image buttons. Image-only
messages are supported; previews remain in the timeline after sending, and
failed sends keep the draft. PNG, JPEG, GIF and WebP are supported, with up to
eight images and 8 MiB combined. Bytes are stored by Core, so reload and
continuation keep the same image context.

A background shell completion (`→ Background Shell Done: …`) is a plain context
notice, without a user-message card or a new turn number.

The top header omits the busy indicator; activity stays with the conversation.

The system prompt opens in its detail view without an inline preview.

Settings reads and edits shared defaults, timeline detail, reduced-motion choice and disabled tools through Core's
`/api/config`, using the current file revision for each write. Configuration
errors are shown in Settings, and `config.changed` refreshes the view after an
external edit. The session context meter uses Core's budget; `~` marks an
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
- **Plan limits (K11).** `/usage` (palette: Show plan limits) shows `GET /api/usage` in the
  notification detail dialog.
- **Tool images (K8).** A tool row whose result has `media` opens with its images
  (`/api/artifact/{id}`, same origin) under the output.
