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
context, turn headers and footers, folding, Markdown, and three detail levels:
activity groups collapsed in low, individual collapsed tools in medium, and
expanded tools in high. High-detail output keeps the first 30 and last 20 lines;
clipping is announced and the show-all control restores the whole inspection
output. Selecting a session loads its full stored history (messages, retained
diagnostics, snapshot) after the bounded snapshot view.

The composer follows the TUI's borderless panel, agent-colour rail and darker
status band. Its controls show the model/provider and effort; the status band
shows latest input-token occupancy and the palette hint. The location strip shows
the home-shortened directory, branch and linked-worktree marker without a project
name. Before a session exists, the agent/model/effort pickers and slash commands
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
| `src/ui/timeline.tsx` | Timeline blocks: context, turns, text, reasoning, tools/diffs, children, diagnostics, footers. |
| `src/ui/*` | Header, sidebars, right sidebar, composer and waiting card, settings, overlays, alerts. |
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
