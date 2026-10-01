# Web app (`nexus web`)

A framework-free browser client (plain HTML, CSS and ES modules; **no build
step**) for the same per-workspace daemon. It mirrors the Textual shell: same
functionality, everything in the same place ([surfaces.md](surfaces.md)). Product
intent is in `plans/webplan.md`; the visual history is in root `design.md`
(its "Signal" finish is superseded by the browser-native finish below). The host
side is in [host.md](host.md).

## Parity with the TUI

Layout, color roles, conversation rendering, composer, context dialogs, settings,
slash commands, shortcuts, the Ctrl+X leader, model picker, voice and agent color
are specified once in [surfaces.md](surfaces.md). The web implementation of each
is named in the `app.js` map below; the shared logic has JS ports
(`tool-details.js`, `context-view.js`, `settings-files.js`, `providers.js`).

## Visual system

Browser-native while keeping the TUI's regions and button placement. One design
system applies everywhere (`tokens.css`, `app.css`).

- **Type:** system sans-serif for interface text and conversation prose
  (`--font-sans`); Monaspace Argon for code and diagnostics (`--font-mono`).
  The CSP allows no external hosts, so the font is vendored
  (`assets/monaspace-argon-latin-400.woff2/.woff`, fontsource
  `monaspace-argon@5.3.0`, SIL OFL 1.1) and declared at the top of `tokens.css`.
  To add weights or scripts, download more fontsource files into `assets/`; never
  link a CDN.
- **Shape:** three radius tokens: `--radius` 12px (cards, dialogs, composer),
  `--radius-sm` 8px (controls, rows, code blocks), `--radius-xs` 5px (tags, chips,
  keys); status dots are round. Use the tokens, never a literal `0` or `999px`
  (except dots and progress bars). No hard offset shadows; floating layers use
  `--shadow` / `--shadow-sm`.
- **Color:** `tokens.css` maps `ui/tui/theme.py` (`_DARK`/`_LIGHT`) role for role
  (`nx-accent` → `--accent`, `nx-blue` → `--info`, …); `data-theme` is `dark`,
  `light` or `system`. Preserve the shared semantic roles.
- **Components:** all status labels share the `.tag` shape (11px, semibold,
  capitals); section labels share one style; selected rows are a tinted or
  neutral fill with no side bar; Markdown renders as web prose (semibold
  headings, dot bullets, inline code chips); stroke SVG icons replace terminal
  glyphs in the same top-bar positions. `--cell` (8px) and `--row` (20px) keep
  the terminal grid relationship (at 1600px wide, one terminal column = one web
  cell); `--control-h` 28px is the common control height.
- **Layout:** on wide screens the conversation, composer, slash menu and approvals
  share one centered column (`--column`, 860px) and the timeline stays pinned to
  the newest message while the layout reflows.
- Verify any visual change with Playwright screenshots (dark and light at 1440,
  1024 and 400px) before calling it done ([testing.md](testing.md)).

## How it is served

`nexus web` (`cli.py:_web`) sends `WebLaunch` over UDS. `Daemon.web_launch()`
starts a loopback HTTP listener (`host/transports/http_sse.py`) and returns
`http://127.0.0.1:<port>/#ticket=…`. `host/web.py` (`BrowserRoutes`) owns every
browser route:

| Route | Purpose |
| --- | --- |
| `/`, `/s/<session>` | `index.html` (deep links are client-routed) |
| `/styles/*`, `/js/*`, `/assets/*` | static files from `nexus/ui/web/`, read on each request (reload to see edits; no daemon restart) |
| `POST /v1/web/ticket/redeem` | one-use, 60-second ticket → `HttpOnly; SameSite=Strict` cookie (12h) + CSRF token |
| `GET /v1/web/bootstrap` | CSRF token + workspace path |
| `POST /v1/web/command` | any host command as JSON `{type:'SessionList', …}` except `Shutdown` and `WebLaunch`; requires an exact `Origin` and `X-CSRF-Token` |
| `POST /v1/web/attachment` | JSON `AttachmentPrepare` only; same cookie/Origin/CSRF checks before buffering, 12 MiB request cap. Enqueue commands carry small draft IDs. |
| `POST /v1/web/voice?request_id=…` | raw mono 16 kHz PCM16 WAV for transcription; same auth checks; 8 MiB cap (commands cap at 1 MiB) |
| `GET /v1/web/session-view?session=` | versioned snapshot (`schema_version: 1`, `seq`, `view`) from `HostFacade.web_snapshot` |
| `GET /v1/web/session-events?session=&from_seq=` | SSE `view` frames of JSON-Pointer ops (`add`/`replace`/`remove`/`append`) or `resync: true` |
| `GET /v1/web/workspace-events` | SSE `workspace` frames with the session list (polled every 0.5s) |
| `POST /v1/web/logout` | drop the cookie session |

