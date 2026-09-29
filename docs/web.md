# Web app (`nexus web`)

A framework-free browser client (plain HTML, CSS and ES modules; no build step)
for the same per-workspace daemon.

## Parity with the TUI

The web app is the Textual shell ([textual.md](textual.md)) in a browser. It has
**the same functionality, with everything in the same place**, and should feel
and behave the same. Only the finish may be more modern.

| Same as `nexus chat` | Where |
| --- | --- |
| Layout: top bar `▌` title … status `+` `▐`; sessions sidebar (34 cells, 272px); conversation; details sidebar (42 cells, 336px); composer with the activity bar under it | `index.html`, `app.css` "shell" |
| Color roles: `tokens.css` maps `ui/tui/theme.py` (`_DARK`/`_LIGHT`) role for role (`nx-accent` → `--accent`, `nx-blue` → `--info`, …); the values are the web's Signal palette ([design.md](../design.md)) | `tokens.css` |
| Conversation: the four-block context header first (`tui_context_header.py`), `▼` user blocks that fold a turn, `Thought:` lines, one-line muted ordinary tool call summaries (consecutive calls tightly stacked), two-line subagent calls (type and description; recent tool calls while running, count and elapsed time when finished), side-by-side diffs under completed Edit/Patch rows (one per file, unified below 760px, like `tui_diff.py`), tool details in a modal on click/keyboard activation, `AGENT · model · 1.2s` footers, red error lines | `renderContextHeader`, `renderMessage`, `renderTool` (`inlineDiffs`, `diffFiles`, `splitDiff`), `renderFooter` |
| Session rows: glyph (`●` current, braille spinner, `✓`, `·`, `◇`), title, status or age, `×` | `renderSessions` |
| Composer: editor, then `Agent  model provider  effort`, then context `3k (2%)` (`ui_support/context.py:context_usage` / `context_measure`) | `renderHeader`, `renderContextMeter`, `contextUsage` |
| Context and Tools dialogs: the groups from `ui_support/context.py` (`context_groups`, `tool_groups`, `context_summary`), collapsed rows with token estimates | `context-view.js`: `renderContextGroups`, `renderToolsReport` |
| Settings → Agents: "New sessions start with" (`AgentDefaultSet`, global or project scope) | `loadDefaultAgent`, `saveDefaultAgent` |
| Details: `SESSION` rows (Status, Agent, Model, Effort, Turns, Tool calls, Tokens, Context), `MODIFIED FILES`, `MCP SERVERS`; Tools rows open the tool detail modal | `renderOverview`, `renderDetails` |
| Chat commands: every `SPECS` entry in `ui/cli/commands.py`, same names, usage, summaries and hidden aliases | `SLASH_COMMANDS`, `parseSlash` |
| Keys: `ui/tui/app.py:SHORTCUTS` (Ctrl+P/N/O/F/G/B/L/S/I/T/E/C/R, Shift+Tab, `a`, Esc). ⌘K and ⌘N also work. | global `keydown` handler, `SHORTCUTS` |
| Agent color: the host's `color` first, else the TUI's name hash (`agent_color`) | `agentColor` |

What may differ is the finish only, specified in [design.md](../design.md)
("Signal"): Monaspace Argon, 20px lines (`--row`), square corners
(`--radius: 0`), flat surfaces, signal-colored tags and rule headings, and hard offset shadows (never blur) on primary buttons and
floating dialogs. The web also has a few browser-only affordances: the details panel tabs
(Tools, Agents, Trees, Logs), the Context/Logs/Export buttons, and the Settings
dialog. Keep these where they are.

When the TUI changes (`app.tcss`, `theme.py`, `ui/cli/commands.py`,
`ui/tui/app.py:SHORTCUTS`, `ui_support/timeline.py`, `tui_panels.py`,
`tui_context_header.py`), change the web client to match, and the other way
round.

### Font

The UI font is **Monaspace Argon** (Latin, 400; fontsource
`monaspace-argon@5.3.0`, SIL OFL 1.1). The CSP allows no external hosts, so the
files are vendored as `ui/web/assets/monaspace-argon-latin-400.woff2` and `.woff`
and declared with `@font-face` at the top of `tokens.css`. It is first in
`--font-mono`, and the whole UI uses `--font-mono`. To add weights or scripts,
download more fontsource files into `assets/`. Never link the CDN. A turn started in the terminal is live in
the browser and vice versa, and closing a view never stops work. Product intent
is in `webplan.md`; the visual spec is in `design.md`. The host side is in
[core.md](core.md).

## How it is served

