# Nexus web app plan

## Goal

Build a polished browser client for the same per-workspace Nexus daemon used by `nexus chat`. A turn started in Textual must be visible and actionable in the browser while it runs; a browser turn must be equally resumable in Textual. Closing either view must not stop the work. The browser app uses plain HTML, CSS, and JavaScript: no React, Vue, Svelte, component framework, CSS framework, or client-side router dependency. Keep the web client ready to be hosted inside a desktop window later, without putting desktop APIs into the core.

This is a living implementation plan. The browser client, host transport, and UI redesign exist in the worktree; broader parity and review gaps below remain open. The reference image's Git panel, commit controls, and other features without a shared host contract have not been implemented. The worktree contained substantial unrelated, uncommitted host and TUI changes; those were preserved.

## Implementation checkpoint — 2026-09-25

**Status: browser UI and Logs diagnostics checkpoint verified.** The browser connects to the same daemon and session log as the terminal. The dark-first interface provides session navigation, transcript/tool/approval views, a pinned composer, optional inspector, browser-local theme and scoped three-level detail preferences, responsive layouts, safe rendering, and reconnect recovery. Root reasoning-effort selection is exposed in the browser and cycles from Ctrl+T in the focused Textual composer, backed by shared host metadata and selection commands. The Logs checkpoint adds a bounded, read-only `LogsRead` command for sanitized daemon and session lifecycle diagnostics, a Textual right drawer, and a browser inspector Logs tab. Broader product parity and manual assistive-technology/screen-reader review remain open. The sections below remain the target design.

### Implemented in the current worktree

| Area | Current implementation | Main files |
| --- | --- | --- |
| Launch and transport | `nexus web` asks the running daemon over its owner-only Unix socket to enable a loopback listener; it does not restart the daemon. The listener serves packaged static assets and deep links, and exposes authenticated browser commands, a session snapshot, session SSE, and a workspace SSE feed. The existing bearer-authenticated peer routes remain separate. | `nexus/cli.py`, `nexus/host/daemon.py`, `nexus/host/protocol.py`, `nexus/host/web.py`, `nexus/host/transports/http_sse.py` |
| Browser authentication | A one-use, 60-second launch ticket in the URL fragment is redeemed for a 12-hour `HttpOnly; SameSite=Strict` cookie. The client removes the fragment after redemption. Browser mutations require an exact Origin and CSRF token; browser commands cannot request daemon shutdown or another web launch. Static responses have a restrictive CSP and other response headers. Credentials are held by the daemon process and expire on restart. | `nexus/host/web.py`, `nexus/ui/web/js/api.js` |
| Shared view projection | The host builds a versioned web snapshot from the Python `ConversationView` with stable IDs and full assistant text. It emits ordered JSON Pointer add/remove/replace operations plus an append operation for growing strings; normal text streaming does not retransmit the growing transcript. A frame over 256 KiB tells the client to resnapshot. A separate workspace feed sends changed session summaries using a bounded 0.5-second metadata poll. | `nexus/host/facade.py`, `nexus/ui/web/js/projection.js` |
| Browser UI | Plain HTML, CSS, and ES modules, with no web framework or Node build step. Session list/new/open, live transcript and tools, composer/stop, approvals, model/root-agent selection, child-agent inspector, fork/export, palette/settings, dark/light theme, browser/workspace/session detail precedence, responsive sidebar/inspector, reconnect, and per-session drafts. Projection patches stage on a detached copy and commit atomically. | `nexus/ui/web/`, `pyproject.toml` |
| Browser test harness | Offline scripted daemon plus real Chromium checks handoff/reload/sequence gaps/first-responder approval; all three detail levels and storage precedence; grouping/fail-open states; keyboard and IME handling; inspector/diff and Logs polling/drawer states; exact responsive boundaries, 200%-equivalent CSS viewport, malformed patch recovery; and named theme/state screenshots under ignored `artifacts/web-e2e/`. | `tests/playwright_web_check.py` |

The present UI does **not** yet establish full parity with every Textual action or the wider CLI. In particular, the workspace utility screens, queue-next-prompt controls, JSONL export, and the future shared attachments/questions/worktrees/Git review are not complete. Nested-agent transcript and tool details are exercised with deterministic projection fixtures. Treat the parity ledger below as acceptance criteria, not as a list of verified features. Long-history and assistive-technology review remain open.

### Logs diagnostics behavior and limits