CSP: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;
connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none';
frame-ancestors 'none'`, plus `no-store`, `nosniff`, `no-referrer`,
`X-Frame-Options: DENY`, and a `Host` check. **No inline `<script>`, no
`style="…"` attributes, no external hosts.** Setting styles via the CSSOM from JS
(`el.style.setProperty`) is fine. Tickets and cookie sessions die with the daemon.
Packaged files are listed in `pyproject.toml` (`"nexus.ui.web" = ["index.html",
"styles/*.css", "js/*.js", "assets/*"]`).

## File map

| File | Contents |
| --- | --- |
| `index.html` | the whole DOM: SVG icon sprite (`#i-*`), `.app-shell` grid (`top`/`side`/`main`/`insp`) with the full-width `.topbar` (`▌` `#sidebar-toggle`, title, Context/Logs/Export, `#live-state`, `+` `#topbar-new`, `▐` `#inspector-toggle`), `#sidebar`, `.main-pane` (`#conversation`, `#timeline`, `#empty-state`, `.composer-wrap` with `#approval-strip`, `#slash-menu`, `#composer-form`, `#activity-bar`), `#inspector`, and overlays `#overlay`, `#settings-overlay`, `#context-overlay`, `#worktree-confirm-overlay`, `#voice-overlay`, `#setup-overlay`, `#agent-overlay`, `#toast-region` |
| `js/app.js` | all behavior; dense one-function-per-line style, so search by function name |
| `js/api.js` | `bootstrap()`, `command(cmd)`, `voice(wav, requestId)`, `snapshot(session)`, `eventUrl(session, seq)`, `exportSession` |
| `js/projection.js` | `applyOperations(root, ops)`: validates and applies patches on a detached copy (atomic) |
| `js/preferences.js` | localStorage detail level (session → workspace → browser precedence) and theme |
| `js/tool-details.js` | `toolDetailSections`/`renderToolDetails`: port of `ui_support/tool_details.py` |
| `js/context-view.js` | port of `ui_support/context.py`: `renderContextGroups`, `renderToolsReport`, `renderCurrentContext` |
| `js/settings-files.js` | Agents/Tools/MCP/Skills/Hooks/Config/Soul editors over `Settings*` (700 ms autosave, agent model/fallback form); port of `tui_settings.py` |
| `js/providers.js` | `createProviders({api, el, $, listId, isOpen})`: Settings → Providers cards, also used by first-run setup |
| `js/voice.js`, `voice-worklet.js` | microphone capture, resample to mono 16 kHz PCM16 WAV, bounded buffers; first use needs explicit confirmation |
| `js/mock.js` | dev-mode `/mock` and the `DEV` badge |
| `styles/tokens.css` | font-face, color tokens, radii, shadows, `--cell`/`--row` |
| `styles/app.css` | all layout and component CSS, sectioned by region, responsive rules last |
| `styles/context-preview.css` | context preview cards (line clamps are asserted by tests) |

### `app.js` map (search these names)