`nexus web` (`cli.py:_web`) asks the running daemon over UDS for `WebLaunch`.
`Daemon.web_launch()` (`host/daemon.py`) starts a loopback HTTP listener
(`host/transports/http_sse.py`) and returns `http://127.0.0.1:<port>/#ticket=…`.
`host/web.py` owns every browser route:

| Route | Purpose |
| --- | --- |
| `/`, `/s/<session>` | `index.html` (deep links are client-routed) |
| `/styles/*`, `/js/*`, `/assets/*` | static files from `nexus/ui/web/`, read on each request (reload the browser to see edits; no daemon restart) |
| `POST /v1/web/ticket/redeem` | one-use, 60-second ticket → `HttpOnly; SameSite=Strict` cookie + CSRF token |
| `GET /v1/web/bootstrap` | CSRF token + workspace path |
| `POST /v1/web/command` | any host protocol command as JSON `{type:'SessionList', …}`, except `Shutdown` and `WebLaunch`. Requires an exact `Origin` and `X-CSRF-Token`. |
| `GET /v1/web/session-view?session=` | versioned snapshot (`schema_version: 1`, `seq`, `view`) from `HostFacade.web_snapshot` / `host_support/browser_view.py` |
| `GET /v1/web/session-events?session=&from_seq=` | SSE `view` frames carrying JSON-Pointer ops (`add`/`replace`/`remove`/`append`), or `resync: true` |
| `GET /v1/web/workspace-events` | SSE `workspace` frames with the session list (`HostFacade.subscribe_workspace`, polled every 0.5s) |
| `POST /v1/web/logout` | drop the cookie session |

The CSP is `default-src 'self'; script-src 'self'; style-src 'self'; …`. **No
inline `<script>`, no `style="…"` attributes, and no external hosts.** Setting
styles through the CSSOM from JS (`el.style.setProperty`) is fine. Packaged
files are listed in `pyproject.toml` (`"nexus.ui.web" = ["index.html", "styles/*.css", "js/*.js", "assets/*"]`).

## File map

| File | Contents |
| --- | --- |
| `ui/web/index.html` | The whole DOM: SVG icon sprite (`#i-*`), `.app-shell` grid (areas `top`/`side`/`main`/`insp`) with the full-width `.topbar` (`▌` `#sidebar-toggle`, title, Context/Logs/Export, `#live-state`, `+` `#topbar-new`, `▐` `#inspector-toggle`), `#sidebar`, `.main-pane` (`#conversation` holding the hidden legacy `#context-preview`, `#timeline` and `#empty-state`, then `.composer-wrap` holding `#approval-strip`, `#slash-menu`, `#composer-form` and `#activity-bar`), `#inspector`, and the overlays `#overlay` (palette/picker), `#settings-overlay`, `#context-overlay`, `#worktree-confirm-overlay`, `#toast-region`. |
| `ui/web/js/app.js` | All behavior. Dense one-function-per-line style, so search by function name. |
| `#setup-overlay` | First-run setup, mirroring `ui_support/tui_setup.py`: a second `createProviders` instance (`#setup-provider-list`) plus the env-key providers (`#setup-env`). `pollSetup` polls `SetupStatus`; the first connected provider marked `auto` goes to `completeSetup` → `SetupSave` without a model (the host picks the newest and reloads routes), then the dialog closes into chat. It transmits no credentials. |
| `ui/web/js/api.js` | `bootstrap()`, `command(cmd)`, `snapshot(session)`, `eventUrl(session, seq)`, `exportSession`. |
| `ui/web/js/projection.js` | `applyOperations(root, ops)`: validates and applies patches on a detached copy (atomic). |
| `ui/web/js/preferences.js` | localStorage detail level (session → workspace → browser precedence) and theme. |
| `ui/web/js/providers.js` | `createProviders({api, el, $, listId, isOpen})`: Settings → Providers cards (also rendered by first-run setup), mirroring `ui_support/tui_providers.py` (sign-in link and code, `ProviderLoginPoll` polling, password field for the OpenCode Go key). |
| `ui/web/js/context-view.js` | `renderCurrentContext(…)` for the inline context preview and the context dialog. |
| `ui/web/styles/tokens.css` | The Monaspace Argon `@font-face`; color tokens copied from `ui/tui/theme.py` (`_DARK`/`_LIGHT`); `--cell` (8px column) and `--row` (20px line); radii and the dialog shadow. `data-theme` = `dark`, `light` or `system`. Change a color in both files. |
| `ui/web/assets/` | Vendored font files (`monaspace-argon-latin-400.woff2`/`.woff`). |
| `ui/web/styles/app.css` | All layout and component CSS, sectioned by region, with responsive rules at the end. |
| `ui/web/styles/context-preview.css` | Context preview cards (line clamps are asserted by tests). |