- `LogsRead` is a sanitized, read-only lifecycle diagnostic projection, not a raw `.log` reader. It never returns full tool inputs/results, prompt or thinking text, permission previews, provider errors, file paths, or arbitrary event fields. Daemon entries use reviewed fixed summaries in an in-memory ring (up to 512 entries / 128 KiB) and therefore cover only the current daemon lifetime; a generation-qualified cursor marks restart/history gaps. Session entries are allowlisted lifecycle events scanned within a 1 MiB read and 4,096-record window, with page limits and explicit truncation/gap status.
- Textual opens/closes its right drawer immediately with Ctrl+E, polls while open, and stops polling on close/unmount. The browser offers Ctrl+E when the browser delivers it and a visible **Logs** toolbar button fallback (some browsers reserve Ctrl+E); its visible-tab drawer polls every 1.8 seconds, cancels or rejects stale requests on close/session switch, marks gaps/restarts, and honors reduced-motion CSS. The TUI no-level status display has been removed.
- Chromium covers Logs tab/polling, failed and delayed reads, drawer modal/focus behavior, and dark/light/narrow screenshots. Screenshot review found no drawer or log-row horizontal overflow. The narrow light drawer capture includes two simultaneous “Connection lost” notices at the bottom; investigate the duplicate notice independently. Adversarial control characters render as replacement glyphs, and the bounded long-tail summary may wrap or show a clipped tail; neither exposed raw payload in the reviewed captures.

### Verification closeout evidence — 2026-09-25

- **Latest final verification:** the supported command `NEXUS_PHASE3_WRITE_REPORT=1 .venv/bin/python -m pytest -q tests/test_phase3_exit.py::test_phase3_exit_baseline_report` passed (**1 passed in 362.37s**) and regenerated both `tests/fixtures/reports/phase3_exit_baseline.json` and `.txt`. Its embedded full-suite result was **3,839 passed, 311 skipped, 3 deselected** in 356.74s; the report's Phase 4 references were **2 passed**. `.venv/bin/python -m pytest -q tests/test_phase3_exit.py`: **13 passed in 4.52s**.
- The combined Textual matrix (`test_ui_tui.py`, `test_tui_session_switch_race.py`, `test_tui_model_picker_repro.py`, `test_tui_model_selection_integration.py`, `test_tui_functional_journeys.py`, `test_tui_completion.py`, `test_tui_keys.py`, `test_tui_layout.py`, `test_tui_integration_render.py`): **127 passed in 38.75s**. `.venv/bin/python tests/playwright_tui_check.py` exited successfully and reported transcript/tool-card rendering, multiline submit, palette, keyboard and mouse model selection, agent picker, rejected-model status, running-turn draft retention, Logs/reasoning/session commands, and responsive resize. `.venv/bin/python tests/playwright_web_check.py` passed the terminal-to-web handoff with a **39 ms** first delta. `git diff --check`: **passed**.
- The apply_patch catalog expectation reconciliation is intentional: **194 affected tool/security tests passed** after correcting the catalog expectations. This verification window started with the already-dirty worktree shown in the initial status snapshot; no additional source change was observed during these runs. The report's embedded suite is evidence for the worktree state it measured, not a claim that unrelated uncommitted changes were absent.
- **Current physical-line measurements in the regenerated report:** core+model+spec **13,114 / 14,000**; host **6,997 / 7,000**; view **2,060 / 2,200**; UI **4,786 / 5,000**. All four budgets are under cap; host has 3 lines of headroom. The JSON report records matching per-directory figures.
- Manual TTY interaction, independent manual IME verification, and screen-reader/assistive-technology review remain outstanding; automated checks do not replace those reviews. Broader Textual-to-web parity, workspace utility screens, queue-next-prompt controls, JSONL export, longer-history recovery, and the planned attachments/questions/worktrees/Git review remain incomplete or unverified. Unsent Textual drafts are retained during a running turn; persistence of separate unsent drafts per session is not implemented/verified.

### Independent review follow-up — 2026-09-25

