# Nexus plan

Rewritten 2026-11-06. The previous plan (1,860 lines, every landed record and every old checklist) is kept whole in
[docs/archive/PLAN-2026-11-06.md](docs/archive/PLAN-2026-11-06.md). **Section numbers quoted by older docs and code
comments ("PLAN.md 8.2", "14.1") refer to that file.** Current behavior is in [docs/README.md](docs/README.md); deliberate
changes from old Nexus are in the [porting ledger](docs/porting-ledger.md).

Rules every item keeps:

- Preserve the visible, ordered context timeline: everything the model receives is shown in order, the UI shows nothing
  the model does not have, wire events are delivered live, deletion and clipping are announced. Exceptions: the Manager's
  own context ([docs/manager.md](docs/manager.md)) and, once built, ZMX children ([ZMX_plan.md](ZMX_plan.md) section 4.3).
- **The TUI (Rust) and NSH (web/desktop) are separate, maintained peers** with a similar design and the same functions.
  The TUI is the reference and NSH always mimics every TUI function; a UI change lands in both in the same change
  ([.agents/skills/nexus-ui-parity/SKILL.md](.agents/skills/nexus-ui-parity/SKILL.md)). A difference needs the user's
  approval and a ledger row. (Decided 2026-11-06: the plan to retire `nexus-tui` for the NSH terminal backend is dropped.)
- **Mobile is a cut-down client** (section 5). Desktop surfaces never ship there.
- Task tools act on the workspace; changes to Nexus state belong to the control plane.
- State is in `~/.nexus` after `nexus migrate`. Never run it against the real home from an agent session; tests use a
  scratch `$NEXUS10_HOME`. Real-provider calls and anything that spends money need the user's approval first.
- CI is deferred (user, 2026-11-04). Do not work on it unless asked.

