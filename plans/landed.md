# Landed

One line per landed item, newest first. Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

- **NSH look audit and first component pass (11-11, plan 05 5.3).** Audit of NSH web 1024/1440 dark/light beside the TUI; fixed the approval table (wrapped label, key order now matches the TUI), Settings label column, `Btn` hairline and `Seg` spacing. Open items and macOS capture listed in the report. [Evidence](../reports/nsh-look-2026-11/).

- **`/btw` modal parity (11-11, plan 42).** NSH now matches the TUI: the `[i]`/`[c]` lines are the tappable folds (button rows removed) and desktop/web gets the `↑↓ scroll · esc close` footer (not on the phone idiom). `session-status-btw` 4/4; paired captures in [reports/42-btw-parity](../reports/42-btw-parity/).

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