- Direct `start_turn(content)` now records a turn-scoped `input.started` event with stable input identity/content. It is separate from queue lifecycle events, so only actual `enqueue()` submissions emit `input.queued`/`input.consumed`; the canonical view reconstructs direct user messages from `input.started` on replay.
- The web projection reuses unchanged serialized turns, messages, and agents by identity, avoiding recursive serialization of unchanged transcript branches per token. Changed messages and blocks are serialized again, and the compatibility top-level `messages` array is flattened across history per patch; removing that O(history) traversal requires changing the versioned projection contract and remains open. Regression coverage asserts reuse without a wall-clock threshold, including agent reuse by stable ID when an ordered entry has no old wire projection.
- **Remaining durability risk:** the direct user `MessageRecord` is written during turn preparation before the detached producer emits `input.started`. A crash in that window leaves the prompt durable without its matching input event/projection identity. This cleanup does not move the event commit point; recovery for that window remains unaddressed.
- Generic web projection strings are clipped to the reducer's `MAX_TEXT` (8192) except assistant text/thinking blocks. Permission previews are already limited to 200 characters by `PermissionEngine.request_for`; error strings and other generic fields are clipped at the host projection boundary. Tests cover these distinct paths.
- Sequence tests now inject duplicate and stale events and verify only seq 3 and seq 5 frames are emitted from a cursor at 1, with the intervening appended text retained. Playwright inspects session record kinds and asserts the user `MessageRecord` consumes the sequence between event frames, then verifies the durable `input.started` follows.
- The prior combined surface allowance is superseded. Current independently reviewed caps are `host` 7,000, `view` 2,200, and `ui` 5,000. The host cap was raised from 6,500 for the distinct browser, bounded file-completion, agent-metadata, and redacted-diagnostics surfaces; UI was raised from 4,600 to 5,000 for the right drawer and expanded composer/picker alongside the browser client. These are independent budgets and ratchets; aggregate line/file totals are verified as exact per-directory sums. The generated report was refreshed by the passing supported regeneration recorded above.

### Corrections and immediate work remaining

1. **Complete.** Added the intentional seq 1 → 3 facade projection regression; reran facade, browser transport, HTTP/SSE transport, and daemon replay/handoff tests; passed the Playwright approval-first-responder flow; removed temporary diagnostic output from the browser test.
2. Finish an explicit Textual-to-web parity pass. Exercise actual tool diffs, nested agent transcripts, model/agent selection from each view, cancel, queue, fork at a chosen point, exports, delete/restore, and the applicable CLI utility screens. Complete missing host commands rather than reading daemon files in JavaScript.
3. Test reconnect after SSE loss and daemon restart, long-session snapshot behavior, slow consumers, session switching, browser history, and uncertain command responses. The current snapshot route has a **32 MiB** response ceiling and returns 413 above it; longer-history pagination or another bounded recovery path is still needed. Command IDs and idempotent `SessionStart`/`SessionEnqueue` retries are still planned, so the client must not automatically retry an uncertain send.
4. Run the broader relevant test suite and packaging check, then inspect Playwright screenshots and keyboard/accessibility/security cases at the sizes and states in the acceptance section. The supported Phase 3 report suite and targeted TUI/web checks have passed, but the broader packaging, full parity, long-history, and manual accessibility review remain outstanding.

The snapshot HTTP route initially called the synchronous `web_snapshot` with `await`, making it return 404; that call was corrected before this checkpoint. The sequence correction is also reflected in the reconnect contract below so future changes do not reintroduce the false-gap loop. The first focused suite run also caught a test fake that incorrectly declared this API async; the fixture now matches production. Earlier embedded full-suite counts are historical; the latest generated report records **3,839 passed, 311 skipped, 3 deselected** and Phase 4 **2 passed**.

## Existing contracts to build on

- `nexus/host/facade.py` is the sole UI-facing runtime API. The per-workspace daemon owns sessions, turns, permissions, model and agent selection, and scheduling. `nexus/ui/**` must not reach into managers or tools.
- `nexus/host/protocol.py` defines tagged, versioned commands/results. Textual uses the Unix socket. `nexus/host/transports/http_sse.py` retains bearer-authenticated peer commands and session events, with `seq` as the SSE ID, strict Origin checks, and bounded slow-client queues. The current worktree also adds the separate browser app, browser login, and workspace session index stream described above.
- The append-only session log and `nexus/view/reduce.py` produce the canonical `ConversationView`: turns, streaming text, tool cards and diffs, nested agents, permissions, queue, usage, context, and presence. `SessionState` keeps its existing wire shape; the new `web_snapshot` serialization includes reducer-local message/tool IDs for keyed browser updates.
- The current Textual chat commands are `/new`, `/sessions`, `/model`, `/agent`, `/tools`, `/details`, `/reconnect`, `/cancel`, `/fork`, `/export`, `/help`, and `/exit`; it has a command palette, keyboard shortcuts, a model picker, a root-agent picker, a live child transcript, and a four-choice approval screen. The wider CLI also has session delete/restore, model inspection/refresh, tools, agents, extensions, doctor, and daemon commands.
- `IMPROVEMENT_PLAN.md` describes planned features such as attachments, path completion, worktrees, aggregate changes, and agent questions. They are not prerequisites to ship the current Textual feature set in the browser. When those features land, put their state and commands in the host contract so both clients can use them.

## Architecture and ownership

