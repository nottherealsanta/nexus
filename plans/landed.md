# Landed

One line per landed item, newest first. Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

- **Shared Rust builds for session workspaces (11-12, plan 36).** Core sets `CARGO_BUILD_BUILD_DIR=~/.cargo/build/nexus-<repo hash>` for bash, background jobs, `!` commands and terminals in a Git workspace, so all worktrees of a repository share compiled dependencies; user build/target dirs and `tools.shared_rust_builds = false` win; `nexus doctor` shows the size. `test_cargo_env.py` plus config/doctor/bash/terminal suites pass; no live cross-worktree cargo run.
- **Git tab summary (11-12, plan 40).** TUI and NSH Git tab shows only the active worktree: `N files changed · +A −D`, group counts, collapsible top-level folders with file rows, `No changes`; branch list removed; Core status adds per-change `added`/`removed`.
- **Alt+Enter interrupt and send, partial (11-12, plan 25).** `mode: "interrupt"` on `POST …/prompt` cancels the turn like Esc and runs the message first, keeping the queue to run after; `composer.interrupt_send` on Alt+Enter in the TUI and NSH desktop/web; NSH `waiting.allow_session` moved to `cmd+shift+enter`. Core, TUI and NSH tests pass; not screenshotted; mobile control still open (needs approval).
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

- **Launch splash without progress bar (11-12, plan 26).** Both clients drop the milestone bar, percentage and step name; the logo animates from the first frame (ambient field, intro particles, light sweep) and a quiet `Starting Nexus Core…` line shows after 5 s. Milestones kept internally for `ready`. TUI and NSH splash tests pass; cold-start capture not verified, see [26](26-launch-splash.md).
- **Ctrl+V pastes text (11-12).** TUI Ctrl+V reads image, else text (`pbpaste`/`wl-paste`/`xclip`, 1 MiB) through the bracketed-paste path, else toasts "The clipboard is empty"; NSH binds Ctrl+V on Apple platforms via `composer.paste`. Verified: TUI and NSH tests, manifest check; not run in real Ghostty.
