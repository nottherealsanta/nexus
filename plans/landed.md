# Landed

One line per landed item, newest first. Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

- **Question choices: arrows select, multi-select is clear (11-25).** A single-choice question now follows the
  highlighted option with the arrows (including Left/Right on a lone question, which no longer opens a review page),
  multi-selects draw checkboxes and a ticked count, Enter with nothing ticked ticks the highlight and stays, and NSH
  no longer resets its highlight when an answer changes. The phone card is unchanged. TUI tests 43/43 pass; NSH tests
  updated but not run (no `node`/`nsh` in this checkout). See
  [plans/53](done/53-question-choice-ux.md).

- **Firecrawl from its own release; SearXNG managed by Core (11-24).** `install.sh` downloads pinned `firecrawl-local` 0.1.1
  from the public `nottherealsanta/firecrawl-local-builds` release, verifies that release's `SHA256SUMS`, warns and
  installs Nexus without the tool when the release or platform asset is missing, and stops on a mismatch; Nexus CI no
  longer builds or serves it. `web_search`'s SearXNG starts on request as a container (Apple `container`, else Docker)
  with a pinned image, packaged settings copied into `<home>/search/`, a generated 0600 secret and a loopback-only port;
  Settings, `nexus search status|start` and the TUI onboarding Web search step use it, and `/api/services` reports it.
  The offline installer test passes; the focused search-service tests are 39 passed and one failed (a container published
  on another port is expected to be recreated; the current work-in-progress does not check the published port), and the
  seven search/services API tests pass. **Not verified:** the external `v0.1.1` release (the public repo had no releases on
  11-24), a live container start, and NSH's onboarding search step (not wired yet). See [plans/51](51-firecrawl.md),
  [plans/52](52-onboarding.md).

- **Reopening an inactive project (11-24).** An explicit `POST /api/project` on an inactive project's path restores it to `current` in place, keeping its id, position and sessions; it bumps the revision and publishes `project.updated`, so the TUI and NSH rails show it. Current and passive projects, a repeated POST and incidental `?directory=` lookups change nothing. Core tests cover the event, revision, position, existing session, current/passive/idempotency and directory lookups; the shared reducers already handled `project.updated`, so there is no client change or new screenshot.

- **Tools summary and modal.** Both clients show tool counts and token estimate in the context header; individual switches are in the draft Tools modal and `/tools` ([plan 48](48-context-header-modes.md)).

- **Phone tool-group summaries.** The phone conversation keeps tool groups in order as compact, always-collapsed, non-expandable rows with working/waiting/done status, counts from every member and the diff, and no tool detail; approvals stay usable and desktop is unchanged ([plan 05](05-mobile-and-design.md)).

- **Antigravity ACP defaults to Auto.** Nexus declines remaining permission requests without prompting; `ask` still shows cards; resumed `default` children move to Auto under `allow`. Verified offline ([plan 06](06-zmx.md)).

- **Large-read regression test.** Assert reader and encoder thread placement for all three large history/snapshot responses rather than a scheduler-dependent ticker deadline. Worker and worktree test setup also moved off-loop; 42 focused tests passed ([plan 50](50-rust-core.md)).

- **Core loop stalls from MCP setup.** Load the schema validator and construct transports off-loop; recheck the captured configuration before opening the transport. Worker tests also move database setup off-loop. 165 focused tests passed ([plan 50](50-rust-core.md)).

- **v0.15 artifact dry run passed.** Hosted NSH web packaging, Core packages and both architectures of TUI and Firecrawl on Linux and macOS built. The downloaded NSH archive served its index from the bundled host. Publication remains gated by the hosted Core matrix ([handoff](../reports/release-0.15.0-validation.md)).

- **v0.15 NSH release packaging.** Enable the NSH web artifact job, pin the locally validated NSH 0.4.0 revision, and include the loopback host alongside the web assets. The offline installer test covers installing both; hosted build tracked in the [release handoff](../reports/release-0.15.0-validation.md).

- **Core release validation.** Search and terminal HTTP fixtures run blocking setup before the loop starts; the offline suite blocks live models.dev downloads without changing the 100 ms watchdog ([plan 50](50-rust-core.md)).

- **Firecrawl startup diagnostics.** Core reports the child PID and either its exit code or the last health probe result; the stand-in server no longer depends on reverse DNS to listen. All 16 focused tests pass locally; hosted checks pending ([plan 51](51-firecrawl.md)).

- **External workers rename (11-23).** ZMX is now **External workers** in Core, both clients, Settings and docs: `PTY · Claude Code`, `ACP · Antigravity`, and `CLI · Antigravity` for a legacy stream-json history. Settings options are `Agent SDK`/`PTY` and `off`/`ACP`. `[workers] claude_backend = "cli"` is canonical; `[zmx]`/`"zmx"` and the old API keys remain aliases, and an old-client write stores `cli`. `GET` and `PATCH /api/config` both carry the unavailable reason. A new Antigravity child carries the ACP marker from creation, so a failure before its first prompt is not labelled legacy CLI. Old worker labels are normalized for display (including rows without a provider) without rewriting model context. Tests: Core config/workers/scenarios; TUI and NSH scenario/settings checks; paired Settings captures in `reports/external-workers-rename-2026-11-23/`.