```text
                         append-only session log
                                  │
                 ┌────────────────┴────────────────┐
                 │  core + managers + supervisor    │
                 │  permissions + Python reducer    │
                 └────────────────┬────────────────┘
                                  │ HostFacade / versioned protocol
                    ┌─────────────┴──────────────┐
                    │                            │
          Unix socket client             loopback web transport
                    │                            │
             Textual chat + CLI          static HTML/CSS/JS app
                    │                            │
              terminal view                  browser view
```

The core owns all durable facts and policy. Textual owns terminal rendering and terminal input. The web app owns DOM rendering, browser input, local drafts, layout, and theme. Neither client runs a model, mutates the session log directly, evaluates permissions, or calculates a separate answer to a core state transition. Both clients may be open concurrently, each on a different session or on the same session.

Place browser assets under `nexus/ui/web/` (`index.html`, `styles/*.css`, `js/*.js`, local icons). Put HTTP routing, browser authentication, view projection, and workspace notifications under `nexus/host/`; keep these usable without importing Textual. A packaged install must include the static files. Add a small `nexus web` launcher through the existing CLI. Keep the existing bearer-authenticated HTTP peer API for non-browser clients; add a browser-specific bootstrap and session-auth path without weakening it.

The future desktop shell should load the same static app and call the same versioned local API. Only the small launcher/auth/window adapter changes. Do not rely on Electron-style globals, a browser extension, `file://` access, or a particular desktop framework. Use feature detection for window controls, file pickers, notifications, and download/share; the browser version remains fully usable without a native bridge.

## Functional parity ledger

The first web release should cover every **current interactive Textual chat** capability. The utility screens below bring the wider local CLI into the same app where they are meaningful. Each row must have a working mouse path and a keyboard path.

| User action | Browser interaction | Shared host contract / source |
| --- | --- | --- |
| Start or resume a session | New button; searchable recent/workspace list; direct session URL | `SessionOpen`, `SessionList`, `SessionState` |
| Send multiline prompt | Composer with Enter to send, Shift+Enter for newline; clear send state | `SessionStart`, event log |
| Follow a running turn | Live transcript, stop action, activity and reconnect banner | Session event/view stream, `SessionCancel` |
| Continue work from either UI | Open same session ID; show whole history and live tail | Same log, `SessionState`, replay from `seq` |
| See turns, Markdown, tool status, progress, Edit/MultiEdit diffs | Keyed timeline cards, expandable bounded details/diff | Canonical `ConversationView` |
| Inspect nested Task agents | Inline child cards; right-panel transcript with breadcrumbs | `AgentTranscript`, agent view projection |
| Decide approvals | Focused four-action sheet; another-view-won state | `PermissionResolve`, permission events |
| Choose model/tier | Searchable model picker; current versus next-turn labels | `ModelsList`, `ModelShow`, `ModelTiers`, `ModelSelect` |
| Choose/reset root agent | Agent picker, source label, next-turn notice | `AgentsList`, `AgentCurrent`, `AgentSelect`, `AgentReset` |
| Inspect tools, model, context, usage, queue, approvals, daemon/session lifecycle diagnostics | Details inspector, Tools tab, and Logs tab; Textual Ctrl+E drawer | `ConversationView`, `ToolsList`, `LogsRead` |
| Fork at latest or a chosen sequence | Fork action in session menu; open child | `SessionFork` |
| Export | JSON, Markdown, JSONL download | `SessionExport` |
| Reconnect | Automatic retry plus explicit Reconnect action | Session snapshot + stream cursor |
| Discover commands/shortcuts | Searchable command palette and Help | Web action registry matching current Textual command set |
| Leave a view | Close tab/window; turn remains daemon-owned | Presence detach; no session mutation |

The current Textual composer refuses another send while its turn is running. The core already supports durable queued input through `SessionEnqueue`; the web app may expose **Queue next prompt** as a separate labeled action once its cancel/drop semantics and Textual discoverability are checked. It must never silently convert Send into queueing. Unsent drafts are local to each view in v1; shared **submitted** prompts, queued prompts, turns, selections, and decisions are durable and sync across clients. If cross-device draft sync is later required, give it an explicit revisioned host contract.

Add a secondary **Workspace tools** area for the wider CLI: session delete/restore (with busy state and trash ID), model catalog/refresh, available agent and tool lists, extension list/reload/validate/trash, and redacted doctor status. Some existing methods have no wire command yet, for example listing trashed sessions; extend the protocol rather than reading daemon files from JavaScript. Local credential login/logout and daemon shutdown are separate owner-only operations; do not expose a raw shell or secrets to the web app. `/exit` maps to closing this view, not stopping the daemon. A web command palette may accept the familiar slash names, but its execution maps to host commands and UI actions rather than sharing Textual widget code.