When an item lands, tick it, add a one-line record under [Landed](#landed) and update the docs it touches. Keep
"not verified" honest.

## Status at a glance

| # | Work | State | First step |
| --- | --- | --- | --- |
| 4 | Tests and lints | **Core and TUI green (816 TUI tests pass; fmt, clippy, ruff clean). NSH: 2 failing tests left** | [4](#4-fix-all-tests-and-lints), do this first |
| 1 | Unmerged work | Local `main` is **34 commits ahead of `origin/main`, unpushed**; PR #100 open; PR #99 looks obsolete | [1](#1-merge-what-is-unmerged) |
| 5 | Mobile cut-down client | Not started; phone build still renders the full timeline. Decided: mobile can approve and answer anything a client can | [5](#5-mobile-cut-down-client-sessions-manager-settings) |
| 6 | ZMX mode (Claude Code, Antigravity) | **Later (user, 11-06).** Plan only, nothing built; kept here for reference | [6](#6-zmx-mode-for-claude-code-and-antigravity-later) |
| 3 | Manager, finished and tested end to end | M1–M5 built; real GPT-Live call worked 11-04; evals, long calls, iOS/native audio and hands-free open | [3](#3-manager-finish-and-test-end-to-end) |
| 2 | Terminal inside Nexus | Spikes done (`reports/section14/spikes.md`); no product code | [2](#2-terminal-inside-nexus) |
| 7 | Everything else carried over | Full backlog in section 9, not scheduled | [7](#7-carried-over-not-scheduled) |
| 8 | Provider usage panel (table) | Landed and visually compared in both clients | [8](#8-provider-usage-panel-usage-table) |

Suggested order: 4, then 1 (so the release PR can merge on a green tree), then 5, then 2 and 3 in whatever order the
user prefers (3 needs real-model approvals). Section 6 (ZMX) is for later. Section 8 (the usage panel) is small, self-contained and can go any time after 4.

## Decisions

Made (user):

| Date | Decision |
| --- | --- |
| 2026-11-04 | Manager is a top-bar dropdown; Settings value rows are segmented; only a session's first message pins to the top; pending rows are `S` / `Q`; finished sessions' events are compacted with a `session.compacted` record; CI deferred |
| 2026-11-05 | Manager header indicator is a gray braille wave field; Stop on hover while Live; an open Manager panel dims the rest |
| 2026-11-06 | Project remove (unregister) and delete (permanent) both stay. Composer voice/send buttons are an approved NSH-only difference. Scroll bar and segmented Settings are shared |
| 2026-11-06 | **NSH terminal backend dropped.** `nexus-tui` stays; `nexus nsh tui` is retired, and its code is kept in the tag `archive/nsh-terminal-backend` (pushed) |
| 2026-11-06 | **Modals are translucent in the TUI again** (the 50% `paint::scrim` blend), reverting the solid cover of `0452ee80`; NSH is back to its translucent backdrop, so they match |
| 2026-11-06 | **Mobile = Sessions, Manager and Settings tabs only** (section 5). It can approve and answer everything a desktop client can (allow/deny, questions), showing less on the small screen |
| 2026-11-06 | **ZMX is for later.** Keep `ZMX_plan.md` and section 6 as they are; build nothing now. The Claude Code Agent SDK / ZMX Settings toggle is part of that later work |
| 2026-11-06 | **macOS GPUI direction:** spike a Rust GPUI host embedding the existing NSH app; keep the current AppKit client intact and expose the spike separately as `nexus gpui`. |

### GPUI host spike

- [~] Added an independent `nexus-gpui` host and launcher. The NSH 0.4.0 bundle and host compile; window rendering, Core connectivity, theme and parity remain unverified. `nsh run App.tsx --target gpui --port 0` produced no output before a 45-second timeout, so its development host is not confirmed working.
- The host pins the same NSH 0.4.0 revision as `nexus-nsh/native` and the installed CLI, rather than the tag, so their registry types match. It registers the app's native Mermaid, annotation and microphone modules. GPUI uses runtime Metal shaders.
- GPUI now loads the built bundle's capabilities metadata: without it, the NSH runtime denies reading the local daemon file. Bundle loading has a regression test; a live GPUI/Core connection remains unverified. Native discovery still does not honor a custom `NEXUS10_HOME`.
- NSH documents GPUI differences including no blur/backdrop filter, different opacity, partial transforms and rounded clipping, macOS video placeholders, web views above NSH content without ancestor clipping, and limited accessibility actions. Manager consent now uses a translucent solid surface without backdrop blur. Verify the theme, assets, network/auth and native features against AppKit and the TUI before considering this more than a spike.

Open (each has a default that holds unless the user objects):

| Decision | Default |
| --- | --- |
| **Z1. ZMX scope (when ZMX is picked up).** `ZMX_plan.md` D1 says subagents only. The new hint ("does not use Nexus agents or tools") reads as if a user would pick ZMX for whole sessions | Keep D1: root sessions always use the SDK; the toggle picks the backend for Claude models a **subagent** uses |
| **Z2. Is the toggle per vendor?** Antigravity has no SDK route | Claude Code: `Agent SDK` / `ZMX`. Antigravity: a plain on/off, shown only in ZMX terms |
| Browser (old 14.4): still wanted? | Deferred. Only the terminal is in this plan; the browser stays in 9.21. If picked up, decide first: rendering in terminals without image support (a lossy text view needs approval and a ledger row), remote browser access (no powerful resource on a tokenless non-loopback server), profile persistence and scoped agent control (start isolated, user-controlled), Chromium distribution, and Core-parsed grid vs raw bytes per client (`reports/section14/spikes.md`) |
| Terminal parser strategy, access model, agent typing (section 2) | Defaults in section 2; confirm at the design review |
| Does `nexus update` install or only report? | Report only until decided |
| Manager Q4: can `navigate` open Settings (now a mobile tab too)? | Open to show a section; never change a value or sign in |
| Do Anthropic Messages and Gemini ship in 0.10.x? | — |
| Can a parent card steer a child session? Persist session grants? Output caps (lines or bytes)? | — |
| Show the Claude SDK's hidden additions (and is sending the account email acceptable)? | — |
| Turn summaries: show "Thought for Ns"; the 140-column footer; store `max_attempts`? | Not shown / accept / today's wording |
| Fold the starting context after the first message; underline prose links? | Leave open / underline in prose |
| Project order in the Projects list and picker (rail only today) | Rail only |

## 1. Merge what is unmerged

State on 2026-11-06 (`git fetch` done). Nothing below has been merged or deleted by this rewrite.

| Item | What it is | Verdict and action |
| --- | --- | --- |
| **Local `main`** | 34 commits ahead of `origin/main` and not pushed: inspector v2, project order/status/remove, overview, sidebar highlight, first-message pin, scroll bar, Settings segments, native inspector and others | [ ] Push after section 4 is green (or push now if the user prefers; the failing tests predate these commits) |
| PR #100 `release 0.12.0` | Release-please bump (version files, changelog). PR #98 (0.11.0) already merged. `lint` fails (TUI fmt) | [ ] Merge after section 4; check the changelog; then the checks in 1.1 |
| PR #99 `fix/ci-core` | Two Core test fixes plus `call.live` docs | **Looks obsolete:** `main` already expects `live: None`, documents `call.live` in `docs/api.md` and skips the inspector test without node; the Core suite is green. [ ] Confirm the diff is empty and close it |
| `origin/fix/daemon-agents-restart` (`74daa3b`) | A clear error when no agent loads, and `install.sh` restarts a running daemon on update | **Real fix, not in `main`.** [ ] Cherry-pick (one conflict, `docs/development.md`), add a test, land |
| `origin/ci/nsh` (`9c5a127`) | Lazy native microphone lookup, unverified | **Superseded:** `main`'s `dictation.ts` already looks it up lazily. [ ] Delete the branch |
| `origin/feat/update-notice`, `fix/default-model-client`, `fix/native-binary-needs-host` | Sept/Oct branches from old Nexus's layout (`rust/tui`, `nexus/client`) | Do not apply to this tree. [ ] Decide whether the update notice is still wanted (it overlaps `nexus update`); otherwise delete |
| Worktrees `design/void`, `design/void-impl`, `polish-timeline` (`.claude/worktrees/`) | 0 commits ahead of `main`, 61–79 behind | Stale. [ ] Remove the worktrees and branches |
| 59 other local branches not in `main` | Old Nexus history | [ ] Review once and prune; ask before deleting anything with unique commits |

### 1.1 Release and installation

- [ ] After the 0.12.0 merge: the release has the four TUI binaries, the wheel and sdist, `install.sh` and
  `SHA256SUMS`; run the installer from the published release on macOS and Linux; plain `nexus` opens the TUI on a
  clean install; the release-download path of `install.sh` passes `--no-binary-package claude-agent-sdk`.
- [ ] Linux: installer checks and `docs/development.md` list `pkg-config`, ALSA headers, a C toolchain, `git`, `uv`,
  Rust, Node 22; confirm the Monty kernel launcher's `/usr/bin/env` path.
- [ ] Remove `nexus-design-mockup/`; bump to the next `-dev` version; replace leftover `nexus10` branch wording in
  `AGENTS.md` and `docs/development.md`.
- [ ] **`nexus update`:** decide whether it installs or only reports (open decision below), then build it.
- [ ] Platform matrix: macOS arm64 from a clean home, then Linux x86_64 and aarch64 (install, `nexus doctor`, daemon,
  TUI, NSH web, a scripted turn, a kernel run).

### 1.2 Retire `nexus nsh tui`

Decided 2026-11-06: retire it, keep the code somewhere safe. The complete code is in the pushed tag
`archive/nsh-terminal-backend` (the `main` commit before the removal); restore it with
`git checkout archive/nsh-terminal-backend -- <path>`.

- [x] Tag the code (`archive/nsh-terminal-backend`, pushed 2026-11-06).
- [ ] Remove `nexus nsh tui` from `scripts/nexus` and `npm run tui`, and the terminal-only code paths NSH grew for it
  (the `--target tui` test target, terminal-only glyph/hint branches). Keep everything web and macOS use.
- [ ] Remove `reports/parity-audit/` and the terminal parts of `reports/nsh-tui-parity/` (the tag keeps them); fix
  `nexus-nsh/README.md` and `docs/development.md` where they describe it. Launcher tests go too.
- [ ] Keep the shared-parity work that landed (scroll bar, segmented Settings, rail, overview, sidebar highlight).
- [ ] Ledger: record that the NSH terminal target is retired and where the code is.

## 2. Terminal inside Nexus

Goal (user, 2026-11-06): a real interactive terminal in Nexus, in the TUI and in NSH (desktop and web, never mobile),
with the same operations in both. Not the old `nexus nsh tui` launcher and not a list of bash jobs. Design inputs:
[reports/section14/spikes.md](reports/section14/spikes.md) (PTY + pyte, `vt100`, `@xterm/headless` all proven in
spikes) and the archived 14.2/14.3.

Defaults to confirm at the first design review (change any of them freely):

- **Core owns the PTY** (`nexus-core/nexus/terminals.py`): clients are viewers/controllers, never process owners.
  Resources have opaque ids, a project/workspace, a creator, status and timestamps. Do not reuse `bash_jobs`.
- **Parser strategy:** Core keeps a bounded raw ring plus a screen checkpoint (pyte, patched for private SGR and the
  alt screen); clients receive checkpoint + subsequent bytes and parse them locally: `vt100` into ratatui in the TUI,
  xterm.js in NSH web/macOS. Decision needed: Core-parsed cell grid instead (consistent, more bandwidth).
- **One active writer;** many viewers. Control is explicit, shown, and released on disconnect, never handed to an agent.
- **Closing a panel detaches; Close terminates.** A Core restart marks terminals ended, never "resumed".
- **Not in the model's context** unless the user attaches output explicitly (a normal visible message with provenance).
  No agent typing in the first version.
- **Access:** loopback or authenticated only. No remote shell over the tokenless non-loopback mode. Escape sequences
  that write the clipboard, open files or launch URLs are gated behind a user action.

Work:

- [ ] Design review and the decisions above; shared resource contract (API, versioned data stream with sequence
  numbers, bounded replay, gap/resync, input ids so a retry never repeats a command). `docs/api.md` first.
- [ ] Core PTY service: spawn in the chosen workspace, process group, resize, binary-safe I/O, UTF-8 split across
  chunks, signals and exit codes, bounded scrollback with an announced discard, orphan cleanup, limits.
- [ ] TUI: panel with a terminal tab and resource list; keys arbitrated with global shortcuts (Esc, Ctrl chords,
  paste, mouse); selection/copy through OSC 52.
- [ ] NSH: the same panel, tabs, list and keys (xterm.js in a WebView on web/macOS; spike the focus and key capture
  against NSH's keymap first). Manifest commands in `interface/manifest.toml`.
- [ ] Tests on real PTYs in an isolated workspace: shell I/O, alt-screen program (`less`, an editor), resize, split byte
  sequences, high-volume output, two clients, control contention, reconnect, exit, close, Core shutdown.
- [ ] Docs: `docs/api.md`, a new `docs/terminals.md`, both READMEs; screenshots in both clients; ledger rows for any
  approved difference.
- Exit: a terminal is not done if it renders only line-oriented output.

## 3. Manager: finish and test end to end

Design: [docs/manager-design.md](docs/manager-design.md). Built: [docs/manager.md](docs/manager.md). M1–M5 are
implemented and a real TUI GPT-Live call with delegated search/read tools worked on 2026-11-04. "Finished" means the
checks below pass in the TUI and NSH and the iOS client (section 5 makes the Manager the mobile client's main surface).

- [ ] **Define the end-to-end suite once, run it in each client:** a scripted day (start a session by voice, ask what is
  running, read a result, approve a waiting action, steer a child, navigate, dismiss a card) with a scratch home and
  scripted provider, in `interface/scenarios/` so both clients share it. The injection and mishearing suite stays.
- [ ] **M0 spikes still unrun:** (a) `pipeline_latency.py` STT → Luna → TTS timings (target under 1.5 s end of speech to
  first audio); (b) `luna_tools.py` tool-choice accuracy; (c) stage mock-ups, look agreed.
- [ ] **Evals with real Luna** (approval needed): retrieval over ~50 sessions and ~100 phrasings (including mangled STT),
  authority, an episode seam, honesty, hand-judged speech quality.
- [ ] **Real voice, each client:** pipeline and GPT-Live with a real microphone and speaker in the TUI and NSH; the
  recent audio-quality polish still needs a listening check.
- [ ] **A 30-minute real call** per client surviving interruption, reconnect and a long delegation (M5 done-when).
- [ ] **iOS and native NSH mic/speaker loop** (needed by section 5): native capture is not built (toast at
  `commands.ts`); GPT-Live route decision from spike (d).
- [ ] **Hands-free (M6):** VAD, barge-in, TUI echo cancellation, notices, optional API-key route for GPT-Live; a week of
  daily use before considering `enabled = true`.
- [ ] **Known boundaries:** authority means "the user spoke" (`docs/manager.md` section 9) and needs a decision; check a
  Manager-started session's worktree after its first turn; Q4 (`navigate` and Settings).
- [ ] **M2 manifest check:** `manager.start` (`/manager`, palette, `alt+m` / `cmd+shift+space`, Q3 to confirm), `manager.talk`,
  `manager.stop` and the engaged keys (design 10.6); run `check_manifest.py`.
- [ ] **Mermaid (step 0):** a real kitty/sixel/iTerm2 terminal and a narrow width; screenshots of the seeded
  `Mermaid diagrams` session in both clients, dark and light; rerun `scenarios.test.tsx` and `mermaid.test.ts` after the lazy
  `nativeModule("nexus/native")` change and the TUI's `cargo test` after the direct-raster change; keep the Ondine pin
  (`b19aa90`) deliberate (an update reruns tests and screenshots; cold CI builds need network for the git dependency).
- [ ] Docs and ledger kept current; tick M0–M6 in `docs/manager-design.md`.

## 4. Fix all tests and lints

Status 2026-11-06: **Core and the TUI are green; NSH is still to do.** Each failure was triaged (code or test wrong?)
rather than updated blindly.

| Suite | Result | Left |
| --- | --- | --- |
| Core `uv run pytest -q` | 1200 passed, 1 skipped (162 s) before the lint change | [ ] Re-run once after the `manager/context.py` lint fix; watch `test_server.py::test_code_nested_write_needs_no_approval_and_kernel_api` (one flake seen under load) and the `test_archive.py` hangs seen under heavy load |
| Core `ruff check nexus tests`, `uv lock --check` | **Clean** (fixed ISC004 in `nexus/manager/context.py`) | — |
| TUI `cargo fmt --check` | **Clean** | — |
| TUI `cargo clippy --all-targets -- -D warnings` | **Clean** (10 errors fixed: `is_multiple_of`, `div_ceil`, `as_chunks`, `contains`, duplicate `if` blocks) | — |
| TUI `cargo test --no-fail-fast` | **816 passed, 0 failed, 8 ignored** | — |
| NSH `nsh check` | Known errors were fixed earlier | [ ] Re-run |
| NSH `nsh test --target web` | 277/279 on 11-06 | [ ] Find and fix the 2 failures (the `i7` picker-footer wording check is one) |
| `check_manifest.py`, `check_theme.py --overlay nexus-nsh/theme-void.toml` | Clean (184 commands, 602 contrast checks) | Keep clean |
| `nexus-mermaid`, `nexus-annotate` | Clean at last check | [ ] Re-run |

What the 12 TUI failures were (all fixed):

- **Real bug, fixed in the test only:** the drag-copy test computed its click column from a byte offset, which is wrong
  now that the rail draws a multi-byte `▏`; the copy code itself was right.
- **Stale tests after the 11-06 design changes:** the left sidebar is 5 cells right of where tests assumed (the project
  rail), the search glyph and session rows moved one cell left, the selected session carries the active block (not the
  spacer row), and the rail's two-letter label `NT` is centred, so its middle cell is `T` (the test expected `N`).
- **Stale Markdown test:** a Mermaid fence draws as a diagram with `◇ mermaid · copy source`; its source is still
  available to Copy (asserted through `fence_sources`).
- **Six Manager tests:** the header indicator is gated on `[manager] enabled` (off by default); the fixture never
  enabled it. No product bug.

Local machine problems met on the way (not repo bugs): Homebrew `node` once failed to load `libuv.1.dylib` (fixed by the
user); Homebrew Python 3.14.7 cannot import `sqlite3` (missing `libsqlite3.dylib`), so `uv run pytest` fails at collection
with that interpreter (python.org 3.14.5 works); `~/.nsh/bin/nsh` must be the NSH 0.4.0 source build (`nsh --version`
prints `0.4.0 (3789a45)`); the wasm test build needs `llvm-ar` (`rustup component add llvm-tools`,
`AR_wasm32_unknown_unknown`) because Apple's `ar` produces a broken archive. Disk: a shared `CARGO_TARGET_DIR` across
worktrees grew ~22 GB in 30 minutes (each NSH build is ~1 GB; `nsh run` hot-reload loops keep rebuilding), so work in one
worktree and prune the target directory.

Then:

- [ ] Fix the two NSH failures and re-run `nsh check`; re-run the Core suite and record the result here.
- [ ] Record anything that fails only under load. Add the TUI replay-bench result to this table without gating on it.

## 5. Mobile cut-down client: Sessions, Manager, Settings

Decision (user, 2026-11-06): mobile (the NSH phone build, iOS and any other mobile target) has **three tabs only:
Sessions, Manager and Settings**. A session view shows the session's status, the user's messages and the agent's replies
and nothing else: no tool groups, no context header. Everything else (files, editor, Git, inspector, terminal, browser,
projects overview and the other desktop surfaces) is desktop/web only. Adding anything beyond this needs the user's
approval. `AGENTS.md` is updated with this change.

Not started: `phone` in `nexus-nsh/src/store.ts` still renders the full timeline and the desktop panels.

- [ ] **Navigation:** a three-tab bar (Sessions, Manager, Settings) replaces the current phone pages; define the Manager
  tab (the header indicator and dropdown become a full tab: caption, cards, typed input, voice; plus the read-only
  backend-session view only if the user wants it).
- [ ] **Session view:** status, user messages, agent replies, composer. **Mobile can approve and answer everything a
  desktop client can** (user, 2026-11-06): allow once/always/deny, answer a question (options and free text), choose
  among offered choices, steer/queue/interrupt. It shows less, because the screen is small: a compact card with the
  command or question and the choices, not the full diff, parameter table or tool group. Define the compact card once
  and test it at 390 px. Allowed, since an answer is part of "status"; the other tool detail stays out.
- [ ] **Settings:** decide which sections apply (Providers sign-in, Appearance, Notifications, Voice, About; hide Agents,
  Tools, Skills, MCP, Hooks, Files/Editor, Terminal). List them in the manifest under a `mobile` scope.
- [ ] **Remove** from the phone build: Files tab, editor, Git, inspector, terminal, browser, overview, project rail
  management, row menus that reach them. Make them unreachable from the palette and shortcuts too.
- [ ] Phone polish already listed in the archive (8.5 keyboard avoidance, 44 px targets, safe areas, gestures,
  Settings as pushed pages, Connect copy for a tailnet host, iPad decision); do the ones on the three tabs.
- [ ] Native mic for the Manager (section 3) so the Manager tab can talk.
- [ ] Tests on the phone idiom at 390 px, a phone screenshot of each tab, a real simulator run and a signed device build
  (see the `nexus-ios-device` skill); update `nexus-nsh/README.md` and `docs/`.

## 6. ZMX mode for Claude Code and Antigravity (later)

**Parked (user, 2026-11-06): not for now.** Keep the plan and `ZMX_plan.md` as they are; do not start M0 or any code until the user asks.

Full design, lessons, milestones and risks: [ZMX_plan.md](ZMX_plan.md) (written 11-04; nothing built). Summary: a
subagent runs on the real `claude` (interactive PTY, hooks, transcript) or `agy` (stream-json) CLI, which runs its own
loop, tools, permissions and sign-in; Nexus shows it as a child session. Off by default, always labelled, no Nexus
permissions inside it, never answers a CLI prompt (it ends as `needs_attention`).

**New (user, 2026-11-06): a Settings toggle for Claude Code between two modes**, in both clients with the same wording:

| Mode | What it is |
| --- | --- |
| **Agent SDK** (default) | The existing `claude` provider: Nexus owns the loop and runs Nexus agents and tools |
| **ZMX** | The real Claude Code CLI runs the work |

Hint under the control (both clients): *"Use Agent SDK for an enterprise or full-price API account. Otherwise ZMX is
usually better, but it does not use Nexus agents or tools."* Antigravity has only ZMX, so it gets an on/off with the
same drawback line (Z2). Scope follows decision Z1.

- [ ] **Decide Z1 and Z2** (above) and where the setting lives (`config.toml`, a user-level key a project file cannot
  set; Settings › Providers). Update `ZMX_plan.md` section 4.8 (it described one global switch).
- [ ] **M0 spikes** with real, small runs and the user's approval: Claude transcript request shape, `--resume`,
  `--append-system-prompt`, `StopFailure`, `--mcp-config`; agy 1.4.12 stream-json turn, `--conversation`,
  `--mode auto`, soft denial, interrupt. Record each answer and a fixture.
- [ ] **M1** agy as a child session (smallest slice): workers, provider row, catalog, `ExternalAgent`, guards,
  `session.worker`, the switch, root refusal, docs, offline suite.
- [ ] **M2** Claude Code as a child session: PTY, hooks, transcript reader, tool-name display mapping, `--resume`
  follow-ups, Claude's own subagents.
- [ ] **Settings toggle and hint** in the TUI and NSH, the `ZMX · Claude Code` / `ZMX · Antigravity` label on the strip,
  child card and tab, the starting-context disclosure card, `needs_attention` state, refusals shown with a reason.
- [ ] **M4** recovery, orphan records, usage panel for agy, `nexus doctor`, `docs/external-workers.md`.
- [ ] **Vendor terms:** ZMX_plan.md never established that driving a subscription CLI this way is permitted. Check
  Anthropic's and Google's current terms before release; the Settings text says so until verified. Keep it off by default.
- [ ] Record the timeline-rule exception in AGENTS.md and the ledger when M1 lands (ZMX_plan D2).

## 7. Carried over, not scheduled

Everything else open in the previous plan is in [section 9](#9-backlog-carried-over-from-the-previous-plan), in full.
The areas, at a glance:

| Area | Where | Note |
| --- | --- | --- |
| Real-device and real-provider verification | 9.1 | Real daemon with both clients, OSC 7501, audio, the drawing modal, iOS |
| File explorer, editor, Git page | 9.2 | Wire the vendored editor into the TUI; explorer actions; watcher; the whole Git page |
| Turn summaries, TUI performance, agent loop | 9.3–9.5 | Cold-start log scan, 1 s image probe, fuzzy `edit`, the 14.7K vs 3.9K token gap |
| Interface polish, both clients, NSH, Settings, TUI | 9.6–9.11 | Modal footers, scroll model, spinners, group fold, pending-row actions, Ctrl+R |
| Project rail order verification | 9.12 | Landed 11-06; needs verification |
| Extensions, providers, worktrees, reliability, event storage | 9.13–9.17 | OpenAI/Ollama real calls, Anthropic Messages, Gemini, worktree merge, retention |
| Inspector hardening; terminal and browser design detail | 9.18–9.22 | 9.19–9.20 are the detail behind section 2; the browser (9.21) is deferred |
| CI | — | Deferred by the user (2026-11-04) |

## 8. Provider usage panel (usage table)

The user's review of the Provider usage modal (screenshot, 11-06) asks for a table instead of the current
label + bar + prose rows: a ten-block progress gauge, then the percentage left, the reserve (the old Nexus
pace calculation) and the expiry time. The words marked in red go away. Both clients land this in the same
change; no desktop client-specific difference is expected. **Landed and visually compared in both clients.** Carried over whole from the previous plan's section 16 (user asked to keep it, 2026-11-06).

### 8.1 The table and its cells (both clients)

One header row, then a provider group row and one column-aligned row per window. The gauge shows *used*
and the next column states what is left, so the two add to 100%:

```text
Window                                  Progress    Left  Reserve  Expires
ChatGPT (Codex)  Plus
  5-hour                                ██████████    0%      —    1h 8m
  Weekly                                ██▃░░░░░░░   76%     -1%   5d 9h
Claude (Agent SDK)  Pro
  5-hour session                        ███▅░░░░░░   64%      —     now
  Weekly (all models)                   ██████▆░░░   32%    +11%   1d 12h
xAI  SuperGrok Lite
  Weekly                                ▁░░░░░░░░░   99%    +30%   4d 20h
GitHub Copilot  Business
  Premium requests (monthly)            ▇░░░░░░░░░   91%     +8%   24d 18h
OpenCode Go
  5-hour                                ░░░░░░░░░░  100%      —    1h 6m
  Weekly                                ███▂░░░░░░   67%    +28%   2d 18h
  Monthly                               ████▆░░░░░   53%    -10%   19d 0h
Updated 2m ago
r refresh · esc close
```

- [x] **Header row:** `Window`, `Progress`, `Left`, `Reserve`, `Expires`, muted, once above the first
  provider. Provider group rows keep the bold name and the muted plan (`ChatGPT (Codex)  Plus`); its window
  labels are indented two cells. Do not repeat the provider on every window.
- [x] **Progress (the wide column, exactly 10 cells):** `used_percent` decides the fill in eighths:
  `e = round(used × 80 / 100)` (nearest, ties to even), then ten cells filled left to right, each cell
  holding `min(8, max(0, e − 8·i))` eighths. Full cells are `█` (U+2588); the boundary cell is
  `▁▂▃▄▅▆▇` (U+2581–U+2587), so it fills **from the bottom up** as requested; empty cells are `░`
  (U+2591). 24% → `██▃░░░░░░░`, 36% → `███▅░░░░░░`, 100% → ten `█`, 0%/unknown → ten `░`. The bar is
  horizontal because the table is one line per window; a literal ten-row-tall gauge would make every
  window ten lines high. Glyphs come from the block-elements range the user linked
  (`https://unicode-explorer.com/b/2580`).
- [x] **Left:** `100 − used_percent`, following the old Nexus `percent()` rule: a whole percent, except a
  non-whole value under 10 or over 90 keeps one decimal (`0.2%`, `99.8%`); unknown is `—`. Right-aligned.
  This replaces the old used-percent column (42% used now reads 58%).
- [x] **Reserve:** copy the `nexus01` calculation from `nexus/ui_support/usage.py`
  (`RESERVE_MIN_PERIOD`, `elapsed_percent`, `reserve`, `reserve_phrase`): only when `period_seconds` is
  at least 7 days and `used_percent` and a future `resets_at` are known;
  `elapsed = 100 × (1 − (resets_at − now) / period_seconds)`, `reserve = elapsed − used`. Round to the
  nearest integer with ties to even, as Python's `round` does (8.5 → 8). Show `+N%` for spare usage,
  `-N%` for over pace, `0%` for on pace and `—` when not estimable (five-hour windows, missing period, a
  reset already due). Right-aligned; spare takes the success colour, over pace the warning colour,
  `0%`/`—` stay muted. Do not show the old `┃` pace marker.
- [x] **Expires:** the time until reset only, never an absolute stamp: `now` under a minute or when due, otherwise the
  two largest units (`3h 10m`, `2d 4h`, `6d 5h`); `—` when `resets_at` is missing.
  Left-aligned. This is the old `resets in …` text without the words.
- [x] **Colours and tone:** filled gauge cells keep the existing used-percent thresholds (success below
  70, warning below 90, error at 90 and above); empty cells use the dim border/muted role. The label and
  the Left number stay in primary text; the provider plan, header, `—` and notes are muted.
- [x] **Words removed (the red marks):** drop the Codex `Limit reached: new requests wait for the next
  reset` note, the xAI per-product notes such as `GrokChat: 1% of credits`, and every per-window `detail`
  suffix (`225 of 300 left`, `20,280 over quota`, `· 20,28…`). `detail` stays in the API for other
  consumers but is never printed by this modal; when a window has no `resets_at`, Expires is `—`
  rather than the old raw-text fallback. Keep the other notes (credits balance/unlimited, no plan
  limits, `Could not read the limits: …`, `No limit windows reported.`) as muted full-width lines under
  their provider, plus `Updated … ago`, the empty and loading states and the `r refresh · esc close`
  footer.
- [x] **Responsive rules:** the gauge and the three numbers keep their widths; the Window column flexes
  and truncates with `…`; Expires truncates with `…`. Never wrap a window row. The TUI keeps its
  80–88-cell large modal and 30-row cap. NSH uses its large modal as `tall` (90% of the window) so the
  table fits; on phone widths (a standing phone-layout exception) each window becomes two lines: label,
  then gauge, Left, Reserve and Expires.

### 8.2 Data and formatting

- [x] `period_seconds` and `kind` are already in the API (`GET /api/usage`); parse them in both clients.
  The reserve is derived client-side from `used_percent`, `resets_at`, `period_seconds` and the client's
  `now`, beside the countdown; do not add a server endpoint or store a stale reserve in the snapshot.
- [x] Unknown `used_percent` → ten empty cells, `—` Left, `—` Reserve, and the countdown if known.
  Missing `period_seconds` (older snapshots) → Reserve `—`, everything else unchanged. A reset at or
  before `now` → Expires `now`, Reserve `—`.
- [x] Filter the removed notes in both clients as well as at the source, so an older saved snapshot
  cannot bring them back: Codex notes starting `Limit reached:`; xAI notes ending ` of credits` that
  are not `Prepaid credits:`. The existing xAI prepaid and Copilot chat/completions/AI-credit filters
  stay.
- [x] Keep the snapshot semantics: the modal is a snapshot; countdowns and reserve do not tick while it
  stays open (the existing background-refresh follow-up remains separate).

### 8.3 Core

- [x] `nexus-core/nexus/provider_usage.py`: remove the `limit_reached` note in `parse_codex` and the
  `productUsage` note loop in `parse_xai`; keep the credits and prepaid notes. Keep
  `parse_copilot`/`parse_claude` `detail` values (the API keeps them; the modal stops printing them).
- [x] Update `nexus-core/tests/test_provider_usage.py` with a `limit_reached: true` payload and a
  `productUsage` list, asserting neither becomes a note; keep the existing prepaid and detail assertions.
- [x] `docs/api.md` (Usage K11): say the modal is a table of gauge + percent left + reserve + expiry;
  `detail` is retained for other consumers but not shown; note the reserve rule and its 7-day threshold.

### 8.4 TUI (`nexus-tui`)

- [x] `src/usage.rs`: extend `Row` with a `Header` row and make `Window` carry the label, the used value,
  the formatted Left, Reserve and Expires strings (computed from `now`). Parse `period_seconds` and
  `kind`. Drop the `detail` join from the reset text. Add pure helpers for `gauge_cells(used) -> [u8;
  10]`, `percent_left`, `reserve`, `reserve_text` and `duration` with unit tests at 0, 1, 9, 10, 24, 36,
  47, 68, 90 and 100 percent; five-hour vs weekly/monthly; a missing/old period; a reset in the past;
  unknown usage; and the half-to-even tie (8.5 → 8).
- [x] `src/render.rs::draw_usage`: draw the header and the five fixed columns, map `gauge_cells` through
  the theme glyphs, colour only the filled cells, right-align Left/Reserve, left-align Expires, and
  truncate the label and Expires. Keep `MODAL_LARGE`, the height clamp and the existing `r`/`esc` hits.
- [x] ASCII mode: ten `#`/`-` cells; a partially filled cell counts as `#` (rounded up). Colour rules are
  unchanged.
- [x] `interface/theme.toml`: add `[glyphs.usage]` with `full` (`█`/`#`), `empty` (`░`/`-`) and
  `eighth_1`…`eighth_7` (`▁`…`▇`/`#`); add `usage` to the glyph checks in `interface/check_theme.py`,
  the `Glyphs` struct/accessor in `src/theme.rs`, and the generator in `nexus-nsh/scripts/gen.py`.
- [x] `tests/behaviour.rs` (and the usage unit tests): assert the header, the gauge strings, `76%`/`0%`
  Left, reserve cells and the hidden `Limit reached` / `of credits` / `of 300 left` text.
- [x] `nexus-tui/README.md`: update the K11 bullet to the table.

### 8.5 NSH (`nexus-nsh`)

- [x] `src/store.ts`: add `period_seconds?: number | null` and `kind?: string` to the usage window type.
- [x] New pure `src/usage-format.ts` (or exports beside `until`): `leftPercent`, `reservePercent`,
  `reserveText`, `gaugeCells` and `until` with the same rounding and threshold rules as the TUI.
- [x] `src/ui/usage.tsx`: replace the flex bars with a monospace table: header row, provider group rows,
  window rows, 10-cell gauge drawn from the generated `usage` glyphs, fixed-width Left/Reserve/Expires
  columns and a flexing Window column. Mark the gauge `accessible={false}` and give each row an
  accessibility label with all four values. Use `Modal … tall`; keep the existing refresh/footer and the
  loading/empty/error states. Hide the removed notes in `notes()` for older snapshots.
- [x] `usage.test.tsx`: update the expected percentages to the Left values (42% used → 58%), assert the
  header, a gauge string such as `██▃`, `—`/reserve cells, and the absence of `Limit reached`, `of
  credits` and the Copilot detail. Unit-test the pure formatting helpers with a fixed clock (including
  8.5 → 8 and the 7-day threshold).
- [x] `nexus-nsh/README.md`: update the K11 bullet to the table.

### 8.6 Shared scenario and screenshots

- [x] Extend `interface/scenarios/provider-usage.json` with the values above: Codex 100%/24%, Claude
  36%/68%, xAI 1%, Copilot 9%, OpenCode 0%/33%/47%, plus the notes that must disappear (`Limit
  reached: …`, `GrokChat: 1% of credits`), a provider with no plan limits and one with an error.
  Keep time-dependent assertions in the pure helper tests; the scenario test asserts labels, Left
  values, gauge glyphs and the hidden words.
- [x] Add a usage fixture to `nexus-nsh/e2e/scripted_core.py` that returns the scenario with
  `resets_at`/`period_seconds` computed relative to the capture time (monkeypatch
  `nexus.provider_usage.providers_usage` or seed the usage cache), so both clients see identical rows
  without provider calls. Document the mode in the script's docstring.
- [x] Capture the same state in both clients: TUI `--dump-frame` at 170x50 and 120x36, NSH web at a
  comparable window (plus one phone-width capture). Compare the header, column order, gauge glyphs,
  numbers, colours and the absence of the marked words; save under `reports/provider-usage-table/`.

### 8.7 Checks when it lands

- [x] `cd nexus-core && uv run pytest tests/test_provider_usage.py -q && uv run ruff check nexus/provider_usage.py tests/test_provider_usage.py`
- [x] `cd nexus-tui && cargo test usage && cargo test --test behaviour usage && cargo fmt --check && cargo clippy --all-targets -- -D warnings`
- [x] `cd nexus-nsh && npm run gen && nsh test usage.test.tsx --target web` (plus the pure helper test and `nsh check`; record the known `app.clock` / `Button.onFocus` errors if they remain).
- [x] `python3 interface/check_theme.py --overlay nexus-nsh/theme-void.toml && python3 interface/check_manifest.py`
- [x] Update `docs/porting-ledger.md` on landing: "Provider usage showed a continuous bar, used percent and prose notes | Both clients show a five-column table: ten-block gauge filled bottom-up, percent left, reserve (old 7-day pace formula), expiry; the Codex limit-reached note, xAI per-product credit notes and per-window detail text are gone (2026-11-06) | User request". Record any screenshot-only difference; none is expected on desktop.
- [x] Tick this section, move its one-line record to Landed and remove the status-table row's "planned" wording.

## 9. Backlog carried over from the previous plan

Every open item of the previous plan that is not covered by a section above, kept in full so nothing is lost. Item text is
verbatim from [the archive](docs/archive/PLAN-2026-11-06.md); `file:line` references inside it point at the code as it was on
2026-11-04 and may have moved. Order inside each area is the old order, not a priority. Not scheduled: pull an area into a
numbered section above when it is started.

### 9.1 Real-world verification (old section 2)

- [ ] **Real daemon with both clients:**
  - model memory with no default model: a draft with no model asks for one (Core's draft-context returns 40 "no
    model selected", so the preview is empty until one is chosen; consider a provider-independent estimate); a
    remembered model whose provider is signed out is shown, not replaced; a pre-change session with no model asks;
  - sign-in from each client's Providers pane, one device-flow and one browser-flow provider;
  - steer/queue ordering, and interrupt returning pending steers and queued messages to the composer oldest first;
  - project tab isolation across projects (browser end-to-end coverage too);
  - `wait: false` child sessions with a real provider.
- [ ] **OSC 7501 program status** in a supporting terminal: reconnect, suspension and several sessions at once
  ([docs/development.md](docs/development.md#terminal-program-status)).
- [ ] **Audio:** completion and input-needed cues through a real audio device in the TUI and a browser;
  dictation loading/start/stop cues and microphone behavior in both clients; STT microphone capture, a real
  Parakeet download and transcription, and the opt-in `nexus/voice/benchmark.py` (speed, RAM).
- [ ] **Static-only UI changes from 11-03 need a runtime pass:** sticky header isolation, square NSH surfaces and
  the 48 px rail, the shared tab widths, collapsed-paste preview and NSH's Ctrl+X leader. Screenshots of both
  clients at 80×24 / 160×48 and NSH at 390 / 1024 / 1440 px, dark and light.
- [ ] **Drawing modal:** a hand-drawn stroke with the real mouse in Ghostty (injected SGR bytes are checked).
- [ ] **iOS:** a real simulator runtime and a signed device build of the current app; a smaller first message page
  on phones if session opens still feel slow over a cellular tailnet.

### 9.2 File explorer, editor and Git page (old 4.1)

Desktop and web only; never mobile. Decisions: the editor stays small (highlighting, undo/redo, no tabs, language servers or Vim mode); Git scope is worktrees, changes and branches with actions (no merge, stash, rebase, conflict UI or hunk staging); both are silent to the model; no new auth. Files and diffs open in a centred modal.

- [ ] Replace the TUI's `Composer` file buffer with the vendored editor. Checked 2026-11-04: `src/code_editor.rs`
  (453 lines) is declared in `lib.rs` and used only by `examples/editor_demo.rs`; the app edits through `FileEdit`
  (`ui.rs:273`, drawn by `draw_file_edit`, `render.rs:530`, keys in `file_edit_key`, `app.rs:7560`: Ctrl+S, Tab
  inserts four spaces, Ctrl+Z/Ctrl+Y, arrows and pages) with no highlighting, folds or diff view. Map TUI keys to the crate's `apply(action)`; its crossterm 0.29
  input feature stays off. Copy through OSC 52 and paste from bracketed paste (`arboard` removed).
- [ ] TUI find and replace on `set_marks` (today `/` searches only the read-only view, `file_search_key`,
  `app.rs:7783`; check what `ctrl+f` at `app.rs:8080` does while editing) with `editor.find` (`ctrl+f`): plain text and a case toggle, the same
  layout as NSH's find bar (`cmd+f`, next/previous, count, Esc closes it before the modal).
- [ ] Reading and editing become one mode in both clients: drop the separate read-only view and `editor.edit`. A
  clean file reloads when it changes on disk; a modified one shows "changed on disk".
- [ ] Check the web bundle serves `nsh-syntaxes.bin` (syntaxes load on first use; plain text until then).
- [ ] Measure the TUI binary before and after each language beyond the starting 13 (Rust, Python, JavaScript,
  TypeScript, Go, C, HTML, CSS, YAML, JSON, TOML, Shell, Markdown); anything else opens as plain text.
- [ ] Tests: TUI unit tests for the vendored additions and key mapping, PTY screenshots at 80×24, 120×40 and 160×48,
  `--bench` typing on a 1 MiB file; NSH save and stale-file paths in the browser harness.
- [ ] Sidebar tabs: the manifest (`interface/manifest.toml:187–196`) lists only Sessions and Files, while NSH's
  `leftTab` also has `projects`; the manifest already names `sidebar.git`, `focus.git_file` and `git.open_diff` with
  no UI behind them. Make the manifest and both clients agree when the Git tab lands.
- [ ] New file, new folder, rename and delete from the keyboard, the row menu and the palette. Delete asks first,
  gives the item count for a folder, and is permanent.
- [ ] Core: `POST /api/project/{id}/files?session=` with
  `{action: "create_file" | "create_dir" | "rename" | "delete", path, to?}`, the same confinement and no-symlink
  rules as `files.py`; publishes `files.changed`. Limits stay UTF-8 and 1 MiB; binary and oversized files say why
  they cannot open.
- [ ] Watcher: `uv add watchfiles`; runs while a client has Files or Git open, on a 60 s lease the client renews;
  250 ms debounce; `files.changed` with `source: "disk"` and `git.changed` for `HEAD`, `index` and `refs`; skips
  Git-ignored paths. If it cannot start (e.g. inotify limits), fall back to a 3 s status poll while the tab is open
  and show that the view is polling.
- [ ] New `nexus-core/nexus/git_ops.py` using `worktrees._git` (it reports stderr; `files._git` drops it);
  `worktrees.ENV` already sets `GIT_TERMINAL_PROMPT=0`. `worktree` must be a registered worktree of the project's
  repo; one lock per repo, as `worktree_lock`. Timeouts 120 s, 300 s for network actions; clients show progress and
  never give up first.
- [ ] Add the route to the mutation matrix in `docs/api.md`: an external effect, so a crash leaves 409
  `mutation_outcome_unknown` and it is never retried blindly. Control-plane only: no task or control tool reaches it.
- [ ] Events `git.changed` and `files.changed` carry no `session_id`, so nothing enters a session's log or
  timeline; confirm against `bus.py`.
- [ ] Safe defaults: pull is `--ff-only` (a diverged branch shows Git's error); push never forces and says when it
  will set an upstream; switch never stashes and shows Git's refusal, switching to a remote branch creates the
  tracking branch; branch delete is `git branch -d`, local only, with a second warning before `-D`, and refuses a
  branch checked out in a worktree; discard is `git restore` or deleting an untracked file, and says it cannot be
  undone; commit has no amend, hooks run and their failures show; worktree remove keeps today's rules (never the
  main checkout, a locked or dirty worktree, or one in use by a session); an in-progress merge or rebase started
  elsewhere shows a banner and disables actions.
- [ ] Confirmation: every action except stage, unstage and refresh opens one popup after the click (fetch
  included), naming the action and target, focus on Cancel; commit asks after the message is written. The dialog
  lists **working** sessions (an active turn, pending approval/question or running background job in this worktree
  or a child of one; warning, danger button) and **idle** ones (non-archived, directory in this worktree), from a
  fresh `git/live` read.
- [ ] Race protection: the request carries `confirmed_live`; if another session started working, Core returns 409
  `worktree_live` with the new list and the dialog reopens.
- [ ] Credentials: fetch/pull/push run as Core with Core's environment; without an SSH agent they fail and show
  Git's message. No credential UI.
- [ ] Core tests on temporary repos with a bare remote, no network: path confinement, refused dirty worktree,
  unmerged branch delete, `--ff-only` on a diverged branch, `worktree_live`.
- [ ] NSH `desktopTools = !(native && touch)` in `store.ts` (absent today; `store.ts:249–252` has only `native`,
  `touch` and `phone`): hides Files and Git and makes `editor.*`, `files.*`
  and `git.*` unavailable. This removes today's Files tab and read-only file view from iPhone and iPad. Check the
  idiom Mac Catalyst reports (it should keep the tools). Core routes are not gated.
- [ ] Docs: `docs/api.md`, `docs/tools.md` (the freshness guard sees user edits; agent tools unchanged),
  `docs/daemon.md`, `docs/development.md` (vendored crate), `interface/manifest.toml`,
  `interface/design-language.md`, `interface/capability-matrix.md`, `nexus-tui/NOTICE`, `nexus-nsh/README.md`.
- [ ] Ledger rows: NSH has several carets, more languages and indentation folds, the TUI Tree-sitter folds; syntax
  colours map to the same tokens but tokenise differently (syntect vs Tree-sitter); no Files or Git tab on iOS; Git
  actions are silent to the model, so a mid-turn branch switch is caught by the re-read check but the model may
  still believe it is on the old branch (an accepted exception to the one-timeline rule).

### 9.3 Turn summaries (old 5)

- [ ] **Real-daemon checks:** a real session (`nexus chat`) shows usage and model on every `turn_request` row, and
  `curl` on a real session returns the documented `/turn` shape. The step 1 migration and backfill run once at the
  next daemon start (0.4 s on a copy; usage for 578 of 587 requests).
- [ ] **Step 7 remainder:** `docs/development.md` where it mentions event history; `interface/design-language.md`
  for the floor row ("the turns above have no recorded details…").

### 9.4 TUI performance and launch time (old 6)

- [ ] **Cold Core start scans the whole event log.** `open_bash_jobs()` (`store.py:2745`) selects `bash.started` /
  `bash.finished` rows with no index led by `type`, so every daemon start scans all events (1.37 M then) and runs
  `json_extract` on each: 1.9 s per cold start, 14.7 s with pending migrations. Add a partial index
  `event_bash on event(type, session_id, id) where type in ('bash.started','bash.finished')` beside the other
  deferred indexes, or keep open jobs in their own small table. Section 13 may remove the need.
- [ ] **The terminal image probe costs up to 1 s on every launch** (`main.rs:526`,
  `Picker::from_query_stdio()` waits for a reply some terminals never send). Skip it when the environment says it
  will not answer, cache the result per terminal, or shorten the timeout.
- [ ] **Paint-time animation overlay.** A viewed, running turn still rebuilds the timeline every frame because the
  spinner, shimmer and flash are baked into the lines. Record their cells and re-style them on a cache hit, if that
  shows up in a profile.
- [ ] **Viewport-only assembly (phase 4).** At 2,000 messages a stream draw is ~15 ms and at 5,000 ~33 ms. Use
  indexed heights and build only visible blocks plus a margin, preserving total height, jump-to-block, sticky user
  headers, folds, scroll anchors and selection/copy; width changes and newly loaded history invalidate heights.
  Trigger: a reported 2,000+ message workload.
- [ ] **Per-dependency invalidation.** Events from non-selected sessions still rebuild the viewed timeline
  (correct but conservative); replace the single `timeline_revision` with explicit dependencies if profiling shows
  it matters.
- [ ] **Draw cost has grown.** The 2026-11-04 replay (200 messages, 160×48, release) measured paced draws at 4.09 ms
  mean and chrome-only frames at 1.73 ms, against 1.13–1.17 ms and 0.36 ms on 10-31 (the timeline build 2.77 ms vs
  1.46–1.58 ms). Re-run on an idle machine; if it holds, bisect the 11-01 to 11-03 UI commits (fixed tab widths,
  rail, sticky header, Manager stage) and restore the budget.
- [ ] Keep a scratch-Core PTY run for end-to-end terminal behavior and input latency; the replay bench
  (`nexus-tui --bench WxH --replay`) measures scheduling, layout and paint only.

### 9.5 Agent loop, tools and context (old 7)

- [ ] **Tiered fuzzy matching in `edit`** (curly quotes, dashes, trailing whitespace, CRLF), as opencode does, and
  a multi-edit input like pi's `edits[]`.
- [ ] **Soft nudge at about 80% of `steps`**, and a default effort for `scout`/`quick`.
- [ ] **Code mode:** measure comparable real-provider tasks for correctness, requests, tokens and elapsed time
  ([docs/agents.md](docs/agents.md#choosing-code-mode)). The live `~/.nexus/agents/build.md` override needs the
  same guidance as `builtin_agents/build.md`.
- [ ] **Explain the 14.7K vs ~3.9K token gap** (was TODO 1). A Claude Haiku 5.5 "hi there" turn read 14.7K input
  (47% cached) against a `~3,965 tok` starting context. Check, cheapest first: restart the daemon (it predated the
  empty working directory and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`) and repeat; capture Claude Code 2.1.314's
  request with a local server ([docs/llm-providers.md](docs/llm-providers.md#claude-agent-sdk)); check whether
  chars ÷ 4 undercounts the 15 tool schemas plus `StructuredOutput`; look for a user-level Claude Code setting or
  memory loaded despite `setting_sources=[]`. Whatever it is, show it in the starting context or remove it.
- [ ] **NSH after a long streamed turn** keeps its main thread busy 5–7 s rendering it, so clicks wait. Remaining
  cost is the live Markdown row and browser layout.
- [ ] **A lighter context endpoint** (budget only) if `GET …/context` cost shows up again; Core rebuilds the whole
  request body per call (5–12 ms warm, 30–130 ms first read). No Core-side cache: a stale one would show context
  the model does not have.
- [ ] Return per-section starting-context detail text from Core for session drafts.
- [ ] Allow `/btw` during a running turn in both clients; NSH browser coverage for opening the original session from
  a handoff.
- [ ] Decide whether to persist session grants; expose grant listing and clearing in the clients.
- [ ] A Settings section for provider limits; verify `/api/usage` with a real account.

### 9.6 Interface polish and iOS (old 8.0)

Items about the phone/iOS build apply only to the three mobile tabs in section 5; the rest of the iOS list is superseded by the cut-down client.

- [ ] Give long TUI confirmation dialogs a scrollable body so they can obey the same 70%
  height cap; they currently retain the existing height fallback to keep confirmation text visible.
- [ ] Finish the footer audit across every modal: consistent helper, ordering and
  close/back/cancel wording, including provider and nested-page hints.
- [ ] Review provider detail with pending sign-in links/codes and the full tool grid at small
  and large sizes in both clients; exercise the remaining keyboard-only paths.
- [ ] Capture real PTY/terminal modal screenshots (the TUI evidence above is the real renderer's
  `--dump-frame` output), and repeat NSH verification with the supported native toolchain.
- [ ] Implement the same model in the TUI (`sticky.rs`, `layout.rs` follow/anchor, the jump pill) and check its
  current behaviour against it.
- [ ] Tests in both clients: stream while scrolled up (the view must not move); stop just short of the end; send
  a message (its band at the top, the reply below); a long reply growing past the viewport (no follow until the
  end is reached); a session switch and back (position restored); late-measured rows above the viewport.
- [ ] Investigate remaining NSH scroll jumps end to end with a scratch Core: the NSH List fixture preserves a
  visible marker for a row growing above it and for prepended rows on web, AppKit and GPUI development hosts;
  the headless engine instead reports offset delta 0 and marker displacement 80 after growth despite an initial
  offset of 150. A headless trace shows the core did emit `ScrollBy` 80; upstream `nsh-testing/src/screen.rs`
  applies that item but, unlike scroll commands, does not add it to `scrolled` for reporting back to the engine.
  This is a plausible cause of stale reported offset/measurement, not yet confirmed by an upstream fix.
  Fake-network full-app tests exercise history paging and a live delta, and compare some mounted message text
  across streaming and a session switch on web, AppKit and GPUI. A browser-coordinate regression confirmed that
  loading older history left offset 0 and displaced the previous first message out of the mounted viewport. NSH now
  restores the old visible row by key on a page prepend; browser tests retain its exact position with the last page,
  another page available, and a scroll while the request is pending. Native paging tests verify data/navigation,
  not coordinates. A GPUI switch test failed once under concurrent host runs and passed five serial reruns; its
  cause remains unknown. Exact native positioning, packaged hosts and a live scratch Core remain unverified.
  Do not treat these cases as proof that all random jumps are fixed.
- [ ] Write the generator (`interface/scripts/spinners.py` or `scripts/gen.py`) that emits the frame lists, and add
  `[glyphs.spinner_tab]` and `[glyphs.spinner_agent]` (unicode, ascii) to `interface/theme.toml`, with the random
  shape choice specified as data (variants plus a per-session seed) so both clients behave the same.
- [ ] TUI: `theme.spinner(frame)` becomes two functions; the tab strip uses the snake (`render.rs:3176`, `:3679`),
  the subagent strip and child cards use the juggler (`timeline.rs:3515`). Check the animation cache keys
  (`Timeline.animated`) still hold.
- [ ] NSH: `Spinner` in `kit.tsx` takes a `kind`; `header.tsx` tabs use the snake, child rows and the subagent strip
  the juggler; regenerate `src/gen/theme.ts`.
- [ ] A preview (TUI `--dump-frame` sequence, NSH story) for tuning; the user approves the look before it lands.
- [ ] TUI: add a fold state per group key (start frame, rows at start), draw the rows that remain this frame
  above the header's final state, then run the existing flash; keep requesting frames while any fold is live; key
  the timeline cache on it like `animated`. Respect reduced motion, reload/reconnect (no fold) and a user opening
  the group mid-fold.
- [ ] NSH: the same sequence, row by row at 80 ms (an opacity/height transition per row, removed from the bottom),
  then the existing 600 ms flash; no animation with reduced motion.
- [ ] A shared scenario (`interface/scenarios/`) with a group that finishes live, plus frame tests in TUI
  `tests/frames.rs` and an NSH test using a fake clock. Ledger row: ported from old Nexus.
- [ ] Both clients: the mark is `S` / `Q` (decided) in primary text, not accent and not bold, and NSH's rows are
  rebuilt to match the TUI's layout (one card, the mark two cells left of the text, the same hint); the selected or hovered row's text goes to primary.
- [ ] TUI: hit targets per row (`Target::PendingRow(i)` and `Target::PendingAction(i, Edit|Up|Down|Remove|Steer)`),
  drawn right-aligned in the card only while the row is hovered (`Cx::hovered`, as the sidebar's hovered-row `×`,
  `ui.rs:541`) or selected; glyph buttons (`✎ ↑ ↓ ×`, ASCII `e ^ v x`) with the keyboard hint kept for the selected
  row. Click runs the same action as the key.
- [ ] NSH: same order and glyphs as the TUI (edit, up, down, remove, then Steer for a queued row); use `arrow-down`
  rather than `chevron-down` so up and down match; disable up on the first row and down on the last.
- [ ] Tests: TUI behaviour test clicking each action (`tests/behaviour.rs`), NSH browser test hovering a row and
  clicking each action; the existing interrupt-returns-the-queue tests still pass.
- [ ] A side-by-side review of both clients against the shared scenarios (`interface/scenarios/`) in dark and light,
  at TUI 80×24 / 120×40 / 160×48 and NSH 390 / 1024 / 1440 px; list every misalignment, inconsistent spacing,
  wording or state and fix them in pairs.
- [ ] Empty, loading, error and offline states on every surface (sessions, files, settings panes, pickers,
  new-session view).
- [ ] Keyboard-only paths and focus visibility in both; decide the focus-ring colour.
- [ ] Motion: one set of durations (flash, fold, spinner, modal open) shared through `theme.toml`, all off with
  reduced motion.
- [ ] Copy pass: sentence case, one verb per meaning (close / back / cancel), no developer notes in the UI.
- [ ] **Keyboard avoidance.** `keyboardFrame()` is used only by Connect (`connect.tsx:68`); the composer dock
  (`composer.tsx:364`) pads only `safeArea().bottom`, so the keyboard can cover the composer; rename, new-session,
  palette and search modals have the same gap. Add the keyboard height to the dock and anchor modals above it.
- [ ] **Desktop affordances in modals.** The shared pass removed the title's Esc chip, but footers and the composer
  still show key hints, and there is no touch close button. Modals retain desktop padding (`p-6`), centred
  placement and a 70% height cap. On touch: a 44 px close button, no key hints, bottom sheets.
- [ ] **Touch targets under 44 px:** composer selectors `h-7` (`composer.tsx:563`), working row, location strip and
  chips (`:633`, `:781`, `:807`), attachment remove `size-8` (`:405`), new-session toggles (`newsession.tsx:193`,
  `:216`), `project-mcp.tsx:23`, Settings rows and pills (`settings.tsx:497`, `:582`, `:600`), `Seg`
  (`kit.tsx:161`), sidebar chips (`sidebar.tsx:320`), header tabs. Add one `hitMin()` helper keyed on `touch`, not
  `isNarrow()`, so an iPad gets touch sizes too.
- [ ] **Conversation header** shows only back, title and the panel button: add project/branch as a subtitle, an
  overflow menu (rename, archive, context) and swipe back.
- [ ] **Gestures:** long-press on session rows for the menu, swipe actions (archive, rename), pull to refresh on
  Sessions.
- [ ] **Settings on the phone** is the desktop two-pane dialog collapsed (`settings.tsx:518–580`): make it a
  grouped list with drill-in rows and keyboard-aware scrolling.
- [ ] **Connect flow** (`connect.tsx`, `App.tsx:73–79`): the copy asks for `NEXUS_HOST=0.0.0.0 nexus serve`, which a
  phone user cannot act on; add a recent-Cores list, paste or QR, a clear "cannot reach (Local Network permission?)"
  error, a spinner while booting (today a blank background), and an offline banner with Retry.
- [ ] **Microphone:** no in-app capture on native (toast at `commands.ts:586`); check
  `NSMicrophoneUsageDescription` in `scripts/native-info.mjs`; the Manager orb uses a hard-coded 84 px offset
  (`manager/stage.tsx:59`) instead of the real tab bar height.
- [ ] **Leaks from desktop:** hover states with no pressed state (add `active:` styles), shortcut text in the palette,
  pickers and switcher, effort hidden on narrow with no phone alternative (`composer.tsx:468`).
- [ ] **Layout:** horizontal safe-area insets for landscape, safe area inside drawers (`overlays.tsx:340`) and
  modals, Dynamic Type / text scaling (fixed `text-[16px]`).
- [ ] **iPad:** `touch` is true but `phone` is false, so it gets the desktop layout with hover affordances; decide
  the iPad design.
- [ ] **Docs:** `nexus-nsh/README.md` says session rows are 56 px (code: 72 px) and its Connect text omits the Local
  Network permission.
- [ ] **Tests:** Playwright tests at a 390 px viewport with `platform.idiom` stubbed to phone, once NSH is back in CI.
  Then review on a real device with screenshots before and after.

### 9.7 Both clients (old 8.1)

- [ ] **Audit navigation, pickers and panel interactions** across the clients (the 11-02 audit covered tool
  groups, palette, child rows, banners, rail, footer, composer, starting context, waiting cards and notices).
- [ ] **Ctrl+R prompt history search** in both clients (in the TUI `ctrl+r` is bound to `app.reconnect`,
  `app.rs:8162`, so pick the binding first; Up/Down recall exists in `composer.rs:69`); verify cross-client per-project recall after restarting
  both, including multiline drafts and images.
- [ ] **Subagent parent card:** model/effort, elapsed time and completed token count; distinguish background
  completion from the start result.
- [ ] **Background shells and archive** (was TODO 3). The count is shown in the location strip (11-02).
  Auto-archive skips a tree with a live background job, but manual `archive_session` (`runtime.py`) does not
  refuse one; make it refuse with a clear message, as it does for a busy kernel, and show why in both clients.
- [ ] Review both clients side by side in light and dark; establish large-session performance budgets; test
  keyboard-only paths; decide the focus-ring colour.
- [ ] Agent cards show the file path but cannot open it (the editor reads project files only; global and built-in
  agents live elsewhere).
- [ ] Show the Claude limits in the timeline: a "buffered, no thinking" note for a Claude model and a clear image
  failure (the error text exists; check how each client renders it).

### 9.8 NSH (old 8.2)

- [ ] **Design parity:** files (done: expanding tree, kept expanded state), sidebar rows now show the branch, the
  linked checkout's folder, or the project name in the one-project list. Left: new-session view and narrow-layout
  states in the terminal compared frame by frame; sidebar search handling and live shimmer; verify Monaspace Argon
  prose rendering natively. Then build the macOS desktop bundle and complete files, Git and worktree UI (section 4).
- [ ] Make NSH tool-result images zoomable.
- [ ] Native mic: in-app capture stays web/TUI until the NSH runtime gains audio input; the mic button points to
  iOS keyboard dictation meanwhile.

### 9.9 Settings (old 8.3)

- [ ] Sign in to a real device-flow and a real browser-flow provider from each client's Providers pane.
- [ ] Complete NSH suite coverage after the mock-stream/toolchain fixes, real PTY screenshots,
  and provider sign-in/detail and tool-grid verification.
- [ ] State the required Claude Code version (2.1.248 or newer) and show "Claude Code is not installed" on the
  Providers page when `cli_path()` is empty.
- [ ] Real Settings panes for MCP servers, skills and hooks when those controls exist; they are deliberately absent.

### 9.10 NSH Void design leftovers (old 8.4)

- [ ] **Fold the starting context once a session has messages?** Default: leave it open and delete leftovers if
  any reappear; the alternative is folding to `Context · 5 items · ~5.4K tokens` in place. See
  [Decisions](#decisions-still-open).
- [ ] **Accent equals primary text,** so links look like plain text. Default: underline links in prose.

### 9.11 TUI (old 8.5)

- [ ] `path:line` and OSC 8 link hit regions in the timeline (NSH already opens prose links). Checked 2026-11-04:
  no OSC 8 output and no link `Target`; Markdown links render as plain text with the URL (`markdown.rs:947–965`); choose a
  syntax-highlighting library for the file view (or reuse the vendored editor's Tree-sitter).
- [ ] Split-diff continuation alignment (`nexus-tui/src/diff.rs`); line numbers for edit/patch diffs whose input
  lacks them.
- [ ] PTY tests at 80×24 and 160×48 for anchor, follow and jump-to-latest; a fixed-fixture review gallery
  (`interface/review.md`).
- [ ] Timeline mouse selection: selection across scrolling, copying the underlying unwrapped text, OSC 52 in real
  terminals.
- [ ] Session info: failed child count, enabled/disabled tools and kernels.
- [ ] OSC 777 notifications in kitty (possibly OSC 99) and focus tracking; debounce completion requests if large
  workspaces make search slow.
- [ ] Verify Cmd+arrow composer shortcuts in a terminal that forwards Super-modified keys.
- [ ] Compare with old Nexus in a real terminal and review screenshots.

### 9.12 Project rail order: verification (old 8.6)

The feature landed on 2026-11-06 (Core order, move API, drag and menu in both clients). The old plan said not to mark it landed until verified; this is that verification.

- [ ] **Core order and synchronization.** Persist a user-defined ordering of visible project IDs in Core, initially
  equivalent to creation time then ID. Append new projects; remove deleted projects; never expose the Manager's
  hidden project. Preserve existing project `created`/`updated` and revision semantics. Include the order in the
  consistent `/api/snapshot` and publish a durable global event on changes, with reducers in both clients; ensure
  snapshot watermark and event ordering cannot lose a move. Keep older snapshots/servers usable with the existing
  creation-order fallback, and surface unsupported or failed mutations rather than pretending they succeeded.
- [ ] **Move API.** Define and document an atomic, idempotency-keyed move by project ID and intended neighbor
  (including an explicit end position), applied against Core's current order rather than replacing a stale whole
  list. Specify validation, no-op/self moves, concurrent moves, deleted or hidden IDs, retries, and how clients
  reconcile failures or a disconnected/reconnecting Core. Make ordering and event publication follow the existing
  mutation/snapshot consistency guarantees; test restart and replay rather than assuming them.
- [ ] **Shared interaction.** In both clients, drag a tile and display the insertion position; release commits,
  while Escape or dropping outside the valid rail cancels. A click without a drag still selects/toggles the
  project. Support moving to either end and through a scrollable/overflowing rail, without confusing the add or
  Settings buttons for projects. Support touch in NSH without interfering with scrolling or accidental selection.
  Add accessible **Move up** / **Move down** project-menu actions in both clients (including disabled boundary and
  offline states), with shared manifest commands and keyboard access. Keep initials assigned in creation order,
  independently of display order. Handle resize, project deletion and incoming reorder during an active drag.
- [ ] **Verification and docs.** Add Core store/API/event tests for migration/default order, create/delete, hidden
  projects, restart, snapshots, replay, concurrency, idempotency and failure handling; add shared scenarios and
  interaction tests for both clients (click vs drag, cancel, boundaries, menus, scrolling and sync between clients).
  Run Core tests, TUI tests, NSH web tests and manifest checks; capture and compare matching TUI/NSH screenshots,
  including a drag target. Update both READMEs, `docs/api.md`, `docs/daemon.md`, and the porting ledger if old
  Nexus behavior changes. Do not mark the work landed until it is verified.

### 9.13 Extensions, skills, MCP and hooks (old 9)

- [ ] Show agent and user-tool errors in client pickers and Settings; a visible changed-file notice for a running
  tool; process memory limits; validate project-only names in `[tools] disabled`. Exercise a user tool in a real
  session and run the reload stress test.
- [ ] Default-on settings for skills; exercise skills with a real model; decide whether and how `allowed-tools` is
  enforced without changing a cached prompt or granting permissions.
- [ ] MCP resources and prompts; Settings screens for adding/editing servers. Output stays bounded, fenced as
  untrusted and visible as received.
- [ ] Exercise browser-use (the pinned `uvx` package, `nexus-browser/`) in a real model session, including server
  failure, screenshots and its LLM-backed tools.
- [ ] Verify the new-session inventory prefers an enabled project MCP row over a same-named disabled global row
  (the `browser-use` case).
- [ ] Hooks: edit them in Settings, render hook rows in both timelines, fire `SessionEnd` on archive; test
  `bash(...)` matchers on compound commands and `PostToolUse` after a denied approval; a blocking hook with a real
  model.

### 9.14 Providers (old 10)

The ZMX item is section 6.

- [ ] **Real OpenAI and Ollama calls** (also in section 2).
- [ ] **Claude (Agent SDK) remaining:**
  - record the Claude Code CLI version in the wire request (hidden additions vary: ~175 more tokens on 2.1.314 than
    on 2.1.285);
  - decide whether to show the SDK's hidden additions (`sdk_adds`: two system lines, `StructuredOutput`, the
    account's email in the first user message, an `# Environment` message) in the clients' starting context, and
    whether sending the email is acceptable; the 420 tokens of fixed overhead are unexplained;
  - confirm the context window the clients show for `claude-haiku-5-5` (the CLI logs `unrecognized_model` but
    serves it);
  - confirm cancelling a turn kills the worker's process group and the CLI child;
  - re-check in NSH; mark the provider `verified` after a multi-tool turn with a real model and add wire fixtures.
- [ ] **J4 Anthropic Messages:** the Anthropic SDK as a plain client (Nexus owns the loop); preserve signed thinking
  blocks for lossless replay; cache-control breakpoints and tool-result media.
- [ ] **J5 Gemini:** a dialect that preserves replayable reasoning.
- [ ] **ZMX mode (subagents only, off by default):** port the Claude Code and Antigravity workers from `nexus01`. See
  [ZMX_plan.md](ZMX_plan.md) (user direction 2026-11-04; replaces the earlier SDK and `opencode-antigravity-auth` idea).
- [ ] Every provider needs wire fixtures, provider/auth docs and a verified real call; group OpenAI-compatible ones.
- [ ] Cross-provider conversion beyond text and tool calls (foreign reasoning is dropped); Copilot Responses
  encrypted reasoning replay.
- [ ] Revisit Responses `instructions` if the Codex backend changes (it accepts it, though the Codex CLI sends a
  developer message).

### 9.15 Worktrees (old 11)

- [ ] Port worktree review and integration as control commands: review against the base, merge by fast-forward or
  merge commit with confirmation, remove the worktree and branch after merge. Commands in both clients; agents get
  them only when granted. Reference: old `nexus/agents/worktree_review.py`, `worktree_integrate.py`,
  `nexus/worktree_cleanup.py`, `tests/test_worktree_merge_permission.py` (in `~/repos/nexus-old`).
- [ ] Decide cleanup on archive/delete (archive must never implicitly delete a worktree) and how to leave one.

### 9.16 Reliability and safety (old 12)

- [ ] Monty: an OS-level sandbox if wanted; checkpoint recovery; forced worker crashes; cold-start latency; the
  Linux `/usr/bin/env` path. (Workers already start through a launcher with an empty environment.)
- [ ] Supervise and report background bash jobs across daemon crashes; never kill a process from a stored PID
  alone.
- [ ] Durable pending directory moves and worktree-in-use checks for external processes; archive transitions
  appear only in the root log today.
- [ ] Archive: bulk/project operations or timestamps, if needed.
- [ ] Export holds a long SQLite read snapshot for very large sessions; consider general artifacts and export
  clipping.
- [ ] Whether read-only agents need sandboxing or restricted command execution beyond the file tools, including
  races with external writers.
- [ ] Retention: deferred partial indexes still cost one initial table scan each. Space reclamation and event
  compaction are section 13.

### 9.17 Event storage (old 13)

Decided 2026-11-04: finished sessions' events are compacted, announced by a `session.compacted` record. The offline scratch copy went 178.2 MB to 2.37 MB; deployment to the real home is the explicit follow-up.

- [ ] **13.0 Deploy what is committed.** Reinstall the tool from the checkout and restart the daemon (the first start
  runs the in-place migrations: 0.3 s on the copy). Done when `nexus doctor` no longer reports different code and
  `/api/info` `retention_status` shows wire payloads expiring. Restarting also rotates the daemon token, which was
  printed into a tool log during this investigation.
- [ ] **13.4 Move the remaining history readers onto state tables.** State tables and inspector Steps/Questions landed. Legacy `stream_summary` and `turn_end` recovery fallbacks remain for pre-checkpoint requests during the transition; remove after one release. Full NSH visual verification remains blocked by the native dependency mismatch.
  Original scope: Question/permission asked/replied into a small
  `wait` table (also serving the inspector's Questions card); background jobs into a `bash_job` table, replacing
  `open_bash_jobs` / `unfinished_bash_jobs` (which also removes the cold-start full scan in section 6); `code_used`
  becomes a session flag; inspector Steps reads the `turn*` tables (add `http_status` and `ttfb` columns if
  missing); drop the legacy `turn_end` fallback after one release. Files: `store.py`, `recovery.py`,
  `continuation.py`, `runtime.py`, `server.py`, `inspector/index.html`, `docs/inspector.md`. Done when
  `grep "from event"` hits only replay, the outbox and maintenance, and the recovery suites pass with the `event`
  table emptied for terminal sessions.

### 9.18 Inspector hardening (old 14.1)

The native inspector first pass landed 11-06; these are the remaining gates.

- [~] **Shared navigation and readable views.** (Clients landed in TUI and NSH: Questions view, per-view state kept across refresh/view/session switches, tree selector, `q`/`kind`/`status`/`type` filters, `summary` rows with expandable `detail` and on-demand `/inspect/record`. Pending: links from Questions into the kernel/job modal.) Preserve selection, zoom, expanded details and scroll
  while refreshing or switching back. Add a session/tree selector and search/filter by request, tool,
  status and event type. Render structured summaries with expandable exact JSON rather than making
  long JSON dumps the only way to understand Conversation, Steps or Requests. Bring pending/replied
  Questions and links to existing kernel/job details into the native inspector. Keep canonical model
  context distinct from diagnostic partials and resource views.
- [~] **Incremental live data.** (Clients landed: `/inspect/head` poll every 2 s only while open, refetch only on head/generation change, one request in flight, stale responses ignored, Live/Paused/Disconnected state with manual Refresh. Not done: using the SSE session events instead of polling, durable-cursor replay.) Use existing session events to invalidate only affected inspector data;
  coalesce refreshes and cancel/ignore stale requests on navigation or close. Reuse durable cursors and
  explicit snapshot resync on overflow/retention changes. No second unbounded event log, no repeated
  full-tree fetch per token, and no background refresh while the inspector is closed. Keep a visible
  paused/live/disconnected state and manual Refresh; an offline view must not look current.
- [ ] **Stable, bounded loading.** (Core landed: inspect API v2 with opaque cursors, byte/lane/tree bounds, `omitted`, `/inspect/record`, `/inspect/head`. Clients landed: cursor stack paging, `omitted` and notes shown, at most 60 rows drawn around the selection. Client memory/render-latency regression checks still pending.) Replace offset-only paging with documented stable boundaries/cursors
  so new turns cannot duplicate or skip older pages. Bound server queries and response bytes, avoid
  loading every message to show one page, virtualize long client lists and load large JSON on demand.
  Announce omitted sessions/lanes/bytes and expired diagnostics; provide a way to inspect the omitted
  records, not just a permanent truncation notice. Profile a large seeded session/tree before choosing
  budgets, then commit repeatable request-count, memory and render-latency regression checks.
- [~] **Cache accounting.** (Clients landed: lifetime vs displayed page, coverage, denominator, per-model rows, writes unknown. Selected-call totals are in lane details only.) (Core landed: `cache`/`cache_range` with coverage and per-model totals; cache writes stay *unknown* because the Claude dialect folds `cache_creation_input_tokens` into `input_tokens` and only reads are stored. Keeping writes needs a versioned `Usage`/`turn_request`/`session_usage` change, not done.) Keep lifetime/session, selected-call and displayed-range totals separately
  labelled. Show per-model breakdowns, explicit unknown/partial coverage and the exact hit-rate
  denominator; do not double-count retries, child sessions or cumulative provider updates. Investigate
  provider-reported cache-read/cache-write fields end to end before adding storage. Only show cache
  writes, TTL/expiry or cost savings when actually measured/supported; otherwise retain unknown, and
  never infer provider cache validity from a recent request timestamp. Version any codec/storage change
  and keep old sessions readable.
- [~] **Gantt v2.** (Clients landed: shared axis, lanes grouped by session with stable ids, fixed ruler, fit/zoom/pan, first-byte marker only when recorded, "end unknown". Not done: collapse lanes by group, nested `code` call timings.) Retain one shared axis and stable session/request/call IDs. Add useful lane grouping,
  collapse/filter, fit-to-range, a clear ruler, selected-range zoom and details linking back to the exact
  request or tool. Show retries, waiting intervals and TTFB markers only where recorded; label unknown
  spans. Capture nested `code` call timings prospectively if feasible, with monotonic duration and wall
  timestamps, not reconstructed guesses. Preserve selection as new rows arrive. Verify overlapping
  subagents, zero-length operations, failures, interruptions, long idle gaps and clock changes.
- [ ] **Exit gate.** Shared scenarios and Core/client tests cover refresh races, pagination under writes,
  retention/resync, cache totals and truthful timing. Compare screenshots and keyboard/mouse behavior
  in both clients, including narrow layouts and large histories. Run a real local Core smoke test only
  in an isolated home; no provider call is required. Update both READMEs and the inspector/API docs.

### 9.19 Terminal and browser: shared resource contract (old 14.2)

Supports section 2 (terminal). The browser parts are deferred.

- [ ] **Resource model and authority.** Define Core-owned terminal/browser resources with opaque IDs,
  project/workspace identity, creator, optional session association, status, timestamps and explicit
  create/attach/detach/close actions. Clients are viewers/controllers, not process owners. A session
  changing directory must not silently move an existing shell or browser; new resources use the chosen
  workspace and existing ones show their actual location/association.
- [ ] **API and streaming design.** Specify authenticated resource metadata/control endpoints and a
  versioned data stream, separately from the conversation SSE log. Include sequence numbers, bounded
  replay/checkpoints, explicit gap/resync messages, backpressure and read-only attachment. Keep raw
  PTY output and browser frames out of durable session-event rows. Input actions need IDs/acknowledgments
  so reconnect/retry never repeats a command, click or form submission.
- [ ] **One active writer.** Allow multiple viewers but give only one user/client/agent explicit control
  at a time. Show who owns control; handoff/revocation is explicit and auditable. Reject stale resize,
  pointer and keyboard input from an old attachment. A disconnect releases control according to the
  documented lease policy; it never silently transfers control to an agent.
- [ ] **Lifecycle, limits and security.** Decide idle limits, concurrent-resource limits, archive/delete
  behavior and graceful shutdown before coding. Closing a panel is detach, not kill; explicit Close
  terminates a resource. For the first version, a Core restart may end processes/contexts but must mark
  them ended/interrupted, never pretend to resume them. Test orphan cleanup. Guard stream origins,
  attachment authorization and cross-project access; do not expose a remote shell or authenticated
  browser over the existing tokenless non-loopback mode without an approved access model.
- [ ] **Visible context boundary.** A terminal/browser panel is a workspace resource, not another chat
  timeline. Opening it or viewing its output does not automatically feed the model. Explicitly attached
  output, page extracts and screenshots become normal visible messages/tool results with provenance,
  clipping and untrusted-content treatment. Separate UI resource-management APIs/control tools from
  workspace task actions; do not add task tools that mutate Nexus sessions, models or workspace state.
- [ ] **Shared shell placement.** Design a shared workspace panel with Terminal/Browser tabs, a resource
  list, split/expand/collapse and focus return, using the existing file-panel/layout machinery where
  appropriate. Keep the session composer draft and scroll unchanged when switching. Put commands and
  shortcuts in `interface/manifest.toml`; arbitrate terminal/browser input versus global shortcuts, IME,
  mouse, copy/paste and Esc without sending a keystroke to both the resource and Nexus.

### 9.20 Integrated terminal MVP (old 14.3)

The detail behind section 2.

- [ ] **Core PTY service.** Add a dedicated resource service (proposed
  `nexus-core/nexus/terminals.py`) for shell spawning in the selected workspace, terminal dimensions,
  environment, process groups, binary-safe I/O and resize. Preserve UTF-8 split across chunks and
  arbitrary terminal control bytes. Set a defined terminal type; support signals, exit codes and
  graceful-close/force-stop. Do not repurpose `bash_output`'s combined text buffer as a PTY.
- [ ] **Shared terminal semantics.** Choose a maintained VT parser/screen checkpoint representation
  and compatible renderers for NSH and Rust. Verify colors, Unicode/wide characters, cursor movement,
  scrollback, alternate screen, bracketed paste and interactive programs (`less`, an editor, a shell
  prompt). Bound scrollback and queued bytes; announce discarded history. Disable or gate terminal
  escape features that write the clipboard, access files, or launch URLs without the user's action.
- [ ] **User interaction.** Open/list/rename/switch/close terminals; show cwd/workspace, running/exited
  status and active controller. Support selection/copy/paste, resize, Ctrl+C and focus handoff. Warn
  before terminating a running shell/process. Detach/reconnect restores a bounded screen checkpoint
  plus subsequent bytes without replaying input. First version is user-operated; agent raw typing is
  deferred until the authority model in 14.5 is settled.
- [ ] **Exit gate.** Tests run real PTYs against an isolated workspace: shell input/output, interactive
  alternate screen, resize, split byte sequences, high-volume output, two clients, control contention,
  disconnect/reconnect, exit, close and Core shutdown. Compare both clients' screens and input behavior.
  A terminal is not done if it only renders line-oriented command output.

### 9.21 Integrated browser MVP (old 14.4), deferred

Deferred (not in this plan's scope). Kept whole.

- [ ] **One actual browser, not two unrelated renderings.** Choose a Core-owned managed Chromium context
  per workspace/browser resource. Prefer displaying that same page through a bounded frame stream and
  shared input/navigation API in both clients; investigate NSH native/web embedding only if it preserves
  the same page identity and automation target. Do not use an arbitrary-site iframe as the architecture:
  frame restrictions, cookies and cross-origin rules make it an unreliable browser.
- [ ] **Browser worker and profile isolation.** Keep browser launch/render/automation off Core's main
  event loop. Use dedicated Nexus profiles with a documented lifetime and user-controlled clearing;
  never attach a personal Chrome profile or import its cookies by default. Handle engine missing,
  launch failure, page crash and worker loss visibly. Propose installation/download UX before adding
  automatic downloads. Keep browser binaries and cache out of session storage.
- [ ] **Minimum complete navigation.** Address bar, back/forward, reload/stop, tabs, active URL/title,
  loading/error state, scrolling, click, keyboard/text entry, selection and explicit Open externally.
  Handle redirects and popups without losing track of the active page. Coordinate input with frame
  revision/viewport/DPI to prevent stale-frame clicks and show when a frame is out of date. Use a
  responsive stream rate with idle suspension and bounded queues, not screenshots in the event log.
- [ ] **Local previews and safe boundaries.** Support a dev server on the Core machine, including when
  NSH is remote; explain that `localhost` is Core's, not the phone/browser host's. Reuse existing network
  checks where applicable and explicitly define the separate opt-in policy needed for local/private
  addresses. Do not weaken `web_fetch`'s public-only policy. Gate file URLs, external protocols, file
  uploads/download destinations, camera/microphone/location, clipboard and popups; reject access outside
  the approved policy. Existing authenticated pages may contain private data: attaching their contents
  to a model is always an explicit, visible action.
- [ ] **Reuse automation rather than launch a second browser.** Review the pinned browser-use MCP
  integration in `nexus-browser/`. Establish whether it can target the exact managed context/tab, with
  stable IDs and explicit control ownership. If it cannot, design an adapter or expose the limitation;
  do not show one page while tools operate another. Preserve current MCP users and keep any LLM-backed
  browser helper's model access, data sharing and cost disclosed and opt-in.
- [ ] **Exit gate.** Use a local fixture website (no external sign-in) with navigation, forms, scrolling,
  tabs, redirects, downloads and deliberate errors. Verify the exact same page state from NSH, TUI and
  the automation adapter; test remote-Core localhost access, reconnect, stale-frame input, profile
  isolation and denied capabilities. Compare screenshots and interaction tests in both clients.

### 9.22 Agent handoff and release gate (old 14.5)

Terminal evidence capture and agent-control policy apply to section 2 as well.

- [ ] **Explicit evidence capture first.** Add user actions to attach selected terminal output, a page
  extract or a screenshot to the current draft. Preview the exact payload, source resource and omitted
  bytes/lines before sending. The user's normal send commits it into canonical context; live panel
  refreshes must never silently replace content already sent to a provider.
- [ ] **Agent control only after policy review.** Classify terminal commands, raw keystrokes, browser
  navigation/form actions and credential-bearing operations. Prefer existing approved `bash` and MCP
  paths where they suffice. A raw interactive shell cannot reliably enforce per-command allow patterns;
  do not present those rules as protection or turn approval of one command into indefinite shell access.
  Define grant scope/revocation, destructive actions and takeover behavior, with visible tool arguments,
  outcomes and failures in the ordinary timeline. Browser content and terminal output remain untrusted
  data, never instructions or implicit permission.
- [ ] **Diagnostic integration.** Link resource actions and captured evidence to session/turn/tool IDs
  when they actually belong to one. Add recorded browser/terminal spans to the inspector Gantt without
  inventing model calls or token usage. Keep full streams private/bounded outside the timeline; retain
  only the explicit model-visible payload and bounded diagnostic metadata under the existing policies.
- [ ] **Release gate.** Core store/API/access/lifecycle tests, shared scenarios, TUI tests, NSH browser
  tests, manifest/theme checks and both builds pass for the changed paths. Run matching screenshots,
  failure/reconnect tests, high-output/large-page profiling and a cross-client handoff demo. Document
  unsupported platforms/capabilities and approved differences. Update both client READMEs, API/daemon/
  tools/inspector/MCP docs and the ledger where behavior changes. Land in vertical slices, each with a
  usable backend and both clients; no mock terminal/browser can be marked MVP complete.


## Landed

Records before 2026-11-06 are in [docs/archive/PLAN-2026-11-06.md](docs/archive/PLAN-2026-11-06.md#landed).

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