| Area | Functions / state |
| --- | --- |
| State | the `state` object (session, view, seq, sessions, tab, detail, theme, context, worktrees, logs, openFiles, health); `nodes` = keyed DOM cache for the timeline |
| Boot and routing | `start`, `autoPanel` (details panel opens by default ≥ 1280px unless closed; localStorage `nexus-web-panel`), `openSession`, `connectSession` (snapshot → SSE → `applyOperations` → `queuePaint`), `startWorkspaceStream`, `popstate` |
| Sessions sidebar | `renderSessions` (current and archived as one-line rows; visible Delete → `SessionDelete` to restorable trash), `archiveSession` (`SessionArchive`; both offer Undo), `#session-filter`, `toggleSidebar`/`syncSidebarToggle` (localStorage `nexus-web-sidebar`; overlay below 960px) |
| Header and composer | `renderHeader` (title, status, agent/model/effort, `--agent-color` on `#app`, `↵`/`■ stop`), `renderContextMeter` (`usageText`, same format as `context_usage`), `sendMessage`, `stopTurn`, drafts (`saveDraft`/`loadDraft` in sessionStorage) |
| Chat commands | `SLASH_COMMANDS` (mirrors `commands.SPECS`), `SLASH_ALIASES`, `parseSlash`, `renderSlash`, `runSlash`; `showText`/`closeText` for `/help`, `/hotkeys`, `/diff`, `/mcp`, `/skills` |
| Timeline | `renderTimeline` → `renderContextHeader` → `entities` → `renderMessage` (`renderUserChrome`), `renderTool`, `renderBoundary`, `renderFooter`; `markdown`/`updateMarkdown` (escaping mini-Markdown) |
| Approvals | `renderApprovals`, `approvalChoices`, `resolvePermission`, `pendingQuestions`, `answerQuestion` (`QuestionAnswer` by `call_id`), `permissionTargetsUnavailable` |
| Subagent page | `openAgent`/`closeAgent`/`renderAgentModal`: `#agent-overlay` at `/s/<session>/a/<agent>` (deep-linkable; Back, `←`, Esc return); `headerBlocks`, `agentTaskPrompt`, `fetchAgentContext`; swaps `nodes` for `agentNodes` and renders the child's body with the root renderers; re-rendered on every patch so a running child is live |
| Right panel | `renderDetails(force)` by `state.tab`; `renderOverview` (`modifiedFiles`, `diffRows`, `mcpSectionBody`), `overviewSignature`, `loadWorkspaceHealth` (`Doctor`, 20s cache), tools/agents tabs, `renderWorktrees*`, `renderLogs`/`readLogs` |
| Pickers | `showPalette`, `renderPalette`, `chooseFromList`, `chooseModel`/`commitModel` (Left/Right adjusts effort; `#palette-refresh` sends `ModelsRefresh` like `↻`/Ctrl+R in the TUI), `chooseAgent`, `cycleReasoningEffort` (Ctrl+T), `refreshModels` (`#palette-refresh`) |
| Settings | `selectSettingsPane`, `syncLayoutSettings`, `openSettings`, `renderSettings`, `installSettingGroups`, `installSettingsNav`, `renderSettingsWorkspace` |
| Context | `refreshContextPreview`, `openContextDialog(mode)` (`'system'` literal text, `'tools'`), `renderContextReport` (Expand all); the legacy inline preview stays hidden |
| Setup | `pollSetup`, `completeSetup` (`SetupStatus` / `SetupSave` without a model; no credentials transmitted) |

Details-panel tabs (Tools, Agents, Trees, Logs), Context/Logs/Export buttons and
the Settings dialog are browser-only and keep their positions.

## Responsive contract (asserted in `tests/playwright_web_check.py`)

The sessions sidebar is **272px**: docked from 960px, an overlay opened by `▌`
below. The details panel is **336px**: docked from 1280px, an overlay below.
Below 700px the top bar keeps only Logs. The page must never scroll horizontally.
Reduced motion disables transitions.

## Rules

- Parity with the TUI ([surfaces.md](surfaces.md)). When `app.tcss`, `theme.py`,
  `commands.py`, `app.py:SHORTCUTS`, `ui_support/timeline.py`, `tui_panels.py` or
  `tui_context_header.py` change, change the web to match, and the reverse.
- New data or actions are host commands; never compute server state in JS from
  files.
- Keep IDs stable: the Playwright check selects `#composer-input`,
  `#composer-model`, `#composer-agent`, `#reasoning-effort`, `#inspector`,
  `#inspector-toggle`, `#close-inspector`, `#tab-*`, `#logs-toggle`,
  `#settings-open`, `#settings-overlay`, `#settings-effective`,
  `input[name=theme|session-detail|workspace-detail|browser-detail]`,
  `#context-*`, `#timeline`, `.tool-card`, `.tool-status-text`, `.permission-card`,
  `.logs-source`, `.log-entry`, `.worktree-*`, `.sidebar`, `.palette-item`.
- Stale async results: compare `session` and request ids (`state.selectionRequest`,
  `state.context.requestId`, `state.logs.requestId`) before applying.
- Host text is untrusted: build DOM with `el(tag, cls, text)`/`textContent`;
  `markdown()` escapes before formatting; never `innerHTML` a server string.
- Overlays set `#app` `inert` and trap Tab; Escape or a backdrop click closes the
  top-most layer; dialogs focus themselves on open. Follow the existing
  open/close helpers.