## Real-time sync contract

### 1. One authoritative session projection

Do not reimplement `nexus/view/reduce.py` in JavaScript. Add a versioned **web view projection** beside the Python reducer that serializes stable `turn_id`, `message_id`, `call_id`, agent ID, and block identity, while preserving the existing `SessionState` wire shape for current clients until a deliberate protocol migration. Keep untrusted previews bounded and sanitized at the host boundary. The browser applies projection changes to plain JS state and paints the DOM; it does not infer permission, queue, or turn transitions from raw text.

Define two host operations:

1. `GET /v1/web/session-view?session=...` returns `{schema_version, session, seq, view}` from a consistent event prefix. The view includes the renderable projection; the client also fetches agent selection metadata through the existing host command.
2. `GET /v1/web/session-events?session=...&from_seq=N` streams `{schema_version, session, seq, ops}` for events with `seq > N`. The current host reduces each event with the Python reducer and emits JSON Pointer add/remove/replace operations, plus a small `append` operation for normal token streaming. A replacement of the changed value is the safe fallback. A patch above 256 KiB becomes an explicit `resync` frame; it does not send a whole growing transcript for each token. If a future protocol introduces named keyed operations or event IDs, migrate and version both sides together.

The projection operation builder must prove that applying its operations to a view at `N` equals a fresh Python reduction at every subsequent sequence. Compare both paths in tests over a recorded event corpus, including duplicate final `text`, nested agents, malformed/unknown events, retries, compaction, permissions, and tool diffs. This is the anti-drift rule between Textual and web.

### 2. Gap-free attach and reconnect

For session `S`, fetch its snapshot at sequence `N`, render it, then subscribe from `N`. The append-only log replays events after `N`, including any events written between the HTTP requests. Track `lastAppliedSeq` per session. Ignore duplicate `seq <= lastAppliedSeq` and accept any subsequent `seq > lastAppliedSeq`: session sequence numbers also cover message and snapshot records, so consecutive event frame numbers are **not** guaranteed. `Session.subscribe` recovers dropped event-bus messages from the log. Use an explicit host resync signal for unrecoverable loss; on changed schema, malformed patch, such a signal, 401, or daemon restart, close the stream and obtain a fresh snapshot before reopening it. Never mark an optimistic turn as durable until the host acknowledges it. A client-generated command ID and idempotent `SessionStart`/`SessionEnqueue` semantics are still needed so retry after an uncertain network result cannot submit the same prompt twice.

Use one live session stream for the active session and abort it on switching. Preserve each session's viewport position and local composer draft in memory; an optional `sessionStorage` draft survives refresh on the same browser. Key state by **workspace + session ID**, never by sequence alone. The existing server SSE backlog limits remain; a slow browser disconnects and catches up from the log, with no pressure on the producer. Use `visibilitychange` to reduce paint work in a background tab, while keeping the stream or reconnect path alive so approvals and completion are not missed.

### 3. Workspace index and cross-view actions

Add a daemon-owned workspace index feed for `SessionList` summaries, active state, title, `last_seq`, `last_activity`, viewer count, creation/fork, trash/restore, and deletion. A small in-memory revision is enough: each reconnect starts with `SessionList`, then listens for changed summaries. The revision resets on daemon restart; the client then reloads the list. This keeps the left sidebar current when Textual starts or completes a turn in a different session. Do not open one SSE stream per sidebar row.

Model/agent changes are session facts. After a `ModelSelect` or agent selection from either client, both views update the selected-next-turn label from durable events; the effective model for an active turn stays derived from `model.started`. If `AgentCurrent` metadata is not fully represented by existing events, emit a durable selection event with the required fields. The workspace feed can prompt the browser to refresh metadata for a background session when needed.

Presence is a count, not a control lock. Every browser tab and Textual attachment has a unique client ID. An approval is shown to all attached views; the first valid `PermissionResolve` wins under `HostFacade` and all others reconcile to the recorded decision. Closing, switching, or losing the browser must not leave a local blocking modal or silently grant permission; the existing unattended policy remains daemon-owned. If another view answers, show “Answered in another view” and dismiss the actionable controls. Cancellation is session-wide and visible to both clients. A viewport tab or browser reload never cancels a turn.

### 4. Browser launch and security

Native `EventSource` cannot set the current bearer `Authorization` header, and a normal page cannot read the mode-`0600` discovery file. Do not place the daemon bearer token in JavaScript, a query string, `localStorage`, or an unauthenticated static page.