- **First-run onboarding (11-23).** Both clients open a full-window tour once per Nexus home (`ui.onboarded` in Core's config): connect a provider, choose models per agent (scout and quick get a small-model hint), then lessons on the timeline, subagents, approvals and navigation. Replay with `/onboarding`. Steps and wording live in `interface/manifest.toml`; desktop and web only. Core `test_config.py`, TUI `onboarding.rs`, NSH `onboarding.test.tsx`. **Not verified:** a real provider sign-in from the tour, light theme, narrow NSH. See [plan 52](52-onboarding.md).
- **Remote MCP servers: add, sign in, edit, remove (11-23).** `POST/PUT/PATCH/DELETE /api/mcp[/{name}]` and the project twins write
  `[mcp.servers]` (comments kept, secrets masked) or one entry of `.mcp.json` (a file the strict reader refuses is never rewritten; a
  literal credential is refused). A remote entry signs in through OAuth (authorization code + PKCE, the SDK's client; tokens in
  `auth.json`, keyed by scope, name and address; `POST/GET/DELETE …/login`, `mcp.login` event; connections never open a browser). Settings › MCP in
  the TUI and NSH (address only, Core names it; Sign in, header, switch, restart, remove) and `nexus mcp add|list|remove|login|logout|restart`.
  Core tests drive a real streamable-HTTP server behind the SDK's OAuth authorization server (`tests/test_mcp_remote.py`);
  TUI `shell.rs` and NSH `mcp-settings.test.tsx` cover the pages. **Not verified:** a real third-party server (Linear, GitHub…), a
  browser sign-in by hand, paired screenshots, a refresh token that the provider revokes mid-session. See [mcp.md](../docs/mcp.md#managing-servers).

- **Antigravity ACP live E2E and approval-card parity (11-23).** A scratch Core ran an `agy/gemini-3.8-flash-low` child on agy 1.5.14: the real ACP `write_file` request surfaced on the approval API and was denied (file absent), a follow-up resumed the same ACP session in a fresh process and returned `resumed`, and token usage was recorded; TUI/NSH short and long approval/prompt cards matched after the shared three-row value wrap, and the denial card now wraps the full `Needs attention` text in both with `Denied through Nexus` and corrected parent-escalation wording (legacy/Claude keep `Denied by the CLI`). Tests: 194 ZMX Core, TUI 11 scenario tests, targeted NSH ACP denial; the full NSH scenario file still has the known connection-reset flake. Not verified: mobile and a daemon restart. Evidence `/tmp/nexus-zmx-live.anqGQS/results.json`, `reports/acp-approval-parity-2026-11-23/` and `reports/zmx-attention-parity-2026-11-23/`.

- **`firecrawl` tool (11-23).** `firecrawl {action: scrape|map|crawl}` over Firecrawl Local 0.1.1 ([tools.md](../docs/tools.md#firecrawl)): scraping with PDF and Office parsing, site maps and one-call crawls, public addresses only, cloud hand-off off. Shipped with releases: the build workflow builds `firecrawl-local-<os>-<arch>` from the pinned crate, `install.sh` installs it (checked against `SHA256SUMS`; a checkout install builds it), uninstall removes it. `nexus firecrawl init|status` is the fallback; a `nexus doctor` row, both clients' Tools grid and timeline labels. 14 stand-in tests plus a live run of the crates.io build against example.com, arXiv PDF, docs.firecrawl.dev and refused local addresses; not verified: a JavaScript-only page (no Chrome run), Linux, and the paired TUI/NSH screenshots. See [plans/51](51-firecrawl.md).

- **Voice init (11-23).** Quiet dependency installation, download both configured models, report failures and give daemon-restart steps.

- **Installer output (11-23).** Quiet uv's package listing and point users to optional `nexus voice init`; offline installer checks cover the hint and failure handling.

- **Claude Agent SDK images (11-18).** The `claude` provider sends user images and tool-result images as real SDK image
  content blocks (streaming input, one user message), after the JSON history message that names them; the JSON history
  keeps tool calls and results. Verified live with `claude-haiku-5-5`: it named a solid green user image and a solid blue
  tool-result image from neutral file names; 26 Claude tests and ruff pass.

- **TUI system appearance (11-18).** System follows macOS light/dark appearance at launch and during use; other platforms retain the terminal hint fallback.

- **Core off the event loop (11-18, plan 50 A, B, C, E).** `nexus bench` and a synthetic fixture generator (A; the large baseline is still to run). SQLite gets one writer thread and four read-only reader threads; hot read routes and retention run off the loop (B; the turn loop's writes are still on it). API bodies and SSE frames encode with orjson, large ones on a thread; file listing and reads, the models.dev catalog parse and the MCP SDK import run off the loop; the test suite fails any unmarked loop stall of 100 ms or more (C). `@` file search answers from a per-root in-memory index: 4.8 ms warm p95 on 100k files, down from 3.1 s; no Rust module (E). Core 1,660 passed; TUI 1,036 passed; NSH has the same 28 failures as on main.

- **Open bundle (11-17, plan 50 D).** `GET /api/session/{id}/open` (API 1.6) returns the session row, starting context, the newest 200 messages and exactly the turns covering them, so both clients open a session with one request instead of four plus up to nine turn pages; older messages page with their turns (`message?before=&turns=1`); `/context?summary=1` sends only the budget figures. New index `turn_session_seq`. Size and query-count budgets are tests (`tests/test_open_bundle.py`). The stored turn-summary read model is not done (plans/50).

- **TUI timeline selection (11-17).** Mouse release copies and retains a 50% blended highlight until the next click. TUI-only with user approval; NSH native selection does not expose selected text on release.

- **Performance timings (11-17).** Core times every request (`server-timing`, `x-nexus-trace` echo, slow-request log) and watches event-loop stalls, naming the requests in flight; `GET /api/debug/perf` reports it. The TUI logs slow requests (client vs Core time) and every history load (parallel phase vs sequential older turn pages) to `logs/tui-perf.jsonl`; `nexus perf` shows both. NSH is not instrumented yet. Core perf tests and the TUI suite pass; not yet measured on a real slow session open.

- **First-message scrolling (11-17).** Both clients keep a new session at its context header instead of scrolling its first message to the top and adding a spacer. The focused first-message checks pass; NSH's session-switch scroll test and two TUI shell tests fail, and paired screenshots remain open.

- **Assistant timeline Markdown links (11-17, plan 49 partial).** Both desktop clients route web links to the browser and workspace file links to file tabs; other Markdown surfaces and paired verification remain open.

- **Context header: draft, then fixed (11-17, plan 48).** Core `worktree: true` on create and draft (starting notice, workspace tools active, `worktree_request` state); both clients' draft `Worktree` row, a fixed header from the first send (no panel background, no switches, read-only dialogs) and `/skills` / `/mcp` as the way to switch a started session; the Manager uses the same flag. Screenshots open.

- **Continue after successful turns (11-17, plan 37 follow-up).** Both clients offer `/continue` for idle sessions, not only interrupted or failed turns; Core retains its existing validation.

- **NSH pending-turn typecheck (11-16, plan 37 follow-up).** Corrected the fixture network type and nullable event session ID; NSH check and five pending-turn tests pass. Timing measurements remain open.

- **Plan 34 deferral (11-16).** Moved retry-on-connectivity-change to backlog 9.16; the child-card, inspector and parent-wait verification remain open.

- **Timeline left alignment (11-13, plan 35 follow-up).** Both desktop clients align activity headers/members, child cards and notices in one icon/label column, retaining indented output; NSH tool/thought icons shrink to chevron visual size. TUI suite, NSH layout/thought/shared scenarios and web build pass; paired light screenshots inspected on one isolated scripted Core.

- **Composer file search parity (11-13).** NSH uses the TUI's recursive Core search, per-prefix results and padded ranking; both accept an `@` file or folder with Enter without sending. Shared ranking and interaction tests cover both clients; paired screenshots remain unverified.

- **Adaptive orchestration prompts (11-13, plan 46).** Focused scout → optional plan → bounded builds → orchestrator-owned verification; actionable scout/planner/builder handoffs, disjoint edit ownership, scope-growth reporting and honest acceptance. Small known tasks stay direct; no runtime, model config or existing snapshot changes. 60 offline agent/snapshot/child tests, focused lint and diff checks pass; real-task improvement not yet measured.

- **ZMX parent card (11-13, plan 6).** Both clients show the agent name instead of colouring the ZMX vendor label as an agent; the vendor label moves to the metadata line. Shared scenario tested; paired screenshots not captured.

- **Code tool display (11-13).** Both desktop timelines show the cell source instead of its potentially large output; the hidden result remains available in raw view.

- **Browser tools (11-13).** browser-use (pinned 1.0.2) runs two ordinary task tools, `browser_exec` and `browser_screenshot`, granted like `bash` (agents without a `tools` list have them). One process per session, started by its first call; a dead process restarts on the next call with a `browser restarted` notice. Default is browser-use's own Chromium on a temporary profile (`--mcp`); Settings > Browser > Use my Chrome profile (`[browser] use_my_chrome`, TUI and NSH) switches to `--cli-mcp`, read at every call. `browser_login` / `browser_feedback` are not offered; the short-lived `expose_browser_tools` entry point is gone. The browser-use MCP default, the repo `.mcp.json` and `nexus-browser/mcp.json` are removed. TUI and NSH label the rows (`Browser`, `Screenshot`, web glyph). `tests/test_browser.py` (7), TUI and NSH tests pass; real browser-use verified with a real model (codex/gpt-6.1-terra) and in its default own-profile mode. Then: `browser.requirements` checks `uvx` (also beside `uv`, used for the launch) and a browser the way browser-use looks; `GET /api/browser`, a `nexus doctor` row and a Settings > Browser status row (TUI and NSH, tested in both) report it, and a call fails at once with the fix instead of a failed start.

- **System theme (11-13).** TUI exposes and persists System alongside Light and Dark; NSH already offers it. TUI detects the terminal background at startup or selection, with the approved live-detection difference recorded in the ledger.

- **Splash verification follow-up (11-12, plan 26).** TUI splash tests 6/6, NSH splash tests 4/4 and web build pass; an isolated NSH cold start reached the live screen, but neither client's animation was captured or verified.

- **Plan reconciliation (11-12, plans 40 and 41).** Moved the landed Git tab and terminal separation plans to done; paired screenshots remain unverified, and the deferred inspector job lane is tracked in backlog 9.18. Plan 44 remains open for background-job pipeline status and later measurement.

- **Git removed from the right sidebar (11-12, plan 33).** Manifest `git_status` section, TUI drawing/cursor/hit targets/session git fetch and diff sync, NSH `GitGroup`/`GitRows`/`gitStatus` are gone; the left Git tab and project diff stay; Core routes untouched. TUI suite and NSH chrome-polish/i6/git-tab pass; not screenshotted.
- **ZMX live end-to-end check and vendor terms read (11-12, plan 6; user-approved).** orchestrate launched an agy 1.5.6 child and a Claude Code 2.1.322 Haiku child in a scratch workspace; both read a file and answered, and read correctly after a daemon restart. Fixed agy exit grace (3 s to 10 s: agy takes up to 2.2 s to exit after stdin closes). Terms summarised in plans/06-zmx.md, no conclusion. Evidence: reports/zmx-2026-11-12/live/.
- **ZMX offline completion (11-12, plan 6).** Daemon-start-with-ZMX-off test; `/api/state` rows now carry `worker` (a reconnecting client lost the ZMX label and card); docs rewritten (external-workers, config, api, models, auth, providers, daemon, READMEs); D2 recorded in AGENTS.md and the ledger; paired TUI/NSH captures of every ZMX scenario agree ([captures](../reports/zmx-2026-11-12/)). 128 ZMX tests pass. No live run.
- **Shared Rust builds for session workspaces (11-12, plan 36).** Core sets `CARGO_BUILD_BUILD_DIR=~/.cargo/build/nexus-<repo hash>` for bash, background jobs, `!` commands and terminals in a Git workspace, so all worktrees of a repository share compiled dependencies; user build/target dirs and `tools.shared_rust_builds = false` win; `nexus doctor` shows the size. `test_cargo_env.py` plus config/doctor/bash/terminal suites pass; no live cross-worktree cargo run.
- **Git tab summary (11-12, plan 40).** TUI and NSH Git tab shows only the active worktree: `N files changed · +A −D`, group counts, collapsible top-level folders with file rows, `No changes`; branch list removed; Core status adds per-change `added`/`removed`.
- **Alt+Enter interrupt and send, partial (11-12, plan 25).** `mode: "interrupt"` on `POST …/prompt` cancels the turn like Esc and runs the message first, keeping the queue to run after; `composer.interrupt_send` on Alt+Enter in the TUI and NSH desktop/web; NSH `waiting.allow_session` moved to `cmd+shift+enter`. Core, TUI and NSH tests pass; not screenshotted; mobile control still open (needs approval).
- **Manager indicator states (11-12, plan 28).** "Voice on" is gone: waves only while connected, a still quiet wave when disconnected, a still flat line when muted, the indicator undimmed under the open panel, colour by speaker (TUI and NSH). Offline tests pass; no screenshots yet.
- **Manager voice buttons (11-12, plan 28).** Connect / Disconnect and Mute / Unmute replace the Voice on/off toggle in the TUI, NSH dropdown and phone Manager tab; the phone tab has the braille wave. Offline tests pass; no device or real-audio check.
- **GPT-Live native PCM framing, partial (11-12, plan 5).** Added tested 24 kHz mono conversion and 20 ms framing; native audio, transport and end-to-end voice are still unimplemented.
- **GPT-Live native transport, partial (11-12, plan 5).** Added a bounded PCM WebSocket with loopback tests; it is not yet connected to iPhone audio or the Manager.

- **Mobile session list, partial (11-12, plan 5.2).** Grouped waiting, running and recent sessions, with waiting/running indicators and corrected all-projects filtering; five mobile rule tests pass. Device verification and the rest of the redesign remain open.
- **Mobile session drawer, partial (11-12, plan 5.2).** Header opens a reusable Sessions drawer, session selection closes it, and All sessions returns to the tab; phone-idiom route test passes. Edge swipe and device verification remain open.
- **Mobile device smoke check (11-12, plan 5.2).** Signed and installed the partial redesign over the existing iPhone app; it launched and displayed live sessions grouped under Running and Recent. Other tabs, the drawer and touch interactions remain unverified on-device.
- **Mobile visual pass, partial (11-12, plan 5.2).** Refined session list, navigation typography and compact approval choice layout; six phone-idiom tests pass and the signed build launched on iPhone. Approval and populated-list visuals still need device checks.
- **Mobile Settings and Connect, partial (11-12, plan 5.2).** Added a connection-status card, grouped section drill-ins and provider detail host, and clarified manual connection setup; phone-idiom navigation tests pass. Pairing and connection telemetry remain open.
- **Mobile keyboard and conversation, partial (11-12, plan 5).** Reserve the keyboard inset, offer a direct back action and keyboard dismiss, preserve drafts on return, and refine the user-message card and composer; phone-route tests pass. Physical keyboard verification remains open.

- **NSH look audit and first component pass (11-11, plan 05 5.3).** Audit of NSH web 1024/1440 dark/light beside the TUI; fixed the approval table (wrapped label, key order now matches the TUI), Settings label column, `Btn` hairline and `Seg` spacing. Open items and macOS capture listed in the report. [Evidence](../reports/nsh-look-2026-11/).

- **`/btw` modal parity (11-11, plan 42).** NSH now matches the TUI: the `[i]`/`[c]` lines are the tappable folds (button rows removed) and desktop/web gets the `↑↓ scroll · esc close` footer (not on the phone idiom). `session-status-btw` 4/4; paired captures in [reports/42-btw-parity](../reports/42-btw-parity/).
- **Mobile redesign (11-11, plan 5.1/5.2).** Phone client: Sessions sections (Needs you / Running / Recent), project switcher sheet that also sets the new-session project, long-press row menu (rename, archive), pull to refresh, session drawer (header button and left-edge swipe on `EdgeDrawer`), compact conversation header with status ring and overflow menu, approval card (Allow once / Always allow / tell it instead + Deny), Settings connection card (host, address, LAN/Tailscale, live latency, trace) with a grouped drill-in list, and a numbered Connect/pairing screen with recent Cores and a Local Network error + Retry. Core: `nexus pair` (address, token, link, terminal QR; `segno`). Tests: `phone-redesign.test.tsx` 9, `mobile.test.ts` 11, `tests/test_pair.py` 6. Simulator, signed device build and physical iPhone remain open. [Screenshots](../reports/mobile-2026-11-11/).

- **Core backlog pass (11-11, plan 9).** Removed the `stream_summary`/`turn_end` recovery fallbacks; `SessionEnd` also fires on archive; hook matcher and denied-approval tests; user-tool `MEMORY_MB`; `[tools] disabled` accepts project-only names without dropping the rest; read-only MCP resources and prompts API; per-section `details` in `/api/draft/context`; worktree review and keyed integrate (Git merge, optional remove) as control-plane API; recovery probes surviving background jobs without killing; export clips the event log with `events_clipped`; inspector paging tests under writes and retention. Ticks with evidence in 9.2, 9.3, 9.5, 9.13, 9.19, 9.20. Client UI for these stays open.

- **Backlog audit notes (11-11, plan 09).** NSH handoff open-original test, project-rail boundary tests, `nsh-syntaxes.bin` served, split-diff and scroll coverage located; see 9.2, 9.5, 9.7, 9.11.

- **Ctrl+R prompt history search (11-11, plan 09 9.7).** Shared `composer.history_search` in both clients; Ctrl+R no longer reconnects in the TUI. TUI shell test and NSH `prompt-history.test.tsx` pass; paired captures in [reports](../reports/ui-backlog-2026-11-11/). Dump-frame runs now load prompt history for `--open first`.

- **Shared motion durations (11-11, plan 09 9.6).** `[motion.durations]` in theme.toml (flash, scroll bar visible/fade) drives both clients; NSH's 600 ms flash and 900/1300 ms scroll-bar timing now match the TUI. Draw-cost bench re-run: paced 1.4–1.5 ms, chrome 0.38 ms, so the 11-04 regression does not reproduce.

- **TUI Git tab at the default sidebar (11-11, plan 09 9.2).** Tab strip widths follow labels, so `[Git]` shows at 28 cells; click test at 28 and 40. [Captures](../reports/ui-backlog-2026-11-11/).

- **Claude Code install note on Providers (11-11, plan 09 9.9).** Core's `claude` provider row carries `note` (required version 2.1.248, or not installed); both provider-detail views show it. Core 15/15, TUI shell test, NSH Providers test pass; NSH README session-row height corrected.

- **Plans 01–23 audit (11-11).** 1, 2, 3, 21 and 23 moved to done/: their remaining boxes need hardware, a paid call or branch-deletion approval. Retook the 21/23 paired captures from current code (TUI 170×50, NSH 1440×900 and 390×844); status strips match, and the `/btw` modal divergence is now plan 42. ZMX isolation test fixed (77/77). 5, 6, 9 and 10 stay open with real offline work. [Captures](../reports/sections21-23/).

- **Search startup with an existing Apple container (11-11).** Recheck for the container after starting its system and serialize concurrent Core start requests; regression tests cover both cases.

- **Stop background shells from the Shells modal (11-11, plan 41).** Both clients: ↑/↓ select, `s` or `[stop]`/**Stop** kills a running job; TUI `resources` 7/7, NSH `resources.test.tsx` 5/5, both modals compared.
- **Terminal is the user's (11-11, plan 41).** The dock holds only user terminals; `bash` (foreground and background) is pipe-only and never creates a terminal resource; `N shell` counts every running job in both clients and opens the jobs list; terminal evidence provenance, inspector `terminal` lane/`resource_spans` and `terminal.updated` removed; old job-linked terminals load as `closed`. The inspector `job` lane and paired screenshots remain open.

- **Pending queue selected/held parity (11-11; plan 31).** Both clients expose selected-row keyboard actions and a held-only Send next control plus `n` hint, with question locking; focused tests and paired held/selected captures pass. Physical touch unverified. [Evidence](../reports/session-controls-2026-11-11/).

- **Session-control geometry and parity follow-up (11-11).** Removed the unheld NSH queue header and restored the copy button's 32px kit geometry; real-browser center clicks copy a 1,156-character worktree path and the main checkout after scrolling. Plans 29, 30 and 32 moved to done; plan 31 retains selected-row hint/held-control parity checks. Physical phone unverified. [Evidence](../reports/session-controls-2026-11-11/).

- **Session controls evidence follow-up (11-11; plans 29–32).** Matched queue, question and worktree component states for reviewed captures; added a real-browser long-path copy reproducer. Keyboard and edge pointer copying are exact, but center pointer fails on overflow; NSH's extra queue header also differs from TUI. Both remain open. [Evidence](../reports/session-controls-2026-11-11/).

- **Session controls follow-up (11-11; plans 29–32).** Fixed NSH pending-row rendering and unhovered disabled-icon leakage; desktop focused suites, phone-idiom routes, Core contracts and interface checks pass. Physical device, controlled paired captures and exact long-path copy remain open; full TUI suite is blocked by an unrelated private-method compilation error. [Evidence](../reports/session-controls-2026-11-11/).

- **Subscription usage sources (11-11).** Core parses Codex personal/managed monthly limits and optional workspace lookups; Claude uses OAuth usage or a guarded interactive `/usage` fallback, including weekly-only and enterprise spend-only accounts. Both clients retain their shared table contract. Approved read-only current-account checks and 56 focused tests passed; Ruff passed. Full Core run timed out; other plans and screenshots remain unverified. [Usage contract](../docs/api.md#usage-k11).

- **Session controls, partial (11-11, working tree; plans 29–32).** Shared `/archive`, `/search [text]`, linked-worktree
  command/glyph/scenario and docs are in place; Core archive/input contracts pass 28/28. TUI implements all four areas;
  its final dedicated suite passes 34/34. The latest NSH session-controls run passed 12/16: archive/search, questions,
  archive refusal, failed-answer retry and basic worktree modal/copy passed; pending-row visibility and long-path
  scrolling remain failing in NSH. NSH build passes. TUI screenshots and basic NSH question/worktree captures are in
  [the evidence folder](../reports/session-controls-2026-11-11/); NSH
  queued-row screenshots/mobile checks and final paired screenshot review remain open. No completion claim for plans
  29–32 yet.

- **Shared Rust build folder for this repo (11-11, working tree).** Repo-root `.cargo/config.toml` sets
  `build.build-dir = "{cargo-cache-home}/build/nexus"`: intermediates shared by every checkout, worktree and crate,
  final binaries stay in each `target/`. Verified: full `nexus build` passed (3:54 cold, 3.2 GB shared folder incl.
  NSH wasm); a fresh worktree's release TUI build took 22 s, recompiling only the four path crates. Other projects'
  worktrees: [36](36-shared-rust-builds.md).

- **Terminal: local tokenless access and Ctrl+` toggle (11-11, working tree).** Core's terminal routes refused every
  tokenless request (the default wildcard bind), so the local TUI got `HTTP 403: terminal access requires
  authentication`. A tokenless daemon now serves terminal routes only to a loopback connection peer with a loopback
  Host/Origin and no proxy-forwarding headers, checked before the idempotency journal; remote tokenless access stays
  refused. Ctrl+` and Ctrl+Shift+` (Ctrl+~) toggles the dock in both clients, including with the screen focused,
  never reaches the shell, and closing detaches. Core terminal HTTP 19 passed, real-Core TUI PTY test passed, NSH
  terminal-panel 2/2 and xterm page test passed, manifest/theme checks passed; manual tokenless local create 201,
  forwarded/foreign-origin 403. Unrelated failures not fixed: TUI `manifest::settings_order_is_fixed` and 5 `shell`
  tests. Paired screenshots in `reports/terminal-toggle-2026-11-11/`; native macOS WebView routing unverified.

- **Session heading, root back, marquee and gutters (11-11, working tree).** Both clients keep one heading
  row under the tabs: a root session leaves it blank (no duplicate tab title), while a viewed subagent shows
  its title on the left and right-aligned agent/model/provider/effort metadata, shifted right by a
  full-height muted-grey back strip at the page's far left: four cells (4 text cells in NSH) wide,
  spanning the heading through the composer, with the arrow centred both ways and the whole strip
  clickable. The old second subagent-tab row is gone; child cards remain the navigation path. A root
  session has no strip. The strip, and Left outside the composer, text fields, the editor and overlays,
  jump straight to the root; Esc still returns to the immediate parent.
  `nav.root` is a dedicated manifest command (`timeline` context, Left), so Esc's parent semantics are
  unchanged. Tab titles wider than their tab scroll on hover inside their own clipped title region,
  leaving the status mark and fixed close slot untouched (reduced motion leaves them still with a
  tooltip and accessible name; NSH also skips hover motion on touch). The timeline uses a 12 px outer
  gutter (1 TUI cell) with card/text insets unchanged, and Shift+Tab cycles eligible agents via the picker's
  next-call switch/cache warning; draft agent, model and effort selections update silently. The session footer
  pins New session / Search sessions / Show archived (also when empty), and the rail pins its add control and
  Settings at its foot. Checks: TUI shell 172 passed/1 ignored with one unrelated ZMX settings-order failure;
  the root-no-rail, four-cell rail, centred arrow, composer geometry, dock no-overlap, metadata balance and
  tab-marquee clipping tests pass. Timeline 71/71; alerts 11 passed/1 ignored.
  Parent-run lib 394 passed/4 ignored with the same unrelated ZMX settings-order failure, and behaviour 80
  passed/2 ignored with one unrelated `session.linked_worktree` failure; full `cargo test` remains blocked by
  the pre-existing `tests/scenarios.rs` private `open_palette` E0624. NSH child-navigation 7/7 (single
  heading row, no strip, four-cell gutter, rail/Left to root, composer cursor preserved), fork-subagent 6/6,
  header-marquee 4/4 and timeline-layout 10/10 pass. At final check, `npm run check` was blocked by unrelated
  concurrent `composer.tsx` errors (`TS` missing and `Button` `onMouseEnter`). Latest
  captures: [TUI and NSH](../reports/session-title-heading-2026-11-11/); the NSH child uses the shared mock
  scenario, so its timeline content differs from the TUI's scripted Core.

- **Scout tool budget (11-11).** Scout turns are capped at 50 tool calls, including nested calls; exhaustion requests a partial report.

- **Harness workflow hardening (11-10, working tree).** Session-private kernels with
  fresh-checkout heap restoration, common nested/direct policy, fork call/time/effect
  boundaries, bounded foreground shell and corrected workflow prompts. Expanded
  integration 221 passed; approved Copilot GPT-6 Luna direct/code/fork smoke passed.
  [Evidence, limits and final gate](../reports/harness-audit-2026-11-10.md).

- **Isolated ZMX transport groundwork (11-10, working tree).** Added `nexus/workers/` with agy stream-json
  transport, shared safety helpers and fake-CLI tests based on `nexus01`; no production caller, provider/config
  registration or UI changes. Baseline Core: 1310 passed/1 skipped; final Core: 1387 passed/1 skipped;
  focused worker suite: 77 passed; full Ruff and `git diff --check` passed.
  M0 live checks and M1 integration are not complete. [Scope and limits](../docs/external-workers.md).

- **Daemon version and plan reconciliation (11-10, working tree).** Daemon identity now uses installed package metadata;
  regression reproduced before the fix, then server/doctor/event-log suites passed 93/93 and focused Ruff passed.
  Summary rows now match sections 10 and 21–23; release-time checks are distinguished from current-tree and platform gates.

- **Fork subagents complete (11-09).** Core and both clients implement inherited context, parent tool rows,
  expandable inherited timelines, report delivery, persistence, recursion/context guards, read-only
  enforcement and a 20-step budget. Prompt-cache reuse was verified on Codex, Copilot, OpenCode and Grok
  ([report](../reports/fork-cache/README.md)); Claude was not checked. Focused rerun: Core 9/9 and TUI 7/7
  pass. NSH has implementation and tests and the recorded release-wide suite passed 371/371; the latest
  focused rerun was blocked by the runner not finding Chrome/Chromium. Screenshot review remains above.

- **Search startup diagnosis (11-11).** Detect an existing service rejecting JSON searches and explain how to restore it or free the port instead of attempting a conflicting launch.

- **0.12.0 released (11-09).** PR #100 merged, release branch removed, no open PRs remain. Four platform binaries, wheel/sdist, installer and checksums published; every payload hash verified. Isolated published macOS arm64 install, daemon and plain TUI smoke passed; Linux installer/runtime and native/device gates remain explicit. [Release](https://github.com/nottherealsanta/nexus/releases/tag/v0.12.0).

- **Automated test gate complete (11-09).** Core 1290 passed/1 skipped; TUI 861 passed/10 ignored plus real-Core PTY smoke; NSH final full web run 371/371. Lints, builds, native/shared/installer checks and existing macOS/Linux PR checks pass. Transient inspector-host failure and remaining UI/runtime gates stay explicit in [the validation report](../reports/release-0.12.0-validation.md). Grok schema backlog item removed at the user's request; no provider fix is claimed.

- **Terminal implementation and safety (11-08).** Paired real-PTY docks hardened for exit, creation races and read-only reconnect; bounded diagnostic parsing, immediate slow-writer revocation, restart-safe control, explicit selection → preview → draft evidence, and exact-tool-linked inspector lifecycle bars/invalidation. Real-Core TUI/NSH smoke and paired captures verified; platform/native/remote and consolidated-suite gates remain open. [Report](../reports/terminal-work-2026-11-08.md).

- **Core network default (11-08).** Core now binds `0.0.0.0` by default for LAN/Tailscale clients; explicit host overrides remain. Native discovery accepts the empty token written for network binds. The default is tokenless: trusted networks/firewall only; loopback opt-in retains token protection. Focused checks: Core daemon 7 passed, native discovery 4 passed, Ruff passed; device connectivity not verified.

- **Consolidation checkpoint (11-08).** Current work collected on main at the user's request; incomplete editor/Git UI and verification gates remain explicitly open. No new feature work continues after this checkpoint.

- 2026-11-08: Fixed a stale file-editor area call that prevented the TUI launcher from compiling.

- **Files action dialogs and native capture build (11-07).** Both clients implement create/rename/delete prompts and counted permanent-delete confirmation; matching new-file/delete screenshots compared in `reports/files-operations/`. Apple microphone bridge builds for macOS and iOS; real device/audio verification remains open.

- **Claude version diagnostics (11-07).** SDK wire requests record the installed CLI version (unknown when unavailable); cached off-loop lookup invalidates on executable changes. Offline adapter suite: 24 passed.

- **Launch and release cleanup (11-07).** Removed the obsolete design mockup, documented and checked Linux source prerequisites, tested installer daemon restart and SDK source build, skipped image queries in known text terminals, and verified state-table job recovery after deleting diagnostic events. Branch history reviewed; pruning awaits approval after automatic review rejection.

- **Conservative edit matching and multi-edit (11-07).** Exact-first typography/whitespace/CRLF tiers and ordered atomic `edits[]`; ambiguity and later failures preserve the file. All 16 tool tests pass.

- **Step budget notice (11-07).** A visible notice at about 80% leaves tools enabled and asks the agent to finish and verify. Scout defaults to low effort, matching Quick. Loop and missing-install regression tests pass.

- **NSH terminal target retired (11-07).** Removed its launcher, npm script, cell-only rendering/key branches, terminal parity test and archived captures; maintained Rust TUI and web/native paths remain. The archived tag preserves the retired target. NSH type check passes.
- **Terminal cursor modes and archive guard (11-07).** TUI arrows/Home/End honor application cursor mode; Core rejects archiving trees with running background jobs. Targeted PTY, API and archive tests pass.

Records before 2026-11-06 are in [docs/archive/PLAN-2026-11-06.md](../docs/archive/PLAN-2026-11-06.md#landed).

- **Core terminal resource slice.** PTY service, bounded replay and authenticated HTTP/SSE routes; real-PTY and HTTP tests. UI panels and cross-platform orphan cleanup verification remain open.

- **NSH control icons (11-06).** Restored bundled theme-tinted SVGs in the shared
  desktop/web Icon component, sidebar tabs and Settings row, with distinct sidebar
  open/closed icons and unchanged status semantics. Rounded surfaces, focus and
  pressed states and a no-blur Manager consent surface are an approved NSH visual
  difference. Web before/after and a seeded TUI frame were checked; AppKit and GPUI
  visual rendering still need verification. The GPUI bundle includes the assets.

- **NSH web archiver discovery (11-06).** Web build, test and development commands
  find `llvm-ar` in the active Rust toolchain when it is absent from PATH, preserve
  explicit archiver settings and explain how to install missing LLVM tools.
  Verified: full `scripts/nexus build` (Core, release TUI, packaged web) and all
  11 NSH host tests, including five archiver regression tests.
  Daemon investigation: the running daemon answers; an isolated startup takes
  0.57 s and all seven lifecycle tests pass. The reported timeout was not reproduced.

- **Provider usage table (11-06).** Core, TUI and NSH show the shared gauge, left, reserve and expiry table; fixture-backed desktop and phone comparisons are in `reports/provider-usage-table/`, including scrollable lower rows.

- **Modal backdrop back to translucent (11-06).** Reverted `0452ee80`: the TUI blends the page 50% toward
  `surface.canvas` (`paint::scrim`) under every modal again, NSH is back to its translucent `bg-black/65` backdrop and
  its own `Dialog`, the terminal-only modal block is gone, and the ledger and `interface/design-language.md` say so.
  Verified: paint unit tests, manifest and theme checks; the TUI suite shows the same failures as before. Not
  re-captured in a terminal screenshot.

- **Timeline notice rows (11-11).** Core attaches durable `notices` metadata to a finished `wait: false` subagent while the model text stays exactly as received; older notices parse the child id/agent/outcome and fill title/model/duration from the child session. Both desktop clients draw single thoughts as `◇` and one finished background shell as `$`, and show `⣿ Subagent done · Agent · title` with a `Subagent done` modal (agent, title, model, duration, exact text) whose footer opens the child. Mobile is unchanged. Core notice tests, TUI scenario/frame tests, NSH scenario/thought tests and paired dark/light captures in `reports/35-timeline-notice-rows/`; see [done/35](done/35-timeline-notice-rows.md) for the honest limits.
- 2026-11-12: phone design pass (Google Sans UI font, rounded surfaces, calmer composer); see plans/05 5.2.
- **Session review: tooling and prompts (11-12, plan 44).** From two days of real sessions: unexpected turn failures close their tool calls (no more permanent HTTP 400), child failure reports carry the provider's reason and whether resuming helps, foreground `bash` reports failures hidden behind pipes, missing paths name likely ones, failed `code` cells keep completed outputs in `last_outputs`, `grep`/`glob`/`ls` read outside the workspace like `read`, `web_fetch` reads JSON, no-op multi-edit entries are skipped, clearer `bash`/`code`/`web_search` texts, NSH test command in AGENTS.md. Focused tests per change; background-job pipeline status remains open; see [44](44-session-review-tooling.md) for measurement queries.

- **Launch splash without progress bar (11-12, plan 26).** Both clients drop the milestone bar, percentage and step name; the logo animates from the first frame (ambient field, intro particles, light sweep) and a quiet `Starting Nexus Core…` line shows after 5 s. Milestones kept internally for `ready`. TUI and NSH splash tests pass; cold-start capture not verified, see [26](26-launch-splash.md).
- **Ctrl+V pastes text (11-12).** TUI Ctrl+V reads image, else text (`pbpaste`/`wl-paste`/`xclip`, 1 MiB) through the bracketed-paste path, else toasts "The clipboard is empty"; NSH binds Ctrl+V on Apple platforms via `composer.paste`. Verified: TUI and NSH tests, manifest check; not run in real Ghostty.
- **Shared UI strings table (11-12, plan 40).** `interface/manifest.toml` gains `[strings]` (`"key" = "text"`, `{name}` placeholders); TUI `Manifest::ui_text`, NSH `uiText` (generated). The Git tab labels moved first; `check_manifest.py` validates keys, placeholders and use. TUI `cargo test`, `git-tab.test.tsx` and the manifest check pass; no screenshots.
- **Phone Send long-press and iPad as desktop (11-12, plans 25, 05).** On the phone, long-pressing Send while a turn runs opens a sheet with Queue / Interrupt and send (`mode: "interrupt"`); only the phone idiom is the cut-down client, an iPad gets the desktop/web layout. Tests: `phone-redesign`, `mobile`.
- 47 Questions live in the composer slot (11-14): TUI + NSH built, tests updated; screenshots compared in the plan file.
- 47 done (11-15): NSH Esc/Up and image-draft tests; TUI/NSH compared (`reports/47-question-in-composer/`); NSH rail joined through the line; caret and highlight no longer reset on model events; 30.2/30.3 marked superseded.

- **Light-theme selection contrast and a safe turn time (11-15, plan 05).** Light `interactive.selection` deepens to a
  warm sand (`#e8d0b9`) and the Settings focus band to neutral `#d9d9d4`, so selected and focused states stand off the
  well in both clients (NSH before/after and TUI light frames in `reports/nsh-look-2026-11/`). NSH's turn meta omits a
  missing or unparsable timestamp instead of printing `Invalid Date`, matching the TUI; `timeline-visual-parity.test.tsx`
  covers it. Theme checks, NSH web tests and type check pass.

- **Plan 10 visual check (2026-11-16).** The blank TUI fork child was a `--dump-frame` timing bug (waited for a snapshot, not the session history), not a decode error; decoding hardened and failures reported; NSH inherited block gets the TUI's rail; paired dark/light captures in `reports/10-fork/`. Observation item stays open.
- **Plan 26 verified (2026-11-16).** Cold start recorded in both clients with a delayed scratch Core; sweep cost measured (~6% of one core while waiting, no dropped frames). `reports/26-launch-splash/`.
- **Plan 44 background-job pipeline status (2026-11-16).** Background `bash` jobs record the last pipeline's `PIPESTATUS` through the same `EXIT` trap as the foreground tool; a clean exit that hides a failing stage reads `exit code 0 · pipeline exit codes 1 0` in `bash_output`, the finish notice and the job JSON (`hidden_failures`). Killed/timed-out jobs get no note; the status file is always removed.
- **Plan 45 closed (2026-11-16).** Superseded manifest items removed (`[navigation.panel]`, `strings_pending`), `terminal.close` / `terminal.focus_list` reworded, leader arrows and Alt-drag / Esc / Ctrl+click verified headless, eight paired captures in `reports/45-tabs-and-splits/`. Real-Ghostty drag stays on the user checklist.
- **Plan 39 landed (2026-11-16).** A linked worktree header shows only when two or more root sessions of it are listed (archived filter and scope applied, recounted on change), in the TUI and NSH (NSH gained the header and the TUI order: main checkout, then worktrees by path). Captures in `reports/39-worktree-header/`. Also: the NSH project MCP panel is mounted in the new-session view again.
- **Plan 37 landed in part (2026-11-16).** Pending working row, phases, queue-while-disconnected, refusal strip, Retry after 10 s in both clients and mobile; `/prompt` shares it; Core existence check. Timing runs still to record. `reports/37-continue-feedback/`.
- **0.13.0 released (2026-11-16)** as is, at the user's request: `v0.13.0` published, assets verified; NSH full web suite stopped, plans 34/37 open items carried over ([validation](../reports/release-0.13.0-validation.md)).
# 2026-11-24: Neutral iOS redesign

The phone client follows the approved neutral redesign ([plans/05 5.5](05-mobile-and-design.md)): no orange accent,
phone-card surfaces, a floating composer with neutral pills, three-pill approvals with the note in the composer, quick
Allow once from Sessions, a waiting badge and pill, a back swipe instead of the drawer, and a Manager tab that starts
nothing on open. Subagents show as cards in the parent timeline that open the child, with Back to the parent and a
count on the Sessions row. Installed on the iPhone.

# 2026-11-23: Phone conversation navigation

A phone conversation now hides the root tab bar and starts its header with an icon-only back chevron (accessible name "Back to Sessions"); Back keeps the session and its draft, and a tap on its row reopens it. The session drawer and its left-edge swipe are unchanged. The header shows the project and live `Working · Ns` beneath the title, with no phone bottom Working row or activity bar. The composer is two compact rows: a full-width input above agent/model selectors and mic/send. Phone prose and user messages use Google Sans at 16/23 and 16/22 while code keeps Monospace. A phone user message now shows only its text; a long press reveals its time, turn number, Copy and Fold controls, and the phone no longer draws the sticky user bar. A synchronous per-draft send guard drops repeated in-flight taps, preserves the draft on failure, and releases for later sends; it does not claim to fix the unproven freeze cause. Headless phone and desktop composer/timeline tests and type checks pass; 390 px screenshots checked, physical-device verification remains open (plan 05).

# 2026-11-24: `nexus update` installs

`nexus update` now updates instead of only reporting. It reads the PEP 610 `direct_url.json` of the `nexus10` distribution: a
release wheel hands off (via `exec`, so the old `uv` process is gone) to that tag's `install.sh`, downloaded from the hard-coded
GitHub release URL and verified against the tag's `SHA256SUMS` before execution; the installer then verifies every payload and
restarts a running daemon. A checkout install refuses a detached HEAD, missing upstream, in-progress merge/rebase, local changes
including untracked files, and local commits, then fetches, fast-forwards only, and runs the checkout's `install.sh --source`.
Installs with no usable install metadata, a VCS source or a vanished directory fail closed with reinstall instructions. A prompt
names what will change and the daemon restart; without a terminal `--yes` is required. `--check` and `--json` remain report-only
with the same JSON shape. Verified offline: mocked release downloads/checksums, real temporary Git clones for fast-forward,
dirty/diverged/detached refusals, CLI flag wiring, `ruff`, focused and full Core tests, `sh -n install.sh`,
`sh scripts/test-install.sh` (now including a tampered wheel that must not install) and `bash -n scripts/nexus`. No real update
or home migration was run.