- Dictation: the runtime is included in the standard install; a separate model download is
  required; the confirmation dialog precedes any download and stays open during
  preparation until the user acknowledges readiness. Real-model inference and
  network behavior are not verified; do not claim proven offline operation.

## Dev loop

With a real daemon and a scripted model (no API key):

1. Write a script modeled on `tests/playwright_web_check.py:main()`:
   `ScriptedProvider(...)` → `Runtime(path, config=Config(...), providers={"scripted": provider})`
   → `Daemon(workspace, socket_path=Path("d.sock"), runtime_factory=...)`; print
   `await daemon.web_launch()`. Keep the socket path short (macOS caps UDS paths
   near 104 bytes).
2. Pre-create sessions with `daemon.facade.open_session(id, create=True, recover=True)`
   and `await daemon.facade.start_turn(id, prompt)`. Add `.agents/mcp.json` (see
   `tests/fixtures/mcp/fs_server.py`) to exercise the MCP panel.
3. Tickets are one-use; mint more from the daemon, or over UDS with
   `UDSClient.connect(sock).call(p.WebLaunch())`.
4. Permission prompts wait only while a viewer is attached; otherwise
   `on_unattended` (default `deny`) applies.
5. Screenshot with Python Playwright (`.venv`). To compare with the terminal,
   render the Textual shell against the same daemon:
   `NexusTextualApp(client, session=…).run_test(size=(200, 55))` → `export_screenshot()`.

## Testing

- `.venv/bin/python tests/playwright_web_check.py`: end-to-end (terminal/browser
  handoff, streaming, approvals, detail levels, settings, logs, worktrees,
  breakpoints, reduced motion, XSS probes); writes `artifacts/web-e2e/`; takes a
  few minutes; seeds `nexus-web-panel=closed`.
- `tests/playwright_context_web_check.py`, `playwright_message_check.py`: context
  and queued-message flows.
- `tests/test_web_transport.py`: routes, auth, CSRF, CSP, snapshot/patch transport.
  (`tests/test_browser_serve.py` covers the Textual browser bridge, not this app.)
- Host projection: `tests/test_host_facade.py`, `test_host_logs_read.py`,
  `test_client_context_inspect.py`.

## File and image input

`/attach <path>` attaches a local file (`/attach clear` removes pending
attachments). The browser also has an Attach file button and accepts image/file
paste and drag/drop in the composer. Expand an attachment to inspect it before
sending; the TUI opens converted documents in a scrollable Markdown preview.
Enter submits attachments even without prompt text; queue, steer, and interrupt
use the same attachment path. Switching sessions clears pending attachments.

PNG, JPEG, GIF, and WebP stay image blocks for vision-capable models (labelled
metadata in the terminal, visible previews in the browser, also after
reconnect). AnyDoc converts PDF, Word, PowerPoint, Excel, OpenDocument, RTF, EPUB
and CSV to Markdown and is installed by default. Text/source files are included
directly, including Unicode BOM encodings. Unsupported binaries, malformed
documents and scanned PDFs needing hosted OCR (disabled) give a visible error.
Limits: 8 MiB per file, eight per message, 12 MiB encoded combined. Prepared
drafts expire after one hour; submitted content stays in the durable log.

Validation: `tests/test_attachments.py`, `tests/test_tui_attachments.py`,
`tests/playwright_attachments_check.py`.

Escape and Ctrl+C dismiss open dialogs and Settings (including nested screens)
and restore focus on the main conversation. On the main conversation, two Escape
presses within 1.5 seconds cancel the active turn and return pending queued messages to the composer via
`SessionCancel(return_queue=True)`. Messages keep queue order, separated by blank
lines, followed by any existing unsent draft; they no longer run automatically.
A single Escape shows a stop hint. Ctrl+C retains immediate
turn cancellation on the main conversation.

Attachments insert editable `image 1`, `image 2`, or `document 1`,
`document 2` references at the composer cursor (each kind is numbered separately
within a draft). Preview rows show the same reference and filename. Removing a
browser attachment does not renumber remaining references. Submitted attachment
metadata carries the same labels beside the image bytes or complete document text
in the durable user message, so references in sentences stay meaningful on replay.

Submitted messages separate the prompt sentence from numbered attachment rows.
The browser shows labelled image thumbnails and expandable full document cards,
also in request-context messages. The terminal shows compact labelled rows;
clicking the message body opens the complete attached text and image metadata.

Inline Edit/Patch diffs stay in two columns at every width: original on the left,
updated on the right. Long lines wrap within their column.