`nexus web` should connect through the owner-only Unix socket and use a UDS-only daemon control operation to start or attach the loopback web listener **without restarting a running daemon**. It then requests a short-lived one-use launch ticket and opens the local page with that ticket in the URL fragment. The fragment is removed immediately with `history.replaceState`; it is never sent by browser navigation. Redeeming the ticket in a same-origin POST body creates a fresh, short-lived, host-only `HttpOnly; SameSite=Strict` browser session cookie. The daemon bearer remains for existing peer API clients. Browser command POSTs also carry a separate CSRF token delivered by an authenticated bootstrap response; enforce exact Origin and token checks. Make cookie renewal and logout explicit, and invalidate tickets/cookies on daemon restart. If opening a browser is unavailable, print the one-time launch URL only to the invoking terminal, never to a daemon log.

Serve packaged, immutable local assets from the same loopback origin, with a restrictive CSP (`script-src 'self'`, no inline script, no remote fonts/assets), `X-Content-Type-Options: nosniff`, no framing, no service-worker registration, and no cache for authenticated API responses. Separate browser routes from the existing peer routes: ordinary HTML navigation and static GETs normally have no `Origin`, so validate the loopback `Host` and fixed asset paths there; require a valid cookie for data streams and exact Origin plus CSRF for mutations. Keep the peer API's current bearer **and** Origin requirements unchanged. Escape text by default and sanitize Markdown to an allowlist before inserting DOM; disable HTML in assistant Markdown. Never convert a file path from a tool result into arbitrary browser file access. Keep server side body, event, diff, and download limits. Avoid a remote-network mode in this release.

## Interface design

The references establish the hierarchy: a compact session/workspace sidebar, a calm conversation canvas, a bottom composer, and a contextual right inspector. They also show light and dark versions of the same arrangement. Recreate the **structure and polish**, not decorative macOS traffic lights in the browser: the browser already has real window chrome. Reserve a clean 48–52 px top bar so a future desktop shell can provide native titlebar drag regions and controls through the adapter.

### Large window layout (about 1280 px and wider)

```text
┌───────────────┬──────────────────────────────────┬─────────────────────┐
│ Workspace     │ Session title · branch · status  │ Inspector tabs      │
│ New / Search  ├──────────────────────────────────┤ Conversation/Agent  │
│ Recent        │ Transcript                        │ Tools/Details/Git*  │
│ Sessions      │  user prompt                      │                     │
│               │  assistant blocks                 │ contextual content  │
│               │  tool and Task cards              │                     │
│               ├──────────────────────────────────┤                     │
│ Settings      │ Composer                          │                     │
│               │ model · agent · context · Send     │                     │
└───────────────┴──────────────────────────────────┴─────────────────────┘
```

Use CSS Grid with a resizable 232–280 px sidebar, `minmax(520px, 1fr)` main column, and a 320–420 px inspector. Persist widths per browser/workspace. Do not let a wide right panel squeeze the transcript below a readable measure: the conversation body has a 760–860 px maximum line width and generous horizontal margins. Pane dividers are hairlines; surfaces, typography, and spacing carry the hierarchy. The inspector is contextual and can close with one click; the main conversation always has priority.

The top bar shows the session title, workspace name, branch/path **only when the host exposes them**, running/idle connection state, context usage, and a compact command/search action. Give session title the strongest weight and secondary metadata a quieter tone. Do not display a fake context percentage or dollar cost: show used/input budget and, separately, the full model window only when known. Show effective model for the running turn and selected model for the next turn as distinct labels.

The sidebar has New session, search (`⌘/Ctrl+K` or `⌘/Ctrl+O`), Recent, and the current workspace's sessions. Each row shows title, short ID/recency, a small running/approval indicator, and an unread activity dot based on `last_seq` versus that browser's viewed cursor. Running sessions update in place. Context menu: Open, Fork, Export, Delete. Search by title or ID; arrow-key navigation and Escape work in every picker. With no sessions, show a short ready/setup state and one New session action.

The transcript is a chronological, accessible list keyed by durable IDs. User prompts are lightly tinted aligned cards; assistant prose is left aligned with comfortable paragraph spacing and safe Markdown; streaming text grows in place without layout flashes. Tool cards show name, status, duration, brief bounded result/progress, and expandable input/result. Edit and MultiEdit open a split or unified diff viewer with a monospace font, line numbers, red/green semantics plus explicit `+`/`−` labels, and copy actions. Task cards show agent name/task, state, nested count, and open a live right-panel transcript with breadcrumbs back to the parent. Reasoning/thinking appears only when the provider supplies it and the shared view exposes it; collapse it by default with a clear label. Keep timestamps and provider metadata in quiet footers or on hover/focus.

