# Web app (`nexus web`)

A framework-free browser client (plain HTML, CSS and ES modules; no build step)
for the same per-workspace daemon. A turn started in the terminal is live in
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
| `ui/web/index.html` | The whole DOM: SVG icon sprite (`#i-*`), `.app-shell` grid with `#sidebar`, `.main-pane` (topbar, `#conversation` holding `#context-preview`, `#timeline` and `#empty-state`, then `.composer-wrap` holding `#approval-strip`, `#slash-menu`, `#composer-form` and `.composer-footnote`), `#inspector`, and the overlays `#overlay` (palette/picker), `#settings-overlay`, `#context-overlay`, `#worktree-confirm-overlay`, `#toast-region`. |
| `ui/web/js/app.js` | All behavior. Dense one-function-per-line style, so search by function name. |
| `ui/web/js/api.js` | `bootstrap()`, `command(cmd)`, `snapshot(session)`, `eventUrl(session, seq)`, `exportSession`. |
| `ui/web/js/projection.js` | `applyOperations(root, ops)`: validates and applies patches on a detached copy (atomic). |
| `ui/web/js/preferences.js` | localStorage detail level (session → workspace → browser precedence) and theme. |
| `ui/web/js/context-view.js` | `renderCurrentContext(…)` for the inline context preview and the context dialog. |
| `ui/web/styles/tokens.css` | Color and type tokens for dark (default) and light; `data-theme` = `dark`, `light` or `system`. |
| `ui/web/styles/app.css` | All layout and component CSS, sectioned by region, with responsive rules at the end. |
| `ui/web/styles/context-preview.css` | Context preview cards (line clamps are asserted by tests). |

### `app.js` map (search these names)

| Area | Functions / state |
| --- | --- |
| State | the `state` object at the top (session, view, seq, sessions, tab, detail, theme, context, worktrees, logs, openFiles, health); `nodes` = keyed DOM cache for the timeline |
| Boot and routing | `start`, `autoPanel` (right panel opens by default ≥1280px unless closed; localStorage `nexus-web-panel`), `openSession`, `connectSession` (snapshot → SSE → `applyOperations` → `queuePaint`), `startWorkspaceStream`, `popstate` handler |
| Left sidebar | `renderSessions`, `sessionStatus` (`working` / `input` / `done` / `idle`; "done" uses the seen map in localStorage `nexus-web-seen:<workspace>`), `archiveSession` (`SessionDelete`, then an Undo toast via `SessionRestore`), `#session-filter` |
| Header and composer | `renderHeader` (title, live-state pill, agent/model/effort chips, agent color `--agent-color` on `#composer-form`, path, Send/Stop), `renderContextMeter`, `sendMessage`, `stopTurn`, drafts (`saveDraft`/`loadDraft` in sessionStorage) |
| Slash menu | `SLASH_COMMANDS`, `renderSlash`, `runSlash` (capture-phase keydown on the composer) |
| Timeline | `renderTimeline` → `entities` → `grouping` (Focused mode groups finished tools) → `renderMessage`, `renderTool` (+ `toolTarget`, `toolDetails`), `renderGroup`, `renderBoundary`; `markdown`/`updateMarkdown` (escaping mini-Markdown) |
| Approvals | `renderApprovals`, `resolvePermission`, `permissionTargetsUnavailable` (the background goes inert while an approval is pending) |
| Right panel | `renderDetails(force)` dispatches by `state.tab`; `renderOverview` (session summary, `modifiedFiles` + `diffRows`, `mcpSectionBody`); `overviewSignature` decides when to re-render; `loadWorkspaceHealth` (`Doctor`, 20s cache); tools/agents tabs inline; `renderWorktrees*`; `renderLogs`/`readLogs` (polling) |
| Pickers and palette | `showPalette`, `renderPalette`, `chooseFromList`, `chooseModel`/`commitModel` (Left/Right adjusts effort), `chooseAgent`, `cycleReasoningEffort` (Ctrl+T) |
| Settings | `openSettings`, `renderSettings`, `installSettingGroups`, `installSettingsNav`/`syncSettingsNav`, `renderSettingsWorkspace` |
| Context | `refreshContextPreview`, `renderContextInline` (only shown on sessions with no turns), `openContextDialog` |

### Responsive contract (asserted in `tests/playwright_web_check.py`)

Sidebar width: **256px ≥1280**, **240px 960–1279**, **60px rail 700–959**
(icons and status glyphs only), and a **300px off-canvas drawer below 700**.
The right panel docks at 340px from 1280px up and is an overlay below that.
The page must never scroll horizontally. Reduced motion disables transitions.

## Working on it

Dev loop against a real daemon with a scripted model (no API key):

1. Write a small script modeled on `tests/playwright_web_check.py:main()`: build a `ScriptedProvider(...)`, then `Runtime(path, config=Config(...), providers={"scripted": provider})`, then `Daemon(workspace, socket_path=Path("d.sock"), runtime_factory=...)`. Keep the socket path short, because macOS caps UDS paths at about 104 bytes. Print `await daemon.web_launch()`.
2. Pre-create sessions with `daemon.facade.open_session(id, create=True, recover=True)` + `await daemon.facade.start_turn(id, prompt)`. Add `.nexus/mcp.json` (`{"servers": {...}}`; `tests/fixtures/mcp/fs_server.py` is a working stdio server) to exercise the MCP panel.
3. Tickets are one-use. Mint more from the daemon, or over UDS with `UDSClient.connect(sock).call(p.WebLaunch())`.
4. Permission prompts only wait while a viewer is attached. With no viewer, `on_unattended` (default `deny`) applies.
5. Screenshot with Python Playwright (`.venv`) at 1440, 1024 and 400px, in dark and light.

Rules of thumb:

- New data or actions go through a host command (see [core.md](core.md)). Never compute server state in JS from files.
- Keep IDs stable. The Playwright check selects on `#composer-input`, `#composer-model`, `#composer-agent`, `#reasoning-effort`, `#inspector`, `#inspector-toggle`, `#close-inspector`, `#tab-*`, `#logs-toggle`, `#settings-open`, `#settings-overlay`, `#settings-effective`, `input[name=theme|session-detail|workspace-detail|browser-detail]`, `#context-*`, `#timeline`, `.tool-card`, `.tool-status-text`, `.permission-card`, `.logs-source`, `.log-entry`, `.worktree-*`, `.sidebar`, `.palette-item`.
- Every render path must tolerate stale async results. Compare `session` and request IDs (`state.selectionRequest`, `state.context.requestId`, `state.logs.requestId`) before applying.
- Everything from the host is untrusted text. Build DOM with `el(tag, cls, text)` / `textContent`. `markdown()` escapes before formatting. Never `innerHTML` a server string.
- Overlays set `#app` `inert` and trap Tab. Escape closes the top-most layer. Follow the existing open/close helpers.

## Testing

- `.venv/bin/python tests/playwright_web_check.py`: the end-to-end browser check (handoff between terminal and browser, streaming, approvals, detail levels, settings, logs, worktrees, breakpoints, reduced motion, XSS probes). It writes screenshots to `artifacts/web-e2e/` and takes a few minutes. It seeds localStorage `nexus-web-panel=closed` so its panel toggles start closed.
- `tests/test_web_transport.py`: routes, auth, CSRF, CSP and snapshot/patch transport. `tests/test_browser_serve.py` covers the Textual browser bridge, not this app.
- Host projection: `tests/test_host_facade.py` (web snapshot and patches), `tests/test_host_logs_read.py`, `tests/test_client_context_inspect.py`.
