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
| 4 | Tests and lints | **In progress (11-06).** Core green (1200 passed, 1 skipped); TUI and NSH below | [4](#4-fix-all-tests-and-lints), do this first |
| 1 | Unmerged work | Local `main` is **34 commits ahead of `origin/main`, unpushed**; PR #100 open; PR #99 looks obsolete | [1](#1-merge-what-is-unmerged) |
| 5 | Mobile cut-down client | Not started; phone build still renders the full timeline. Decided: mobile can approve and answer anything a client can | [5](#5-mobile-cut-down-client-sessions-manager-settings) |
| 6 | ZMX mode (Claude Code, Antigravity) | **Later (user, 11-06).** Plan only, nothing built; kept here for reference | [6](#6-zmx-mode-for-claude-code-and-antigravity-later) |
| 3 | Manager, finished and tested end to end | M1–M5 built; real GPT-Live call worked 11-04; evals, long calls, iOS/native audio and hands-free open | [3](#3-manager-finish-and-test-end-to-end) |
| 2 | Terminal inside Nexus | Spikes done (`reports/section14/spikes.md`); no product code | [2](#2-terminal-inside-nexus) |
| 7 | Everything else carried over | Listed, not scheduled | [7](#7-carried-over-not-scheduled) |

Suggested order: 4, then 1 (so the release PR can merge on a green tree), then 5, then 2 and 3 in whatever order the
user prefers (3 needs real-model approvals). Section 6 (ZMX) is for later.

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

Measured 2026-11-06 on `main` plus the modal revert. Fix each cause; do not just update expectations (decide whether the
code or the test is wrong).

| Suite | Result | Work |
| --- | --- | --- |
| Core `uv run pytest -q`, `ruff check nexus tests`, `uv lock --check` | **1200 passed, 1 skipped** (162 s) | [ ] Re-run `ruff` and `lock --check`; watch `test_server.py::test_code_nested_write_needs_no_approval_and_kernel_api` (one flake seen under load) and the long `test_archive.py` hangs seen under heavy load |
| TUI `cargo fmt --check` | Fails: 5 diffs (`live.rs`, `manager.rs`, `dictation.rs`, `render.rs`, `tests/manager.rs`) | [ ] `cargo fmt` |
| TUI `cargo clippy --all-targets -- -D warnings` | Fails: 10 errors | [ ] `is_multiple_of` ×3, `div_ceil` ×1, identical `if` blocks ×2 (`app.rs`, `render.rs`), `chunks_exact` constant size ×2 (`live.rs`, `speech.rs`), `iter().any()` → `contains()` ×2 |
| TUI `cargo test --no-fail-fast` | **12 fail** | See below |
| NSH `nsh check` | Known errors fixed earlier (11-06); re-run | [ ] Re-run |
| NSH `nsh test --target web` | 277/279 on 11-06 | [ ] Fix the two failing tests (the `i7` picker-footer wording check is one; find the other) |
| `check_manifest.py`, `check_theme.py --overlay nexus-nsh/theme-void.toml` | Clean (184 commands, 602 contrast checks) | Keep clean |
| `nexus-mermaid`, `nexus-annotate` | Clean at last check | Re-run |

TUI failures to triage (code or test wrong?):

- `markdown::tests::old_renderer_keeps_links_tables_and_fence_source_visible`
- `shell::dark_frames_use_the_theme_file_surfaces` (surface `#0b0b0b`, test expects `#141414`)
- `shell::composer_rows_are_edge_input_blank_control_edge_strip_at_every_width`
- `shell::project_rail_labels_activity_and_session_restoration` (`T` where `N` is expected; the rail changed in 15.3)
- `shell::session_rows_follow_d03_at_sidebar_widths_22_28_and_34`
- `timeline::mouse_drag_selects_and_copies_timeline_text_without_activating_the_block` — **looks like a real
  regression in selection/copy; fix the code first**
- Manager header and dropdown tests in `tests/manager.rs`: `header_indicator_and_connecting_show_before_the_start_reply`,
  `hovering_the_live_voice_indicator_offers_stop_and_clicking_it_stops_the_voice`,
  `palette_context_modal_closes_before_the_dropdown`, `shared_dropdown_contract_keeps_text_audio_and_debug_separate`,
  `the_header_has_a_manager_button_left_of_the_right_toggle_that_opens_the_context`,
  `the_header_indicator_is_a_braille_wave_field_and_goes_on_end` (the 11-05 braille indicator and Stop-on-hover
  decisions are the likely cause; update the tests to the decided design unless a behavior is actually broken)
- (`a_viewed_subagent_keeps_the_root_tab_and_names_itself_below` passes now.)

Then:

- [ ] Re-run everything and write the result table here; record anything that fails only under load.
- [ ] Add the TUI replay bench result (draws were ~3× the earlier figure) to the table without gating on it.

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
- **Provider usage table (16):** ten-block gauge, percent left, reserve and expiry in both clients; fully specified.
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

## Landed

Records before 2026-11-06 are in [docs/archive/PLAN-2026-11-06.md](docs/archive/PLAN-2026-11-06.md#landed).

- **Modal backdrop back to translucent (11-06).** Reverted `0452ee80`: the TUI blends the page 50% toward
  `surface.canvas` (`paint::scrim`) under every modal again, NSH is back to its translucent `bg-black/65` backdrop and
  its own `Dialog`, the terminal-only modal block is gone, and the ledger and `interface/design-language.md` say so.
  Verified: paint unit tests, manifest and theme checks; the TUI suite shows the same failures as before. Not
  re-captured in a terminal screenshot.