The composer is pinned below the scroll region, not over transcript content. It has a multiline `<textarea>`, a clear focus ring, visible Send/Stop action, model and agent chips, context/queue status, and a simple “Commands” affordance. Enter sends; Shift+Enter inserts a newline; on IME composition Enter must not send. When a turn is running, Stop is prominent and sending is disabled unless the explicit queue action is enabled. Preserve a draft per session when switching. Show exact error text from a redacted host result and keep the draft on send failure. Attach/paste/drop controls should appear when the shared attachment pipeline described in `IMPROVEMENT_PLAN.md` exists; do not ship decorative nonworking icons.

Map primary shortcuts explicitly: `⌘/Ctrl+K` opens the palette, `⌘/Ctrl+N` starts a session, `⌘/Ctrl+O` searches sessions, `⌘/Ctrl+F` forks the active session, `⌘/Ctrl+G` chooses a root agent, and Escape closes the topmost sheet/drawer. Provide a separate visible Stop button and shortcut that does not conflict with the browser's copy command; `Ctrl+C` in Textual remains its terminal-specific binding. Put the shortcut beside each palette result and in Help. Never intercept standard text-editing keys inside the composer or a search field.

The right inspector defaults to Details/Tools or the selected Task agent. Details includes model/provider, usage, context assembly and compaction, queue, approvals, subagent tree, and event/turn diagnostics. The **Git** tab seen in the reference is a later host feature: show it only after the aggregate change-review contract exists. Its planned design is staged/unstaged/untracked groups, per-file counts, full diff, branch/worktree identity, and commit/revert actions backed by explicit host operations and confirmations. Never infer repository state from rendered tool output or run Git from browser JavaScript. Commit/push/sync are separate actions with distinct scopes and should not be implied by a single ambiguous button.

Approvals use a focused sheet above the composer with tool, requested action, scope, and four labeled choices: Allow once, Allow for session, Deny once, Deny for session. If persistence is unavailable, label the choices accurately. Default focus should favor the safe choice; Escape resolves as Deny once. Include the agent/request identity if a child initiated it. The sheet survives a stream reconnect by reconstructing from pending permission state and disappears only after a durable resolution or explicit denial. Distinguish a network error from another view winning the approval.

### Visual system

- Use system UI fonts (`-apple-system`, `BlinkMacSystemFont`, `Segoe UI`, sans-serif) and a local monospace stack for paths/code. Body text 14–15 px with 1.5 line height for long AI output; title 17–20 px; compact metadata 11–12 px. Avoid tiny type in the transcript.
- Use an 8 px spacing rhythm, 28–34 px controls, 36–42 px session rows, 8–12 px card radii, subtle layered shadows only for floating surfaces, and thin separators. Icons are local SVGs with 1.5–2 px strokes and text labels/tooltips for ambiguous actions.
- Define independent light and dark token sets, matching the reference's warm off-white and charcoal surfaces. Example roles: `canvas`, `sidebar`, `elevated`, `hover`, `border`, `text`, `muted`, `accent`, `success`, `warning`, `danger`, `code-bg`, `diff-add`, `diff-remove`. Use an amber/coral accent sparingly for focus and active session, and preserve conventional green/red only for actual outcomes. Respect `prefers-color-scheme`, allow a manual override, and never simply invert colors.
- Keep the main reading canvas opaque. A restrained blur may be used on popovers or the sidebar with a solid fallback. Use 140–220 ms opacity/transform transitions for menus, sheet opening, and pane resizing; preserve scroll position during updates; honor `prefers-reduced-motion`.
- Meet WCAG AA contrast, visible focus, full keyboard operation, semantic buttons/headings/lists, `aria-live` for new status without announcing every streamed token, and correctly labeled approval decisions. Test at 200% zoom and with a screen reader. Color must never be the only status cue.

### Smaller windows

At 960–1279 px, collapse the inspector into an on-demand drawer. At 700–959 px, compact the sidebar to an icon rail or a toggleable drawer and keep transcript/composer full width. Below 700 px, use a single-column session list or conversation view, a full-height inspector overlay, and touch-sized controls. All primary flows—send, stop, select session/model/agent, inspect a child, answer approval—must still work. Use CSS container queries or simple media queries, not a JavaScript layout framework.

### Explicit states and feedback

Design and screenshot these states in both themes: fresh workspace, empty session, long transcript, streaming text/tool/child agent, pending approval, another view answered, queued work, disconnected/reconnecting, model unavailable, failed turn, narrow window, and reduced motion. For new users, the empty state shows the chosen model/agent, a brief prompt suggestion, and a shortcut hint. Toasts acknowledge reversible UI actions; durable actions show their server-confirmed state. A connection banner says a turn may still be running in the daemon and offers Reconnect. The app must never clear the transcript merely because the stream is down.