### `app.js` map (search these names)

| Area | Functions / state |
| --- | --- |
| State | the `state` object at the top (session, view, seq, sessions, tab, detail, theme, context, worktrees, logs, openFiles, health); `nodes` = keyed DOM cache for the timeline |
| Boot and routing | `start`, `autoPanel` (right panel opens by default ≥1280px unless closed; localStorage `nexus-web-panel`), `openSession`, `connectSession` (snapshot → SSE → `applyOperations` → `queuePaint`), `startWorkspaceStream`, `popstate` handler |
| Left sidebar | `renderSessions` lists current and archived sessions as one-line `SessionRow`s (glyph from `data-glyph`: `●` current, braille spinner via the `spin` keyframes while working, `✓` done, `·` idle, `◇` archived; title; status or age; `×`). Every row has a visible Delete button (`SessionDelete` to restorable trash). `archiveSession` uses `SessionArchive`; both actions offer Undo. `#session-filter` searches both sections. `toggleSidebar`/`syncSidebarToggle` hide the docked sidebar (localStorage `nexus-web-sidebar`) or open it as an overlay below 960px. |
| Header and composer | `renderHeader` (title, plain-text status, agent/model/effort labels, agent color `--agent-color` on `#app`, `↵`/`■ stop`), `renderContextMeter` (`usageText`, same format as `ui_support/context.py:context_usage`) and the activity bar, `sendMessage`, `stopTurn`, drafts (`saveDraft`/`loadDraft` in sessionStorage) |
| Chat commands | `SLASH_COMMANDS` (mirrors `ui/cli/commands.py:SPECS`), `SLASH_ALIASES`, `parseSlash` (a typed `/cmd args` runs on Enter), `renderSlash`, `runSlash` (capture-phase keydown on the composer); `showText`/`closeText` is the text dialog for `/help`, `/hotkeys`, `/diff`, `/mcp` and `/skills` |
| Timeline | `renderTimeline` → `renderContextHeader` (first child; the four `System prompt`/`Tools`/`Skills`/`MCP` blocks from the last good `ContextInspect` in `state.header`, ported from `ui_support/tui_context_header.py`) → `entities` (messages, tools, boundaries, and a `footer` per finished turn) → `renderMessage` (`renderUserChrome` adds the `▼` fold toggle; thinking renders as the `Thought:` line), `renderTool` (one-line glyph + call arguments + first result summary; activation opens the full call/result modal), `renderBoundary`, `renderFooter` (`AGENT · model · 1.2s`); `markdown`/`updateMarkdown` (escaping mini-Markdown) |
| Approvals and questions | `renderApprovals` (approval and question cards as `.slash-item` lists; keys `y/a/n/d` or `1-3`), `approvalChoices` (mirrors `ui_support/prompts.py`), `resolvePermission`, `pendingQuestions`, `answerQuestion` (`QuestionAnswer` by `call_id`), `permissionTargetsUnavailable` (the background goes inert while an approval is pending) |
| Subagent page | `openAgent`/`closeAgent`/`renderAgentModal`: `#agent-overlay` is a second `app-shell` over the root at `/s/<session>/a/<agent>` (deep-linkable; Back, `←` and Esc return to the parent). Clicking a Task card opens it directly (`openToolDetails` skips the details dialog when a child exists). It has the root's top bar, a context header built by `headerBlocks` from the request the child actually sent plus a grey Task block after the System prompt (`agentTaskPrompt`: the recorded prompt, else the Task call's input) (`AgentTranscript` → `context`, fetched by `fetchAgentContext`), `agent.body` rendered with the root renderers (`renderMessage`, `renderTool`, `renderFooter`, `renderAgentCards`) by swapping `nodes` for `agentNodes` and `state.view` for the body, a read-only composer row and a details panel. Re-rendered from `renderTimeline` on every patch, so a running child is live. Context blocks open the root context dialog on `state.context.agentResult` (never refreshed). |
| Right panel | `renderDetails(force)` dispatches by `state.tab`; `renderOverview` (session summary, `modifiedFiles` + `diffRows`, `mcpSectionBody`); `overviewSignature` decides when to re-render; `loadWorkspaceHealth` (`Doctor`, 20s cache); tools/agents tabs inline; `renderWorktrees*`; `renderLogs`/`readLogs` (polling) |
| Pickers and palette | `showPalette`, `renderPalette`, `chooseFromList`, `chooseModel`/`commitModel` (Left/Right adjusts effort), `chooseAgent`, `cycleReasoningEffort` (Ctrl+T), `effortCommand`/`selectEffort` (`/effort`) |
| Settings | `openSettings`, `renderSettings`, `installSettingGroups`, `installSettingsNav`/`syncSettingsNav`, `renderSettingsWorkspace` |
| Context | `refreshContextPreview` (also feeds the header), `renderContextInline` (the legacy inline preview; kept hidden, as the terminal's preview is off by default), `openContextDialog(mode)` (Context button, `/context`, or a header block; the System prompt block opens `mode='system'` (Markdown), the Tools block `mode='tools'`), `renderContextReport` (grouped context or tools list, with Expand all) |

### Responsive contract (asserted in `tests/playwright_web_check.py`)

Like the terminal, panels dock while there is room and otherwise open over the
chat column. The sessions sidebar is **272px (34 cells)**: docked from **960px**,
an overlay opened by `▌` below that. The details panel is **336px (42 cells)**:
docked from **1280px**, an overlay below that. Below 700px the top bar keeps
only Logs. The page must never scroll horizontally. Reduced motion disables
transitions.

## Working on it

Dev loop against a real daemon with a scripted model (no API key):

1. Write a small script modeled on `tests/playwright_web_check.py:main()`: build a `ScriptedProvider(...)`, then `Runtime(path, config=Config(...), providers={"scripted": provider})`, then `Daemon(workspace, socket_path=Path("d.sock"), runtime_factory=...)`. Keep the socket path short, because macOS caps UDS paths at about 104 bytes. Print `await daemon.web_launch()`.
2. Pre-create sessions with `daemon.facade.open_session(id, create=True, recover=True)` + `await daemon.facade.start_turn(id, prompt)`. Add `.nexus/mcp.json` (`{"servers": {...}}`; `tests/fixtures/mcp/fs_server.py` is a working stdio server) to exercise the MCP panel.
3. Tickets are one-use. Mint more from the daemon, or over UDS with `UDSClient.connect(sock).call(p.WebLaunch())`.
4. Permission prompts only wait while a viewer is attached. With no viewer, `on_unattended` (default `deny`) applies.
5. Screenshot with Python Playwright (`.venv`) at 1440, 1024 and 400px, in dark and light.
6. To compare with the terminal, render the Textual shell against the same daemon: `open_client(workspace, socket_path=…, spawn=False)`, `NexusTextualApp(client, session=…).run_test(size=(200, 55))`, `export_screenshot()`. At 1600px wide, one terminal column equals one 8px web cell.

Rules of thumb:

- Keep parity with the Textual shell (see "Parity with the TUI" above). New functionality lands in both surfaces, in the same place.

- New data or actions go through a host command (see [core.md](core.md)). Never compute server state in JS from files.
- Keep IDs stable. The Playwright check selects on `#composer-input`, `#composer-model`, `#composer-agent`, `#reasoning-effort`, `#inspector`, `#inspector-toggle`, `#close-inspector`, `#tab-*`, `#logs-toggle`, `#settings-open`, `#settings-overlay`, `#settings-effective`, `input[name=theme|session-detail|workspace-detail|browser-detail]`, `#context-*`, `#timeline`, `.tool-card`, `.tool-status-text`, `.permission-card`, `.logs-source`, `.log-entry`, `.worktree-*`, `.sidebar`, `.palette-item`.
- Every render path must tolerate stale async results. Compare `session` and request IDs (`state.selectionRequest`, `state.context.requestId`, `state.logs.requestId`) before applying.
- Everything from the host is untrusted text. Build DOM with `el(tag, cls, text)` / `textContent`. `markdown()` escapes before formatting. Never `innerHTML` a server string.
- Overlays set `#app` `inert` and trap Tab. Escape, or a click on the backdrop outside the dialog, closes the top-most layer; dialogs have no Close button and focus the dialog itself on open. Follow the existing open/close helpers.

## Testing

- `.venv/bin/python tests/playwright_web_check.py`: the end-to-end browser check (handoff between terminal and browser, streaming, approvals, detail levels, settings, logs, worktrees, breakpoints, reduced motion, XSS probes). It writes screenshots to `artifacts/web-e2e/` and takes a few minutes. It seeds localStorage `nexus-web-panel=closed` so its panel toggles start closed.
- `tests/test_web_transport.py`: routes, auth, CSRF, CSP and snapshot/patch transport. `tests/test_browser_serve.py` covers the Textual browser bridge, not this app.
- Host projection: `tests/test_host_facade.py` (web snapshot and patches), `tests/test_host_logs_read.py`, `tests/test_client_context_inspect.py`.
