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
context, Markdown (prose 15/24; fenced code is a panel card with the language and
a Copy button; inline code is 13 px Monaspace on the panel), and three detail levels: activity groups collapsed in low,
individual collapsed tools in medium, and expanded tools in high. Each user
message is the turn's header: a full-width raised card (`#n · time` on the right,
Copy and Fold on hover) that folds to one line. A sticky copy of the current
user card sits under the header with `↑ N lines of this turn above`, pushed up by
the next card and clicked to scroll back. Turn footers are a right-aligned meta
row (`▪ Agent · models · effort · tools · duration · tokens · cost`) with no
status word: a failed turn shows its first error line above it, and an
interrupted one adds `/continue`. Durations read `0.4s`, `4.2s`, `1m 05s` and
`1h 02m`. Activity groups are 28 px rows with `+N −M` and failures when folded;
members are 24 px rows on a tree line with verb icons (the status glyph replaces
the icon while running, failed or waiting), and a member's output sits in a
320 px box with Show all. Thoughts show `Thought · preview`, with the body behind
a 2 px rule; a thought with no tool beside it is a single `Thought · preview` row, never a group. High-detail output keeps the first 30 and last 20 lines;
clipping is announced and the show-all control restores the whole inspection
output. Selecting a session loads its full stored history (messages, retained
diagnostics, snapshot) after the bounded snapshot view.

The waiting card (D14) is docked above the composer in its 760 px column: a 3 px accent
rail, `◆ Allow bash?` (`Nexus asks` for a question), `waiting on you · 0:42` counted from
when the client first saw the request, the parameter table (at most six rows), for edit,
write and patch the unified diff capped at 12 rows with `N more rows`, and a key-chip
choice list. Choices Core does not offer (allow for session, deny with a note) are muted
with their reason beside them. Questions show radios or checkboxes and a free-text field;
`⌘↵` submits. Alerts (D15): toasts are 360 px, top-right under the header (and the banner),
oldest first, three at most and then `+N more`, with no lifetime hairline. A toast keeps its
full text on the record; clicking it, or pressing Enter on it, or clicking an entry of the
notifications drawer opens it in the modal dialog (`c` copies, Esc closes). The banner is a
36 px strip, and the jump-to-latest pill (`↓ 3 new · ⌘↓`) sits above the composer, right-aligned
with its card.

The chrome follows D02. The 44 px header band and both sidebars sit on `bg-sidebar`
(the chrome surface) with no edge rules; the centre is `bg-background` (the canvas). The
header is three slots that match the sidebars (264 px, the centre, 300 px when the right
sidebar is docked). Over the docked left sidebar its left slot holds the 32 px toggle and
the Sessions, Files and Projects icons (they follow the sidebar's open state; narrow, they
are in the drawer with labels). The session tabs are 28 px with a 6 px radius, aligned with
the 760 px reading column: the viewed tab is on the canvas surface with primary text, the
others are muted with a hover fill. Their × shows on hover (and on the viewed tab). The run status sits at the
column's right edge; the bell and right toggle are at the far right. Glyphs and status
tones come from `interface/theme.toml` (`glyphs.status`, `glyphs.attention`). The
`border.strong` token is emitted as `border-strong` (`--border-strong`) for the decorative
split rules and tool boxes.

The composer is a card on the panel surface (12 px radius, 1 px border that turns
to the focus colour, 12x16 padding) with pill selectors for agent (in its colour),
model with its muted provider, and effort. Its control row ends with the context
meter (an 80x4 px bar in the agent colour and `82k · 41%`) and the send button.
While a session runs, a Working row above the card shows the elapsed time and
output tokens (`Working · 12s · ↓4.1k`) with Stop. Typing `/` opens a completion
block on the card's top edge (commands, with their key hints); `@` completes
project paths from Core's file listing. Tab accepts, Enter runs a slash command,
Esc closes. The project strip below the card shows the project name (bold), the
home-shortened path, `⌥ branch`, and `checkout` or `worktree` (bold violet), with
`ctrl+p commands` on the right when idle. Before a session exists, the agent/model/effort pickers and slash commands
set draft creation fields. Timeline detail is a saved NSH preference. The
starting-context section lists the prompt, tools, instructions, agent and
child/workspace tools with estimates and the catalog window share. Turn footers
include catalog-priced cost when prices are available. The right sidebar derives
session turns, model calls, tool usage, enabled tools, token totals,
latest-request context and catalog-priced cost from loaded messages and wire
events; it also counts descendant sessions. Right-click or Shift+F10 opens the
manifest-driven session/project row menu, including confirmed project removal,
and Projects has a trailing `+ Add project` row. Settings shows Core's default
model, catalog and loaded agents, with model and agent pickers and catalog
refresh. Worktree data is loaded for branch/location display, while directory
and worktree actions remain disabled with a reason. Kernel state is the remaining
session-info item not reported by Core.

Still missing (shown disabled with their reason, never as a control that appears
to work): the complete files/editor and Git workflows, worktree controls,
steering/queueing, handoff and one-turn sessions, inspector wire controls,
settings editing beyond theme, sign-in over the API and dictation. Those need
Core increments (PLAN.md Steps 11a/12a) or the remaining 12.4 client work.

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
| `src/core/api.ts` | Wire types and `/api/info` compatibility (accepts `"1.0"` or `{major, minor}`). |
| `src/core/reduce.ts` | Pure client model: snapshot, events, dedup by durable id, full-history merge. |
| `src/core/sync.ts` | Connection supervisor: handshake, snapshot, SSE resume, backoff, restart detection, history loading. |
| `src/store.ts` | Reactive view state: connection, model revision, drafts, overlays, alerts, prefs. |
| `src/commands.ts` | The command registry from the manifest: availability terms, handlers, keys, Esc chain. |
| `src/ui/timeline.tsx` | Timeline blocks: context, user cards (turn headers), text, code fences, thoughts, groups and members, children, diagnostics, footers, and the sticky user bar. |
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