## Frontend implementation without frameworks

Use ES modules and native browser APIs. Suggested modules: `api.js` (typed command wrapper, auth, aborts), `stream.js` (SSE parsing/reconnect and sequence checks), `store.js` (plain state and subscriptions), `projection.js` (apply **host-defined** view operations), `session-list.js`, `timeline.js`, `composer.js`, `inspector.js`, `permissions.js`, `palette.js`, and `router.js` (History API only). Keep the action registry as data so buttons, slash aliases, and palette results call the same function. Use `<template>` elements and `DocumentFragment` for keyed DOM updates; batch streaming paints with `requestAnimationFrame`, update text nodes instead of resetting `innerHTML`, and virtualize only after measuring a real long-transcript bottleneck. CSS files define tokens, layout, components, and responsive rules. No CDN assets or build-time Node dependency should be required to launch the app.

The URL should identify the workspace-local session (`/s/<encoded-id>`) so reload and desktop deep links return to the same conversation. Use `pushState`/`popstate`; the server serves the same shell for valid app routes. Browser history changes only navigation, never session history. Downloads use explicit export responses and safe filenames. Preferences such as theme and pane widths can stay in `localStorage`; no prompt, event, approval, bearer, or session cookie is stored there.

## Delivery sequence and gates

1. **Baseline and contracts.** Finish/review the current uncommitted TUI/host work; record the existing Textual journey and protocol version. Document every current chat action against the parity ledger. Add stable web projection IDs/schema, shared view-operation tests, and a workspace summary feed. Update the import-boundary and line-budget gates intentionally: a real web UI will exceed today's 14,000-line `host/ + view/ + ui/` cap, so replace the arbitrary combined cap with separate, reviewed budgets before adding files.
2. **Safe launch and transport.** Implement `nexus web`, owner-only launch-ticket creation, same-origin static serving, cookie/CSRF auth, CSP, browser session snapshot/stream, and clean shutdown. Keep the existing HTTP bearer transport tests passing. Confirm a running daemon can enable web access without restarting or dropping an active turn.
3. **Core browser journey.** Ship session list/new/open, snapshot and live transcript, composer, stop, reconnect, model and agent pickers, approvals, nested agent inspector, Tools/Details, fork, export, palette/help, light/dark themes, and responsive layout. Reach Textual chat parity before adding screenshot-only features.
4. **Utility screens and deferred shared features.** Add the wider CLI host-backed utility screens. Add attachments, path completion, questions, worktrees, and aggregate Git review only through shared host contracts as their core work lands; expose them in both Textual and web. Preserve backwards-compatible event replay or migrate/version older sessions.
5. **Desktop packaging later.** Wrap the same local web assets in a desktop window, swap only launcher/bootstrap/window integrations, use real platform chrome instead of simulated browser traffic lights, and rerun the exact browser parity suite against the packaged app.

### Acceptance tests

- **Cross-client run:** Open one session in Textual and browser. Start a turn in either; both show the same user prompt, streaming answer, tools, child agent, final status, model, usage, queue, and approval outcome, in the same order. Switch/reload one view mid-turn; no duplicate message or lost event. Close both views; the daemon completes the turn and either UI can resume it later.
- **Races:** Resolve an approval from each view in separate runs and race both; exactly one decision is committed. Cancel from one view and observe it in the other. Select model/agent during a running turn; both clients show it as next-turn selection while the active turn retains its effective model/agent.
- **Transport:** Drop SSE, overflow a slow browser buffer, restart the daemon, switch sessions repeatedly, and retry an uncertain send. The client catches up or requests a fresh snapshot; it never silently duplicates a submission. Verify the workspace list updates for changes made in Textual without manual refresh.
- **Security:** A different Origin, missing/expired ticket, missing CSRF token, missing/invalid cookie, cross-site form, path traversal, untrusted Markdown/HTML, and oversized request all fail safely. Existing bearer-authenticated peer HTTP tests still pass. No secret, daemon bearer, launch ticket, raw config, or host file contents leak into page source, logs, URLs after bootstrap, or exports.
- **UI quality:** Render and inspect light/dark screenshots at large, medium, and narrow widths; exercise keyboard-only and screen-reader approval flows; test long responses/diffs, text selection, IME Enter, copy/export, and 200% zoom. Check macOS/Windows/Linux browser fonts and reduced motion. A packaged install opens the web app with no Node toolchain or network asset fetch.

Completion means the browser is a full second view of the same daemon sessions, with verified live handoff in both directions. The eventual desktop app then becomes a packaging and platform-integration step rather than a new runtime or conversation model.
