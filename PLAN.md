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
| 7 | Everything else carried over | Listed, not scheduled | [7](#7-carried-over-not-scheduled) |
| 8 | Provider usage panel (table) | Fully specified, not started; both clients | [8](#8-provider-usage-panel-usage-table) |

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

Open (each has a default that holds unless the user objects):

| Decision | Default |
| --- | --- |
| **Z1. ZMX scope (when ZMX is picked up).** `ZMX_plan.md` D1 says subagents only. The new hint ("does not use Nexus agents or tools") reads as if a user would pick ZMX for whole sessions | Keep D1: root sessions always use the SDK; the toggle picks the backend for Claude models a **subagent** uses |
| **Z2. Is the toggle per vendor?** Antigravity has no SDK route | Claude Code: `Agent SDK` / `ZMX`. Antigravity: a plain on/off, shown only in ZMX terms |
| Browser (old 14.4): still wanted? | Deferred. Only the terminal is in this plan; the browser stays in the archive |
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
- [ ] **Mermaid (step 0):** a real kitty/sixel/iTerm2 terminal and a narrow width; screenshots of the seeded
  `Mermaid diagrams` session in both clients, dark and light; rerun `scenarios.test.tsx` and `mermaid.test.ts`.
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

All details are in the archive; section numbers are that file's. Pick these up deliberately.

- **File explorer, editor, Git page (4):** wire the vendored editor into the TUI, find/replace, explorer file actions and
  the watcher, the read-only then full Git page. Desktop and web only.
- **Providers (10):** real OpenAI and Ollama calls, Anthropic Messages, Gemini, Claude SDK remaining items.
- **Worktrees (11):** review and integration commands; cleanup on archive or delete.
- **Event storage (13):** deploy the committed compaction, move the remaining history readers onto state tables.
- **TUI performance (6):** cold-start log scan, the 1 s image probe, draw cost growth, viewport-only assembly.
- **Agent loop (7):** fuzzy `edit`, a soft nudge near the step limit, the token-gap investigation, `/btw` mid-turn.
- **Interface polish (8.0–8.6):** modal footer audit and scroll model, tab/subagent spinners, group fold, pending-row
  actions, Ctrl+R history search, subagent parent card, project drag-reorder verification.
- **Extensions (9) and reliability (12):** skills, MCP resources, hooks UI; sandboxing, background job supervision, retention.
- **Real-device verification (2):** real daemon with both clients, OSC 7501, audio, drawing modal, iOS.
- **Browser (old 14.4–14.5), inspector hardening remainder (14.1):** deferred; see Decisions.
- **CI:** deferred by the user.

## 8. Provider usage panel (usage table)

The user's review of the Provider usage modal (screenshot, 11-06) asks for a table instead of the current
label + bar + prose rows: a ten-block progress gauge, then the percentage left, the reserve (the old Nexus
pace calculation) and the expiry time. The words marked in red go away. Both clients land this in the same
change; no desktop client-specific difference is expected. **Not started.** Carried over whole from the previous plan's section 16 (user asked to keep it, 2026-11-06).

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

- [ ] **Header row:** `Window`, `Progress`, `Left`, `Reserve`, `Expires`, muted, once above the first
  provider. Provider group rows keep the bold name and the muted plan (`ChatGPT (Codex)  Plus`); its window
  labels are indented two cells. Do not repeat the provider on every window.
- [ ] **Progress (the wide column, exactly 10 cells):** `used_percent` decides the fill in eighths:
  `e = round(used × 80 / 100)` (nearest, ties to even), then ten cells filled left to right, each cell
  holding `min(8, max(0, e − 8·i))` eighths. Full cells are `█` (U+2588); the boundary cell is
  `▁▂▃▄▅▆▇` (U+2581–U+2587), so it fills **from the bottom up** as requested; empty cells are `░`
  (U+2591). 24% → `██▃░░░░░░░`, 36% → `███▅░░░░░░`, 100% → ten `█`, 0%/unknown → ten `░`. The bar is
  horizontal because the table is one line per window; a literal ten-row-tall gauge would make every
  window ten lines high. Glyphs come from the block-elements range the user linked
  (`https://unicode-explorer.com/b/2580`).
- [ ] **Left:** `100 − used_percent`, following the old Nexus `percent()` rule: a whole percent, except a
  non-whole value under 10 or over 90 keeps one decimal (`0.2%`, `99.8%`); unknown is `—`. Right-aligned.
  This replaces the old used-percent column (42% used now reads 58%).
- [ ] **Reserve:** copy the `nexus01` calculation from `nexus/ui_support/usage.py`
  (`RESERVE_MIN_PERIOD`, `elapsed_percent`, `reserve`, `reserve_phrase`): only when `period_seconds` is
  at least 7 days and `used_percent` and a future `resets_at` are known;
  `elapsed = 100 × (1 − (resets_at − now) / period_seconds)`, `reserve = elapsed − used`. Round to the
  nearest integer with ties to even, as Python's `round` does (8.5 → 8). Show `+N%` for spare usage,
  `-N%` for over pace, `0%` for on pace and `—` when not estimable (five-hour windows, missing period, a
  reset already due). Right-aligned; spare takes the success colour, over pace the warning colour,
  `0%`/`—` stay muted. Do not show the old `┃` pace marker.
- [ ] **Expires:** the time until reset only, never an absolute stamp: `now` under a minute, otherwise the
  two largest units (`3h 10m`, `2d 4h`, `6d 5h`); `—` when `resets_at` is missing or the reset is due.
  Left-aligned. This is the old `resets in …` text without the words.
- [ ] **Colours and tone:** filled gauge cells keep the existing used-percent thresholds (success below
  70, warning below 90, error at 90 and above); empty cells use the dim border/muted role. The label and
  the Left number stay in primary text; the provider plan, header, `—` and notes are muted.
- [ ] **Words removed (the red marks):** drop the Codex `Limit reached: new requests wait for the next
  reset` note, the xAI per-product notes such as `GrokChat: 1% of credits`, and every per-window `detail`
  suffix (`225 of 300 left`, `20,280 over quota`, `· 20,28…`). `detail` stays in the API for other
  consumers but is never printed by this modal; when a window has no `resets_at`, Expires is `—`
  rather than the old raw-text fallback. Keep the other notes (credits balance/unlimited, no plan
  limits, `Could not read the limits: …`, `No limit windows reported.`) as muted full-width lines under
  their provider, plus `Updated … ago`, the empty and loading states and the `r refresh · esc close`
  footer.
- [ ] **Responsive rules:** the gauge and the three numbers keep their widths; the Window column flexes
  and truncates with `…`; Expires truncates with `…`. Never wrap a window row. The TUI keeps its
  80–88-cell large modal and 30-row cap. NSH uses its large modal as `tall` (90% of the window) so the
  table fits; on phone widths (a standing phone-layout exception) each window becomes two lines: label,
  then gauge, Left, Reserve and Expires.

### 8.2 Data and formatting

- [ ] `period_seconds` and `kind` are already in the API (`GET /api/usage`); parse them in both clients.
  The reserve is derived client-side from `used_percent`, `resets_at`, `period_seconds` and the client's
  `now`, beside the countdown; do not add a server endpoint or store a stale reserve in the snapshot.
- [ ] Unknown `used_percent` → ten empty cells, `—` Left, `—` Reserve, and the countdown if known.
  Missing `period_seconds` (older snapshots) → Reserve `—`, everything else unchanged. A reset at or
  before `now` → Expires `now`, Reserve `—`.
- [ ] Filter the removed notes in both clients as well as at the source, so an older saved snapshot
  cannot bring them back: Codex notes starting `Limit reached:`; xAI notes ending ` of credits` that
  are not `Prepaid credits:`. The existing xAI prepaid and Copilot chat/completions/AI-credit filters
  stay.
- [ ] Keep the snapshot semantics: the modal is a snapshot; countdowns and reserve do not tick while it
  stays open (the existing background-refresh follow-up remains separate).

### 8.3 Core

- [ ] `nexus-core/nexus/provider_usage.py`: remove the `limit_reached` note in `parse_codex` and the
  `productUsage` note loop in `parse_xai`; keep the credits and prepaid notes. Keep
  `parse_copilot`/`parse_claude` `detail` values (the API keeps them; the modal stops printing them).
- [ ] Update `nexus-core/tests/test_provider_usage.py` with a `limit_reached: true` payload and a
  `productUsage` list, asserting neither becomes a note; keep the existing prepaid and detail assertions.
- [ ] `docs/api.md` (Usage K11): say the modal is a table of gauge + percent left + reserve + expiry;
  `detail` is retained for other consumers but not shown; note the reserve rule and its 7-day threshold.

### 8.4 TUI (`nexus-tui`)

- [ ] `src/usage.rs`: extend `Row` with a `Header` row and make `Window` carry the label, the used value,
  the formatted Left, Reserve and Expires strings (computed from `now`). Parse `period_seconds` and
  `kind`. Drop the `detail` join from the reset text. Add pure helpers for `gauge_cells(used) -> [u8;
  10]`, `percent_left`, `reserve`, `reserve_text` and `duration` with unit tests at 0, 1, 9, 10, 24, 36,
  47, 68, 90 and 100 percent; five-hour vs weekly/monthly; a missing/old period; a reset in the past;
  unknown usage; and the half-to-even tie (8.5 → 8).
- [ ] `src/render.rs::draw_usage`: draw the header and the five fixed columns, map `gauge_cells` through
  the theme glyphs, colour only the filled cells, right-align Left/Reserve, left-align Expires, and
  truncate the label and Expires. Keep `MODAL_LARGE`, the height clamp and the existing `r`/`esc` hits.
- [ ] ASCII mode: ten `#`/`-` cells; a partially filled cell counts as `#` (rounded up). Colour rules are
  unchanged.
- [ ] `interface/theme.toml`: add `[glyphs.usage]` with `full` (`█`/`#`), `empty` (`░`/`-`) and
  `eighth_1`…`eighth_7` (`▁`…`▇`/`#`); add `usage` to the glyph checks in `interface/check_theme.py`,
  the `Glyphs` struct/accessor in `src/theme.rs`, and the generator in `nexus-nsh/scripts/gen.py`.
- [ ] `tests/behaviour.rs` (and the usage unit tests): assert the header, the gauge strings, `76%`/`0%`
  Left, reserve cells and the hidden `Limit reached` / `of credits` / `of 300 left` text.
- [ ] `nexus-tui/README.md`: update the K11 bullet to the table.

### 8.5 NSH (`nexus-nsh`)

- [ ] `src/store.ts`: add `period_seconds?: number | null` and `kind?: string` to the usage window type.
- [ ] New pure `src/usage-format.ts` (or exports beside `until`): `leftPercent`, `reservePercent`,
  `reserveText`, `gaugeCells` and `until` with the same rounding and threshold rules as the TUI.
- [ ] `src/ui/usage.tsx`: replace the flex bars with a monospace table: header row, provider group rows,
  window rows, 10-cell gauge drawn from the generated `usage` glyphs, fixed-width Left/Reserve/Expires
  columns and a flexing Window column. Mark the gauge `accessible={false}` and give each row an
  accessibility label with all four values. Use `Modal … tall`; keep the existing refresh/footer and the
  loading/empty/error states. Hide the removed notes in `notes()` for older snapshots.
- [ ] `usage.test.tsx`: update the expected percentages to the Left values (42% used → 58%), assert the
  header, a gauge string such as `██▃`, `—`/reserve cells, and the absence of `Limit reached`, `of
  credits` and the Copilot detail. Unit-test the pure formatting helpers with a fixed clock (including
  8.5 → 8 and the 7-day threshold).
- [ ] `nexus-nsh/README.md`: update the K11 bullet to the table.

### 8.6 Shared scenario and screenshots

- [ ] Extend `interface/scenarios/provider-usage.json` with the values above: Codex 100%/24%, Claude
  36%/68%, xAI 1%, Copilot 9%, OpenCode 0%/33%/47%, plus the notes that must disappear (`Limit
  reached: …`, `GrokChat: 1% of credits`), a provider with no plan limits and one with an error.
  Keep time-dependent assertions in the pure helper tests; the scenario test asserts labels, Left
  values, gauge glyphs and the hidden words.
- [ ] Add a usage fixture to `nexus-nsh/e2e/scripted_core.py` that returns the scenario with
  `resets_at`/`period_seconds` computed relative to the capture time (monkeypatch
  `nexus.provider_usage.providers_usage` or seed the usage cache), so both clients see identical rows
  without provider calls. Document the mode in the script's docstring.
- [ ] Capture the same state in both clients: TUI `--dump-frame` at 170x50 and 120x36, NSH web at a
  comparable window (plus one phone-width capture). Compare the header, column order, gauge glyphs,
  numbers, colours and the absence of the marked words; save under `reports/provider-usage-table/`.

### 8.7 Checks when it lands

- [ ] `cd nexus-core && uv run pytest tests/test_provider_usage.py -q && uv run ruff check nexus/provider_usage.py tests/test_provider_usage.py`
- [ ] `cd nexus-tui && cargo test usage && cargo test --test behaviour usage && cargo fmt --check && cargo clippy --all-targets -- -D warnings`
- [ ] `cd nexus-nsh && npm run gen && nsh test usage.test.tsx --target web` (plus the pure helper test and `nsh check`; record the known `app.clock` / `Button.onFocus` errors if they remain).
- [ ] `python3 interface/check_theme.py --overlay nexus-nsh/theme-void.toml && python3 interface/check_manifest.py`
- [ ] Update `docs/porting-ledger.md` on landing: "Provider usage showed a continuous bar, used percent and prose notes | Both clients show a five-column table: ten-block gauge filled bottom-up, percent left, reserve (old 7-day pace formula), expiry; the Codex limit-reached note, xAI per-product credit notes and per-window detail text are gone (2026-11-06) | User request". Record any screenshot-only difference; none is expected on desktop.
- [ ] Tick this section, move its one-line record to Landed and remove the status-table row's "planned" wording.

## Landed

Records before 2026-11-06 are in [docs/archive/PLAN-2026-11-06.md](docs/archive/PLAN-2026-11-06.md#landed).

- **Modal backdrop back to translucent (11-06).** Reverted `0452ee80`: the TUI blends the page 50% toward
  `surface.canvas` (`paint::scrim`) under every modal again, NSH is back to its translucent `bg-black/65` backdrop and
  its own `Dialog`, the terminal-only modal block is gone, and the ledger and `interface/design-language.md` say so.
  Verified: paint unit tests, manifest and theme checks; the TUI suite shows the same failures as before. Not
  re-captured in a terminal screenshot.
