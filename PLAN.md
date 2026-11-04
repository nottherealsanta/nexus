# Nexus plan

This is the one plan for Nexus 0.10.x: what has landed, and everything that is still to do. It replaces the
separate plan files and `TODO.md` (see [Archived plans](#archived-plans)). Current behavior is described in
[docs/README.md](docs/README.md); deliberate changes from old Nexus are in the
[porting ledger](docs/porting-ledger.md). The Manager's design is in
[docs/manager-design.md](docs/manager-design.md); its remaining work is tracked here.

Rules that every item below must keep:

- Preserve the visible, ordered context timeline: everything the model receives is shown in order, the UI shows
  nothing the model does not have, wire events are never dropped and clipping is announced. Do not rewrite context
  already sent to a provider. The Manager's own context is the one exception (`docs/manager.md`).
- Task tools act on the workspace; changes to Nexus state belong to the control plane.
- Every UI change lands in the TUI and NSH in the same change
  ([.agents/skills/nexus-ui-parity/SKILL.md](.agents/skills/nexus-ui-parity/SKILL.md)); a client difference
  needs the user's approval and a ledger row.
- The file explorer, file editor and Git page are for desktop and web only, never iOS or another mobile target.
- State is in `~/.nexus` after `nexus migrate`. Never run it against the real home from an agent session; tests use
  a scratch `$NEXUS10_HOME`.

How to update this file: when an item lands, tick it and move its one-line record to [Landed](#landed); when a
decision changes, update [Decisions still open](#decisions-still-open). Keep "not verified" honest: an item that
was only reviewed statically, or only tested offline, says so.

## Status at a glance (2026-11-04)

| Area | State | Next |
| --- | --- | --- |
| Release 0.10.0 | Published 2026-10-30 | Release PR #98 (`release 0.11.0`) is open; see [1](#1-release-and-installation) |
| **Tests and lint** | **Red locally:** 7 TUI and 3 Core tests fail, TUI fmt and clippy fail, 5 NSH browser tests fail | [0](#0-make-the-tests-green) first |
| CI | Red on `main`, NSH not in CI. **Deferred by the user (2026-11-04): do not work on CI for now** | [Deferred](#deferred-ci-user-decision-2026-11-04) |
| Turn summaries | Steps 0–6 and 8 done, 7 partly done | Real-daemon checks, docs, step 9 (live partial) — [5](#5-turn-summaries) |
| TUI render performance | Phases 1–3 done, phase 4 partly | Only if a profile asks for it — [6](#6-tui-render-performance-and-launch-time) |
| NSH Void design | Mostly complete; both passes done, since made square (radius 0) | Two open questions — [8.4](#84-nsh-void-design-leftovers) |
| Polish | TUI modals, Manager call mode, scroll model, spinners, group fold, iOS app | [8.0](#80-polish-user-review-2026-11-04) |
| Editor, explorer, Git page | Steps 0–1 done, step 2 in progress. The TUI app still edits in the plain `Composer` buffer; the vendored editor is used only by `examples/editor_demo.rs`. NSH's Files tab is one directory at a time; no Git tab in either client | Wire the vendored editor into the TUI — [4](#4-file-explorer-file-editor-and-git-page) |
| Manager | Core for M1–M5 built and tested offline; clients' stage, cards, orb, voice and `via Manager` built | M0 spikes and every real-model / real-audio check — [3](#3-manager) |
| Mermaid (Manager step 0) | Done in both clients | Real terminal image-protocol check, screenshots — [3.2](#32-mermaid-verification) |
| Settings | One two-pane dialog in both clients | Real sign-ins, screenshots, browser coverage — [8.3](#83-settings) |
| Providers | Codex, Copilot, Grok, OpenCode, Claude (Agent SDK) work | OpenAI and Ollama real calls; Anthropic Messages, Gemini — [10](#10-providers) |
| Event storage | Every event is durable; full request bodies with base64 images grow quadratically; the running daemon predates wire expiry; the file never shrinks | Deploy, reclaim, ephemeral deltas, dedupe requests, compact finished sessions — [13](#13-event-storage-harden-finished-sessions-into-messages-and-flush-their-events) |

## Health check (2026-11-04)

Run by subagents on HEAD `e438b61` with a scratch `$NEXUS10_HOME`; the working tree differed only in the plan
merge (comments and docs). Repeat this table when the tree is green again.

| Check | Result |
| --- | --- |
| Core `uv run ruff check nexus tests`, `uv lock --check` | Clean |
| Core `uv run pytest -q` | **3 failed**, 1124 passed, 5 skipped (285 s); see 0 |
| `nexus-mermaid` test, fmt, clippy `-D warnings` | Clean (31 + 1 tests) |
| `nexus-annotate` test, fmt | Clean (7 tests) |
| `check_manifest.py`, `check_theme.py` (and `--overlay nexus-nsh/theme-void.toml`) | Clean (141 commands, 576 contrast checks) |
| TUI `cargo test --no-fail-fast` | **7 failed**, 729 passed, 7 ignored; see 0 |
| TUI `cargo fmt --check` | **Fails**: `live.rs`, `manager.rs`, `dictation.rs`, `render.rs`, `tests/manager.rs` |
| TUI `cargo clippy --all-targets -- -D warnings` | **Fails**: 9 errors; see 0 |
| TUI release build | Builds; binary 19.4 MB (was 13 MB before Mermaid raster, annotate and editor crates) |
| TUI replay bench (160×48, 200 messages, release) | First frame 16.3 ms; burst 16 frames, draw mean 4.35 ms; paced draw mean 4.09 / p95 8.71 ms; chrome-only mean 1.73 ms; RSS 32 MiB. Draws are ~3× the 10-31 figures (1.2 ms paced, 0.36 ms chrome): investigate, see 6 |
| NSH `nsh check` | Clean (run without `npm run gen`) |
| NSH `nsh test --target web` (Chrome for Testing) | 157 tests: 152 passed, **5 failed**; 4 files cannot load `nexus/native`; Manager tests run and pass |
| GitHub `ci` on `main` | Last 5 runs failed: `lint` (TUI fmt), `core` (the Manager status test), `tui` |

Local machine problems found on the way (not repo bugs): Homebrew `node` failed to load `libuv.1.dylib` (fixed by
the user, 2026-11-04); Homebrew Python 3.14.7 cannot import `sqlite3` (missing `libsqlite3.dylib`), so `uv run pytest` errors at collection
with that interpreter (python.org 3.14.5 works); `~/.nsh/bin/nsh` is 0.2.0 while NSH needs the 0.3.0 source build at
`09c1632`; the wasm test build needs `llvm-ar` (`rustup component add llvm-tools`, `AR_wasm32_unknown_unknown`),
because Apple's `ar` produces a broken archive.

## Landed

The record of what is done, newest first. Details are in the docs and the ledger.

**Since the release (2026-10-31 to 2026-11-03)**

- **Manager attention guard and credential redaction (11-03).** Coalesced notifications no longer mark requests
  counted only as "more" as presented; quoted and JSON-style secret assignments are masked before Manager speech
  and session-derived text leave Core (pattern-based, not a guarantee).
- **Manager Core M1–M5 and client stage (11-03).** Hidden Manager sessions, 16 tools with the authority guards,
  episodes/recap/snapshot/delta, stage cards, navigation with a back stack, every route and event of design 10.7,
  `[manager]` config, `nexus manager`, durable `origin = "manager"`, attention, privacy notices, red-team evals,
  GPT-Live Core over the Codex sign-in (`aiortc`, Core owns the WebRTC call). Clients: stage, orb, cards,
  `manager.navigate` handling, `via Manager` labels, NSH `core/gptlive.ts` and TUI `live.rs` (stub-tested only).
  TUI `tests/manager.rs` passes (13 tests).
- **Mermaid in both clients via Ondine (11-03).** `nexus-mermaid` (text rows for pie/Gantt/timeline, Ondine SVG
  behind bounded/safe options, shared resvg PNG with bundled Monaspace Argon); identical paths in TUI and NSH,
  `◇ mermaid · copy source`, source fallback; theme insertion escaped; CI `mermaid` job.
- **Settings as one two-pane dialog (11-03)** in TUI and NSH: search, section rail, provider detail modal with
  link and code rows, Codex browser (PKCE loopback) and device sign-in, unavailable rows removed.
- **Drawing on composer images (11-03).** Drawing modal in both clients, flattened by `nexus-annotate`; kitty
  graphics and SGR 1016 pixel mouse in Ghostty.
- **Collapsed text pastes (11-03).** ≥ 500 characters become one `[Pasted ~N tokens]` marker; Core stores the full
  text as a collapsed part.
- **Project MCP also loads `.agents/mcp.json` (11-03)**; `.mcp.json` wins a duplicate, which is reported.
- **Step-limit default 500 (11-03)** (`MAX_STEPS`); per-model subagent effort; step-limit wrap-up; no
  read-before-edit guard.
- **UI polish (11-03):** fixed tab widths in header and subagent strip, square NSH surfaces (radius 0), 48 px NSH
  rail (approved difference), sticky user band scoped per session, project rail toggling the Sessions sidebar.
- **Kernel and shell counts (11-02)** in the location strip, with a read-only modal of kernels and shells.
- **Launch splash (11-02)** in both clients, following startup milestones.
- **Kernels released on archive (11-02)**; a busy owned kernel refuses archive.
- **Code-mode guidance (11-02)** in built-in agents; scouts get code with read-only nested tools.
- **Completion sound (11-02)**, distinct from the input-needed cue; reasoning-effort memory across sessions.
- **NSH closer to the TUI (11-02)**, TUI/NSH audit with `interface/scenarios/activity-group.json`, Markdown
  rendered in the file view, UI parity rule in `AGENTS.md`.
- **Gzip JSON responses (11-02)** over 500 bytes (event stream excluded); faster phone session opens.
- **Plan-limit usage for Claude (11-02)** through the CLI's `/usage`; every connected provider has a usage row.
- **NSH Void design (11-01)**, first and second pass: Geist and Monaspace Argon, one text edge, filled boxes with
  one status rail, compact starting-context ledger, waiting card with drawn marks, dock rework. NSH only;
  `nexus-nsh/theme-void.toml`.
- **NSH on macOS and iOS (11-01):** AppKit/UIKit packaging, direct connect by address, phone bottom-tab
  navigation; a signed iPhone build connects (no in-app mic: native NSH has no audio input).
- **Editor/Git steps 0–1 and step 2 in part (11-01/02):** NSH `Editor`/`DiffView`, vendored
  `ratatui-code-editor` (13 languages, theme tokens, Home/End, page and word moves, forward delete, undo grouping),
  NSH find bar, save-size fix (8 MiB envelope, 1 MiB content); files and diffs open in a centred modal in both
  clients (supersedes the right-hand panel).
- **Idle auto-archive (11-01)** after `[sessions] auto_archive_idle_seconds` (default 3,600).
- **Retention no longer stalls clients (11-01)**; lost background jobs get a terminal `bash.finished`.
- **Ask custom answers (11-01)** on every question in both clients; Core always accepts free text.
- **STT on Apple silicon (11-01):** PyTorch below 2.15 on macOS arm64.
- **TUI render performance phases 1–3 (10-31, follow-up 11-01).** Batching (902 events → 16 frames), layout cache,
  chrome-only frames reuse the timeline, `Rc<TLine>` (5,000-message RSS 1.1 GB → 0.33 GB), animation and clock
  keyed only when baked in.
- **Expirable wire bodies (10-31)**, content-addressed and deduplicated; stable envelopes.
- **Project MCP opt-out (10-31);** client approval UI removed.
- **Speech to text: pinned Parakeet Redux (10-31)** in an isolated, offline worker.
- **Ask questions paging, notification sounds, session state labels (10-31).**

**Release 0.10.0 (2026-10-30)**

- `nexus10` merged into `main` with `-s ours` (old Nexus 0.7.2 is the second parent; branch `nexus01`, tag
  `v0.7.2`). Release has TUI binaries for macOS arm64/x86_64 and Linux x86_64/aarch64, the Core wheel and sdist,
  `install.sh` and `SHA256SUMS`; the one-line install was run against the real release on macOS arm64.
- CI and CD adapted from old Nexus (`ci.yml`, `build-artifacts.yml`, `release.yml`, `pr-title.yml`); `release-as`
  removed afterwards. Stale PRs #95 and #70 are closed.
- `install.sh` (POSIX `sh`): from-checkout build, or release download with checksum verification; rejects musl,
  unknown platforms and checksum mismatches; warns about a missing `libasound.so.2` on Linux.
- Linux check on Debian 13 x86_64: Core 767 passed; TUI builds with `pkg-config` and `libasound2-dev`.
- Claude through the Agent SDK (provider `claude`, 17 tests, a real Haiku 5.5 tool turn); the SDK installs from
  source (560 KB) instead of the 223 MB wheel; hidden title request disabled; worker in an empty directory.
- Turn summaries steps 0–6 and 8 (2026-10-29): clients read `/api/session/{id}/turn` and `turn.summary`, not the
  event log (open 240 → 75 ms, RSS 133 → 19 MB).

## TODO

Items are grouped by area. Each says what "done" means where it is not obvious.

### 0. Make the tests green

Fix these locally before new work: they are real regressions or stale tests, independent of CI (which is
deferred).

- [ ] **TUI: not started in a branch yet.** An agent began investigating the failing tests on 2026-11-04 and was
  stopped before committing anything. CI's `tui` job stops at the first failing test binary (the markdown lib test),
  so the shell and timeline failures are hidden there; run `cargo test --no-fail-fast` locally.
- [ ] **TUI formatting:** `cargo fmt` in `nexus-tui` (`live.rs`, `manager.rs`, `dictation.rs`, `render.rs`,
  `tests/manager.rs`). This is CI's `lint` failure.
- [ ] **TUI clippy (`-D warnings`):** unused `composer_inner` (`render.rs:703`); `if_same_then_else`
  (`app.rs:10399`, `render.rs:7402`); `chunks_exact` with a constant size (`live.rs:68`, `speech.rs:155`); manual
  `is_multiple_of` (`paint.rs:350`, `render.rs:6330`, `splash.rs:261`); `useless_format` (`stage.rs:636`); unused
  import `SyncMsg`.
- [ ] **TUI failing tests.** For each, decide whether the code or the test is wrong; do not just update
  expectations:
  - `markdown::tests::old_renderer_keeps_links_tables_and_fence_source_visible` (`src/markdown.rs:1131`): the old
    renderer no longer shows table cells or the Mermaid fence source.
  - `shell::dark_frames_use_the_theme_file_surfaces` (`tests/shell.rs:147`): surface is `#0b0b0b`, the test expects
    `#141414`.
  - `shell::a_viewed_subagent_keeps_the_root_tab_and_names_itself_below` (`:2764`): tab row reads
    `Root task │ · Scout Child task` (fixed tab widths, 11-03).
  - `shell::composer_rows_are_edge_input_blank_control_edge_strip_at_every_width` (`:2810`): at width 120 the idle
    live row is no longer blank.
  - `shell::project_rail_labels_activity_and_session_restoration` (`:4761`): rail label `T` where `N` is expected.
  - `shell::session_rows_follow_d03_at_sidebar_widths_22_28_and_34` (`:3015`): row index 0 where 2 is expected.
  - `timeline::mouse_drag_selects_and_copies_timeline_text_without_activating_the_block` (`tests/timeline.rs:46`):
    a drag no longer emits `Effect::Copy("Hello")`. This one looks like a real regression in selection/copy.
- [ ] **Core failing tests: fixed in PR #99 (`fix/ci-core`), not yet merged.** Both `core` jobs pass on the PR;
  merge it with the TUI fix, since `ci-ok` stays red until then.
  - `test_manager_service.py::test_start_returns_a_call_and_status_restores_it_without_secrets`: now expects
    `live: None`; `docs/api.md` and `docs/manager.md` document `call.live`.
  - `test_eventlog.py::test_inspector_load_and_replay_have_independent_deduplication`: skips with a reason when
    `node` cannot run (the skip path itself is untested, since `node` works again).
  - `test_manager_live.py::test_two_real_peers_exchange_audio_in_process`: passes on CI (macOS and Ubuntu) and 4/4
    locally; the earlier failure was not reproduced, so it is unchanged.
  - Seen once under load, passes alone: `test_server.py::test_code_nested_write_needs_no_approval_and_kernel_api`
    (possible flake; watch it).
- [ ] **NSH failing tests:** `i7.test.tsx` "Settings opens as a modal … Esc returns" (line 62) expects Settings to
  stay open after the first Esc; after the two-pane rebuild the first Esc leaves the Providers page. Decide the
  intended Esc behaviour (it should match the TUI: Esc steps back, then closes) and fix the test or the code. The
  four files that cannot load `nexus/native` are in 8.2. WIP on branch `ci/nsh` (`9c5a127`, unverified): the
  dictation module looks up `nexus/native` lazily, as `mermaid.ts` does; `npm run check` and the browser tests have
  not been run on it, and the `MicrophoneModule` type it imports is not declared by `nexus/native` yet.
- [ ] Then rerun the [health check](#health-check-2026-11-04) and update its table.

### 1. Release and installation

- [ ] **Release PR #98 (`release 0.11.0`).** Open since 10-30; review its changelog before merging. Confirm the
  release has the four TUI binaries, the wheel and sdist, `install.sh` and `SHA256SUMS`, and run the installer from
  the published release on macOS and Linux. (Its `tui` checks fail; see item 0. CI itself is deferred.)
- [ ] **`nexus` opens the TUI on an installed system** (was TODO 2). Plain `nexus` and `nexus tui` exec
  `nexus-tui` (`cli.py` `_launch_tui`, commit `8622de5`), but that commit is not in `v0.10.0`. After the next
  release, check on a clean machine that `install.sh` then `nexus` opens the TUI with no extra step.
- [ ] **Check the Claude SDK source-build pin** in `uv.lock` and in a `uv tool install` of the built wheel; the
  release-download path of `install.sh` must pass `--no-binary-package claude-agent-sdk` as the from-checkout path
  does.
- [ ] **Remove `nexus-design-mockup/`** once NSH design parity is finished (8.2), and bump to the next `-dev`
  version after the release.
- [ ] **Update leftover `nexus10` wording** in `AGENTS.md` and `docs/development.md` where it names the branch
  rather than the pre-release home.

#### CI (started 2026-11-04, paused)

The user first deferred CI, then asked for it to be worked on, then paused it. State on 2026-11-04:

- `main` (`89eb847`) fails `lint` (`cargo fmt` diffs in `dictation.rs`, `live.rs`, `manager.rs`, `render.rs`,
  `tests/manager.rs`), `tui` (macOS and Ubuntu) and `core` (macOS and Ubuntu). `mermaid` passes.
- PR #99 (`fix/ci-core`) fixes `core`; open, not merged.
- Branch `ci/nsh` has one WIP commit (above); the CI job itself is still `if: false`.
- Anyone running `cargo fmt` while editing the Manager files will also clear most of the `lint` failure.

- `ci-ok` failed on the last five pushes to `main` (11-01 to 11-03): `lint` (TUI fmt), `core` (the Manager status
  test), `tui`. Item 0 fixes the causes locally.
- Confirm `ci-ok` is the required check in the repository settings.
- Re-enable NSH: remove `if: false` from `nsh` in `ci.yml` (line 119) and `nsh-web` in `build-artifacts.yml` (line
  27). `.github/actions/setup-nsh` builds `nsh` from the pinned `09c1632`, cached by commit; first runs took over 17
  minutes. Replace the pin with a published `nsh` release once one has the style properties NSH uses
  (`numberOfLines`, `userSelect`, `objectFit`, `pointerEvents`); nsh publishes binaries only for macOS arm64 and
  Linux x86_64. Until then no release carries `nexus-web.tar.gz`, and nothing in CI checks NSH or the phone code.
  Then remove `npm install -g nsh` from the from-checkout install path.
- A non-blocking TUI replay-bench report in CI.

#### 1.1 Platform matrix (O5, was TODO)

Each cell: install, `nexus doctor`, daemon start, TUI against the daemon, NSH web, a scripted turn and a Monty
kernel run.

| Platform | Status |
| --- | --- |
| macOS arm64 | Development machine; run the full list once from a clean `$NEXUS10_HOME` |
| macOS x86_64 | Binary builds and passes `--help` on its runner; not run |
| Linux x86_64 glibc | Core and TUI pass on Debian 13 (767 passed); installer tested from CI artifacts; NSH, a live daemon + TUI session and kernels remain |
| Linux aarch64 glibc | Supported (built on `ubuntu-22.04-arm`); run the same checks as x86_64 |
| Older glibc than Debian 13 | Release binaries build on Ubuntu 22.04 (glibc 2.35); not run |
| Alpine/musl | Not supported: `install.sh` rejects it with a clear message |

- [ ] Linux needs, in the installer's checks and `docs/development.md`: `pkg-config`, ALSA headers
  (`libasound2-dev` / `alsa-lib-devel` / `alsa-lib-dev`), a C toolchain, `git`, `uv`, Rust, Node 22 and `nsh`.
- [ ] Confirm the Monty kernel launcher's `/usr/bin/env` path on Linux.

#### 1.2 `nexus update`

- [ ] Decide whether `nexus update` installs or only reports (see [Decisions](#decisions-still-open)), then build
  it.

### 2. Verification with real providers, daemons and devices

Nothing here needs new code unless a check fails. Each needs a scratch `$NEXUS10_HOME` or a copy of a real home.

- [ ] **Real OpenAI and Ollama calls.** Codex and the device-flow providers (Copilot, Grok, OpenCode) are done.
- [ ] **`nexus migrate` on a copy of a real old `~/.nexus`**, on a fresh machine. Never the real home.
- [ ] **Real daemon with both clients:**
  - model memory with no default model: a draft with no model asks for one (Core's draft-context returns 400 "no
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

### 3. Manager

Design: [docs/manager-design.md](docs/manager-design.md) (section numbers below refer to it). What is built:
[docs/manager.md](docs/manager.md). **Nothing has run against a real model or real audio yet.** Decisions D1–D10
and Q1–Q3, Q5–Q7 are settled; Q4 is open.

#### 3.1 Milestones

| # | Delivers | State | Done when |
| --- | --- | --- | --- |
| M0 | Spikes: (a) STT → Luna → TTS latency, (b) Luna through the loop with control-style tools, (c) stage mock-ups, (d) a real GPT-Live call over the Codex sign-in | Scripts written (`nexus-core/scripts/spikes/pipeline_latency.py`, `luna_tools.py`), **not run** | Findings and screenshots written into the design; effort setting and stage look agreed |
| M1 | State, search and view | Core done | Both clients' own view-reporting tests (`POST /api/client/view`) run and pass |
| M2 | Typed Manager on the stage | Core and clients implemented; TUI `tests/manager.rs` (18) and NSH `manager.test.tsx` / `manager-cards.test.tsx` pass (2026-11-04, Chrome for Testing) | A typed Manager runs a whole day of real use; the interaction model is reworked (8.0) |
| M3 | Pipeline voice | Core done | Real microphone and speaker in the TUI and NSH: tap to talk, orb on real levels, thinking cue, playback, cancel; latency recorded (target under 1.5 s from end of speech to first audio) |
| M4 | Attention and acting | Core done | The injection and mishearing suite passes; the approval/question flow works by voice end to end |
| M5 | GPT-Live | Core done offline; NSH web and TUI stub-tested | iOS and native NSH mic/speaker loop; M0 spike (d) decides the route and the meaning of `speakable`/`commentary`; reconnect/pause polish; a 30-minute real call in each client surviving interruption, reconnect and a long delegation |
| M6 | Hands-free and polish | Not started | VAD and barge-in, TUI echo cancellation, iOS check, settings, notices, optional API-key route for GPT-Live; a week of daily use, then consider `enabled = true` |

Detail for each:

- [ ] **M0 (a)** `pipeline_latency.py`: record a WAV, `/api/voice/transcribe`, a one-shot `copilot/gpt-6-luna`
  call, `SpeechService.speak(text)`; log each stage at efforts `none`, `low`, `medium`; write results into
  design section 5.
- [ ] **M0 (b)** `luna_tools.py`: Luna through `nexus/loop.py` with stub `overview`, `find_sessions`, `show` and
  `navigate` over a seeded scratch home; record tool choice accuracy, spoken phrasing, whether ids are read aloud.
- [ ] **M0 (c)** stage mock-ups: orb, a `board` card and a `diagram` card from a fixture in both clients behind a
  dev flag; screenshots side by side; agree the look.
- [ ] **M0 (d)** a real GPT-Live call over the Codex sign-in with client delegation.
- [ ] **M2 manifest check:** `manager.start` (`/manager`, palette, `alt+m` / `cmd+shift+space`, Q3 to confirm),
  `manager.talk`, `manager.stop` and the engaged keys of 10.6; run `check_manifest.py`.
- [ ] **Evals with real Luna** (section 11): retrieval (~50 sessions, ~100 phrasings including mangled STT;
  top-1 and false confidence), authority, conversation across an episode seam, stage and view, honesty, and spoken
  quality judged by hand. The scripted versions already run in the Core suite.
- [ ] **Known authority boundary** to revisit: authority means "the user spoke"; ordinary acts are not tied to what
  the user asked for (docs/manager.md section 9).
- [ ] **Worktree instruction check** (risk table): after a Manager-started session's first turn, check its
  directory and say if it did not move into the requested worktree.

#### 3.2 Mermaid verification

- [ ] The TUI with a real kitty/sixel/iTerm2 image protocol and a narrow width (the `--dump-frame` half-block
  fallback at 170×120 is the visual floor; the raster's 0.5× legibility floor is the cutoff to source).
- [ ] Screenshots of the seeded `Mermaid diagrams` session in both clients, dark and light, wide and narrow.
- [ ] Rerun NSH's `scenarios.test.tsx` and `mermaid.test.ts` after the lazy `nativeModule("nexus/native")`
  change, and the TUI's `cargo test` after the direct-raster change.
- [ ] Keep the Ondine pin (`b19aa90`) deliberate: an update reruns tests and screenshots; cold CI builds need
  network for the git dependency.

### 4. File explorer, file editor and Git page

TUI and NSH on desktop and web only. Agreed 2026-11-01; files and diffs now open in a **centred modal over the
dimmed page** at every width (2026-11-02, supersedes the right-hand panel). Long lines wrap.

Decisions: the editor replaces the user's editor but stays small (highlighting, undo/redo, the
`ratatui-code-editor` feature set; no tabs, language servers or Vim mode). Git scope is worktrees, changes and
branches with actions; no merge, stash, rebase, conflict UI or hunk staging. The editor and Git page are silent to
the model: no timeline notices and no new agent tools. No new auth.

#### 4.1 Steps

| # | Step | State | Done when |
| --- | --- | --- | --- |
| 0 | Prove the risky parts | Done 2026-11-01 | — |
| 1 | Panel (now the modal), back/Esc, waiting badge, manifest `[navigation.panel]` | Done 2026-11-01; screenshots pending | Screenshots at narrow and wide in both clients |
| 2 | Editors | In progress | Save, stale-file and reload tests pass in both clients, including the NSH browser paths |
| 3 | Explorer actions and watcher | Not started | An agent's write shows in the tree without a refresh |
| 4 | Git page, read-only | Not started | All sections match `git` output on a repo with worktrees and remotes |
| 5 | Git actions | Not started | Each action tested on a temporary repo with a bare remote, with and without a running session; stage and unstage run with no popup |
| 6 | iOS gate and finish | Not started | Tools absent in the iOS layout and present on macOS and web |

**Step 2, remaining:**

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

**Step 3, explorer and live updates:**

- [ ] NSH's Files tab becomes the TUI's expanding tree, keeping expanded state. Checked 2026-11-04: `FilesTab`
  (`sidebar.tsx:421`) lists one path with a Parent button and the same chevron on every folder.
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

**Step 4, Git page read-only.** A new left sidebar tab (Sessions, Files, **Git**, Projects), repo-wide, starting on
the current session's worktree; the right sidebar's Git section stays as a summary and links here.

```text
WORKTREES (3)
● main        2 changes
  feat-x      clean      ↳ "Auth" running
BRANCHES
  LOCAL   main ↑1  · feat-x (worktree) · old-spike
  REMOTE  origin/main · origin/release
CHANGES on main
  STAGED    M server.py
  UNSTAGED  ?? notes.md
```

| Section | Shows | Actions (step 5) |
| --- | --- | --- |
| Worktrees | Path, branch, dirty count, sessions using it | Create, remove |
| Changes | Staged and unstaged with line counts; Enter opens the diff | Stage, unstage, discard, commit |
| Branches | Local and remote, upstream, ahead/behind, last commit, which worktree has it | Switch, create, delete |
| Remote | Upstream state in the section header | Fetch, pull, push |

Read routes, project-scoped next to `files`:

| Route | Returns |
| --- | --- |
| `GET /api/project/{id}/git` | Worktrees with their live sessions, local and remote branches, remotes |
| `GET /api/project/{id}/git/status?worktree=` | The shape of today's session `git` response |
| `GET /api/project/{id}/git/file?worktree=&path=&side=head\|index` | That version's text for diffs (UTF-8, 1 MiB) |
| `GET /api/project/{id}/git/live?worktree=` | Working and idle sessions, for the confirmation dialog |

**Step 5, Git actions.** `POST /api/project/{id}/git/action` with `{action, worktree, confirmed_live, ...}`, one of
`stage`, `unstage`, `discard`, `commit`, `switch`, `branch_create`, `branch_delete`, `fetch`, `pull`, `push`,
`worktree_create`, `worktree_remove`.

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

**Step 6, iOS gate and finish:**

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

Risks to keep in view: the vendored crate is young and upstream fixes are pulled by hand; the TUI binary grows from
13 MB to about 22 MB with the starting languages; discard and file delete are permanent; Core off loopback serves
without a token, so discard, delete and push join that surface (trusted networks only, by decision).

### 5. Turn summaries

Steps 0–6 and 8 are done; clients read `/api/session/{id}/turn` and `turn.summary` and never load the event log.
Preserve durable `input.*` events and the ordering of delivered steers; a steer is not a new turn. Benchmark:
[docs/turn-summary-benchmark.md](docs/turn-summary-benchmark.md).

- [ ] **Real-daemon checks:** a real session (`nexus chat`) shows usage and model on every `turn_request` row, and
  `curl` on a real session returns the documented `/turn` shape. The step 1 migration and backfill run once at the
  next daemon start (0.4 s on a copy; usage for 578 of 587 requests).
- [ ] **Step 7 remainder:** `docs/development.md` where it mentions event history; `interface/design-language.md`
  for the floor row ("the turns above have no recorded details…").
- [ ] **Step 9: live partial; stop saving deltas.** Not started.
  - New `nexus/live.py` with `LivePartial` (`request_id`, `seq`, `parts`, `reasoning_seconds`): a text or reasoning
    delta appends to the last open part of its kind or opens one; `tool_call` opens a part, `tool_args` appends raw
    argument text to the open call with that id; `done` closes the oldest open part with its final encoding and
    `"done": true`. It can absorb the current `ReasoningClock` in `runtime.py`.
  - The runtime folds deltas and saves the partial at most once a second (`save_partial`) to a `partial` column on
    `turn_request`; `/turn` serves `partial` on the running request; the saved message clears it; an interrupted
    request keeps it and recovery's counts (`partial_summary`) match it, replacing `store.stream_summary`.
  - `Bus.publish(..., durable=False)`: no `append_event`, `id = None`, delivered live only; the SSE writer, client
    cursor parsing and `bus.overflow` (which reports the last durable id) must handle id-less events, and
    `Last-Event-ID` never points at an ephemeral event.
  - Delta fields gain `request_id`, `part` and `seq`. A joining or reconnecting client reads `/turn`, takes the
    running request's `partial`, applies only deltas for that `request_id` with a greater `seq`, and starts a new
    scratch for another request. TUI and NSH seed from `partial`.
  - The partial is a diagnostic, never shown as an assistant message: streaming while it runs; after an
    interruption, the recovery notice and the partial marked as not in context.
  - Tests: folding (interleaved reasoning and text, two tool calls, `PartDone` replacing folded text); a client
    opening mid-stream sees the text so far with nothing duplicated or missing across part start/end, retry and
    message saved; a crash mid-stream leaves a partial at most a second old; no delta rows in `event`.
  - Docs: `docs/stream-events.md`, `docs/api.md`, `docs/inspector.md` (streaming detail now from `wire_event`),
    `docs/daemon.md`; the recovery notice text changes once `partial_summary` exists.
  - Done when a second TUI opened during a long response shows the same text as the first, and the session's event
    log holds no delta rows. If rewriting the growing JSON each second costs too much, store parts in their own rows
    and append (as pi-durable's `PartialChanges`).
- [ ] Old clients reconnecting mid-response will lose the deltas they missed (the message still arrives): note it
  in the API changelog when step 9 lands.

Out of scope: a windowed message transcript, server-side views with patches, changing wire-event retention.

### 6. TUI render performance and launch time

From `reports/tui-load-time-investigation.md` (2026-11-03), still open:

- [ ] **Cold Core start scans the whole event log.** `open_bash_jobs()` (`store.py:2745`) selects `bash.started` /
  `bash.finished` rows with no index led by `type`, so every daemon start scans all events (1.37 M then) and runs
  `json_extract` on each: 1.9 s per cold start, 14.7 s with pending migrations. Add a partial index
  `event_bash on event(type, session_id, id) where type in ('bash.started','bash.finished')` beside the other
  deferred indexes, or keep open jobs in their own small table. Section 13 may remove the need.
- [ ] **The terminal image probe costs up to 1 s on every launch** (`main.rs:526`,
  `Picker::from_query_stdio()` waits for a reply some terminals never send). Skip it when the environment says it
  will not answer, cache the result per terminal, or shorten the timeout.


Phases 1–3 are done (see [Landed](#landed)); targets are met at 1,000 messages (5.2 ms paced draw). Remaining work
waits for a profile or workload that needs it.

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

### 7. Agent loop, tools and context

From the session retrospective (2026-11-03, 261 sessions):

- [ ] **Tiered fuzzy matching in `edit`** (curly quotes, dashes, trailing whitespace, CRLF), as opencode does, and
  a multi-edit input like pi's `edits[]`.
- [ ] **Soft nudge at about 80% of `steps`**, and a default effort for `scout`/`quick`.
- [ ] **Code mode:** measure comparable real-provider tasks for correctness, requests, tokens and elapsed time
  ([docs/agents.md](docs/agents.md#choosing-code-mode)). The live `~/.nexus/agents/build.md` override needs the
  same guidance as `builtin_agents/build.md`.

Context and sessions:

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

### 8. Interface (TUI and NSH)

#### 8.0 Polish (user review, 2026-11-04)

The Void design is mostly complete. What the user called out next (2026-11-04): TUI modals, the Manager's
interaction model, scrolling (the sent message at the top; NSH must stop yanking the reader), new spinners, the
old Nexus tool-group fold, pending steer/queue rows, a general polish pass, and the iOS app.

##### TUI modals

The TUI mostly looks good, but Settings and the other modals need a design pass. Today every modal shares
`modal()` (`render.rs:7605`: scrim, raised fill, bold title on row 1, `esc` at the right edge) and `modal_rect`
(`paint.rs:74`: 2 cells from each edge, at most 70% of the height), but sizes and footers diverge:

| Modal | Drawn by | Size | Footer |
| --- | --- | --- | --- |
| Settings | `draw_settings` (`render.rs:6984`) | 88×40 | Own; wording changes per pane (`esc close` / `esc back` / `esc cancel`) |
| Provider detail | `draw_provider_detail` (`:7434`) | 66 × 10–22 | Own |
| Palette, row menu | `draw_palette` (`:8118`), `draw_row_menu` (`:8044`) | 64×22, placed a quarter down; row menu anchored to its row | Own |
| Kernels and shells | `draw_resources` (`:8700`) | 70% width, 52–100 | `hint_row` |
| Help, Notifications | `draw_help` (`:9129`), `draw_notifications` (`:8389`) | 64×30 fixed | `hint_row` / own |
| Alert detail, context detail | `:9051`, `:8672` | 60% width, 48–92 | `esc close` |
| Context doc, context entries | `:8901`, `:8973` | 70% width, 48–100 | `scroll`/`move`, `esc close` |
| Usage | `draw_usage` (`:7803`) | 64 × rows+6 | `r refresh, esc close` |
| Learn checklist | `:7915` | 76 | Own, built dynamically |
| Image preview, annotate | `:8456`, `:8484` | 85% width | `esc close` / own `esc cancel` |
| Confirm dialog, inline edit | `draw_dialog` (`:9206`), `:296`/`:632` | computed / 60×8 | `←/→ choose, enter confirm, esc cancel` / `enter save, esc cancel` |

- [ ] Define a small set of modal sizes (for example small 60, medium 72, large 88 or 70% of the width with one clamp)
  and one height rule; put every modal on one of them.
- [ ] One position rule (centred), or a documented reason for the palette sitting high.
- [ ] One footer: every modal uses `hint_row` with the same order and wording. Pick one verb per meaning (`close`
  leaves the modal, `back` goes up a level, `cancel` discards input) and drop the duplicate `esc` in the title when
  the footer already says it, as NSH did.
- [ ] Settings: review spacing, hierarchy and focus in both panes at 80×24 (where 88×40 does not fit) and 160×48;
  the provider detail modal with its link and code rows; the tool grid.
- [ ] Record the rules in `interface/design-language.md`, apply the same rules to NSH's modals, and take PTY and
  NSH screenshots before and after.

##### Manager interaction in the TUI (and NSH)

It looks good, but the interaction model is wrong, starting with Esc. Today (`manager.rs`, `stage.rs`, the
`[[keymap.escape]]` chain in `interface/manifest.toml:85`):

- `alt+m` toggles: starts a call and opens the mic, sends while listening, barges in while busy, focuses an
  unfocused card. `alt+shift+m` (or `/manager stop`) is the only way to end a call.
- Esc is taken only while the Manager is "engaged" (`manager_engaged_input`): it closes the typed field (dropping
  its text), drops a recording (the call stays open), dismisses a focused card, or stops speech (only when no card is
  focused; with a focused card it dismisses the card instead).
- While the Manager is thinking, running a tool, or idle in a call, Esc is not the Manager's: it falls through the
  app chain and, if the viewed session is busy, **interrupts that session**. Pressing Esc to stop the Manager can
  therefore stop the user's coding session.
- No hint anywhere says that `alt+shift+m` ends the call; the only Esc hints are on cards (`esc close`) and the
  consent notice.

**Brainstorm (user, 2026-11-04): the call takes the stage.** The user's direction: pressing Esc to stop the
Manager must not be confusing; perhaps the orb sits in the centre while a call is in focus and everything else drops
to low opacity. Options to settle before building:

| | A. Call mode (recommended starting point) | B. Ambient orb (today, refined) | C. Push to talk only |
| --- | --- | --- | --- |
| Look | Orb centred and large; the app behind it at ~30% opacity (TUI: the existing `paint::scrim` dim; NSH: an opacity layer); caption under the orb; a card, if any, below the caption | Orb small at the bottom centre; app at full strength | Orb appears only while the key is held |
| Who gets keys | The call, entirely; nothing reaches a session by accident | Shared with the app, as now | The app, except the talk key |
| Esc | **Minimise**: the call keeps running, the orb shrinks to the corner, the app returns to full opacity and gets its keys back. Never ends the call, never interrupts a session | Must mean something per state (today's problem) | Not used |
| End the call | An explicit **End** under the orb (click, `alt+shift+m`, or saying "that's all") | Same | Releasing the key ends the turn; the call is implicit |
| Talk / interrupt | `space` (or `alt+m` from anywhere) talks; talking over the Manager stops its speech (barge-in) | `alt+m` | Hold `alt+m` |
| Proactive lines | Arrive in the minimised orb as a caption; never take the stage | Same | Toast |

- [x] Chosen: **A, call mode** (user, 2026-11-04).
- [ ] For the chosen model, write the state table in `docs/manager-design.md` 10.1 and 10.6: states (no call,
  focused: listening / typing / thinking / speaking / card; minimised: idle / speaking / proactive; consent) and
  what Esc, `alt+m`, `alt+shift+m`, Space, Enter, digits and a click do in each. Keep four actions distinct: stop
  speaking, cancel the Manager's turn, dismiss the card, end the call.
- [ ] Hard rule whatever the model: while a call is open, Esc never reaches the session interrupt.
- [ ] One quiet hint line under the orb with the current state's keys, including how to end the call.
- [ ] Build it in both clients together (TUI `manager.rs`, `stage.rs`, `orb.rs`; NSH `src/ui/manager/stage.tsx`,
  `orb.ts`), with the manifest updated, `check_manifest.py` run, and tests in `tests/manager.rs` and
  `manager.test.tsx` for every state's Esc. Screenshots of focused and minimised calls in both clients.

##### Scrolling: the sent message goes to the top, and new content never yanks the reader

Two user reports (2026-11-04), one model for both clients:

1. **NSH keeps scrolling when a new message comes**: while the user reads further up, new content pulls the view
   back down.
2. **The message just sent should sit at the top of the screen** (TUI and NSH): after the user sends a message the
   timeline scrolls so that message is the first thing in the viewport, and the reply streams in below it.

The model:

- **On a session's first send:** scroll so the new user band's top meets the viewport's top (below the header). Reserve space below
  it (a spacer as tall as the viewport minus the band) so this works even when the reply is still empty; the spacer
  shrinks as the reply grows and disappears once the reply fills the screen.
- **While the reply streams:** the view stays where it is. It does not follow the bottom; text grows down out of
  view, and the jump-to-latest pill shows "new below". Following resumes only when the user scrolls to the end, or
  presses the pill (or End).
- **When the user scrolls up:** never move them, whatever arrives (messages, tool rows, measured heights,
  summaries, child cards). Row height changes above the viewport must keep the visible content anchored (scroll
  anchoring by the first visible row), not shift it.
- **Other sessions and session switches** never scroll the viewed timeline; a switch restores that session's last
  position (NSH today forces the end on every switch).
- Decided (user, 2026-11-04): **only a session's first message** is pinned to the top. Later sends follow the
  normal rule: the view follows the reply only if the reader was at the end.

Likely causes of report 1 in NSH (`nexus-nsh/src/ui/timeline.tsx`), from the 11-04 code read:

- Follow heuristic (lines ~1463–1472): the reader counts as scrolled up only when `y < lastY - 0.5` **and**
  `gap > lastGap + 0.5`; while content streams in, a slow upward scroll can fail that test and snap back to the end.
  Follow resumes only within `END_SLOP` (24 px, line ~127) of the end, so a scroll that stops just short never
  re-follows.
- `keepEnd` runs on every `rows()` or `rowHeights()` change (line ~1454) through `setTimeout(toEnd, 0)` and
  `scrollToEnd({animated: false})` (~1419–1425); rows measured late (`ROW_ESTIMATE = 72`) can fight the reader or
  stop short of the end against a stale content height.
- Session switch (~1430–1452) resets `following`, `lastY`, `lastGap` and `viewH` and forces the end; the scroll
  offset is kept per session but not restored, so every session reopens at the end.
- `scrollY` is per session (~115) but `viewH` is global (~116), so the sticky band and the jump pill's `newBelow`
  (~1381) use stale geometry until the next scroll event.
- The sticky band's geometry (~1118–1160) uses estimated heights for unmeasured rows, so the band can jump.
- `scrollEventThrottle={64}` (~1476) delays follow transitions.

- [ ] Implement the model in NSH: replace the `keepEnd` on every `rows()`/`rowHeights()` change with explicit
  follow state; anchor on the first visible row when heights change; make `viewH` per session; restore the per-session
  offset on a switch; pin on send with the spacer.
- [ ] Implement the same model in the TUI (`sticky.rs`, `layout.rs` follow/anchor, the jump pill) and check its
  current behaviour against it.
- [ ] Tests in both clients: stream while scrolled up (the view must not move); stop just short of the end; send
  a message (its band at the top, the reply below); a long reply growing past the viewport (no follow until the
  end is reached); a session switch and back (position restored); late-measured rows above the viewport.

##### Spinners: a 4-dot tab spinner and a juggling subagent spinner

User request (2026-11-04): replace the fixed spinner with two livelier ones, each **four dots**, one for session
tabs and one for subagents. Today both clients use one 8-frame ring from `interface/theme.toml`
`[glyphs.spinner]` (`⣾ ⣽ ⣻ ⢿ ⡿ ⣟ ⣯ ⣷`, ASCII `| / - \`), read by `theme.rs:492` (TUI) and `kit.tsx`
`Spinner` (NSH) at the TUI's 125 ms frame.

Design, using the 8 outer dots of one Braille cell as a clockwise track (`0x01 0x08 0x10 0x20 0x80 0x40 0x04
0x02`, i.e. `⠁ ⠈ ⠐ ⠠ ⢀ ⡀ ⠄ ⠂`):

- **Tab spinner, "snake" (1 cell).** Four lit dots chase round the track: a head and three trailing dots, one step
  per frame. Each full lap the snake changes shape at random (seeded per session, so tabs drift out of step): solid
  (`⡇ ⠏ ⠛ ⠹ ⢸ ⣰ ⣤ ⣆`), a gap after the second dot (`⣃ ⡍ ⠞ ⠳ ⢩ ⣘ ⡴ ⢦`), or split into two pairs
  (`⢣ ⣉ ⡜ ⠶ …`). It is always exactly four dots, so its weight never flickers; the shape change is what makes it
  feel alive. Generated as `chr(0x2800 | mask)` from the track; frame interval 80–100 ms.
- **Subagent spinner, "juggler" (2 cells = a 4×4 dot field).** Four balls on a cascade arc between the two cells:
  each ball rises in one column, crosses at the top and falls in the other, offset a quarter period apart, so the
  pattern reads as juggling rather than rotating. Frames are generated by a small script from the ball paths (no
  collisions: two balls never share a dot) and tuned by eye in a preview; then checked into `theme.toml`.
- **ASCII and mono:** the snake becomes a 4-character bounce (`[.   ]` → `[..  ]` → `[... ]` → `[....]` →
  `[ ...]` …); the juggler becomes `o` characters on two rows only where two rows fit, else the same bounce.
- **Reduced motion:** the static `running` status glyph, as today.

- [ ] Write the generator (`interface/scripts/spinners.py` or `scripts/gen.py`) that emits the frame lists, and add
  `[glyphs.spinner_tab]` and `[glyphs.spinner_agent]` (unicode, ascii) to `interface/theme.toml`, with the random
  shape choice specified as data (variants plus a per-session seed) so both clients behave the same.
- [ ] TUI: `theme.spinner(frame)` becomes two functions; the tab strip uses the snake (`render.rs:3176`, `:3679`),
  the subagent strip and child cards use the juggler (`timeline.rs:3515`). Check the animation cache keys
  (`Timeline.animated`) still hold.
- [ ] NSH: `Spinner` in `kit.tsx` takes a `kind`; `header.tsx` tabs use the snake, child rows and the subagent strip
  the juggler; regenerate `src/gen/theme.ts`.
- [ ] A preview (TUI `--dump-frame` sequence, NSH story) for tuning; the user approves the look before it lands.

##### Tool groups fold away when they finish (port from old Nexus)

Old Nexus (`origin/nexus01`, `rust/tui/src/transcript/fold.rs`, `Cache::step_folds`; spec in its
`docs/ratatui-parity.md` "Fold on completion"): when a folded group goes from running to finished, its visible tree
rows leave **one per 80 ms frame from the bottom**, then the header **flashes** (accent → muted over six frames)
and settles to `▸ <summary> · 4.6s`. While the fold runs the group is drawn from an overlay block
(`Cache::fold_overlay`) and the frame clock keeps running (`Cache::folding()`). With animation off there is no fold.
A group first seen finished (reload, reconnect) never folds; a group the user opened mid-fold shows its rows.
Individual tool completions inside a still-running group never trigger it.

Today both clients have only the header flash (TUI `group_flash`, `FLASH_FRAMES`, `timeline.rs:2597`; NSH
`FLASH_MS = 600`, `timeline.tsx:82`, `:1192`): the running group's last five rows vanish at once.

- [ ] TUI: add a fold state per group key (start frame, rows at start), draw the rows that remain this frame
  above the header's final state, then run the existing flash; keep requesting frames while any fold is live; key
  the timeline cache on it like `animated`. Respect reduced motion, reload/reconnect (no fold) and a user opening
  the group mid-fold.
- [ ] NSH: the same sequence, row by row at 80 ms (an opacity/height transition per row, removed from the bottom),
  then the existing 600 ms flash; no animation with reduced motion.
- [ ] A shared scenario (`interface/scenarios/`) with a group that finishes live, plus frame tests in TUI
  `tests/frames.rs` and an NSH test using a fake clock. Ledger row: ported from old Nexus.

##### Pending steer and queue rows: quiet marks, actions on hover

User request (2026-11-04): the pending-input rows above the composer should not show an orange `S` / `Q`; the mark
should be white (primary text), and hovering a row reveals clickable actions: edit, move up, move down, remove
(and steer, for a queued item).

Today:

- **TUI** (`draw_composer`, `render.rs:5944`): one filled card; each row draws `S` or `Q` in `ACCENT` bold
  (`render.rs:5997–6006`) two cells left of the text. Actions are keyboard-only on the selected row (`enter edit ·
  backspace remove · s steer · shift+↑/↓ move`, `:6010`); no mouse targets on the rows (only `Target::Composer`).
- **NSH** (`PendingInputs`, `composer.tsx:651–715`): rows say `steer` / `queued` in meta text, and already reveal
  icon actions on hover (pencil, x, Steer, arrow-up, chevron-down; always shown on touch or narrow). The two
  clients already differ.
- Core already has every route: `PATCH /api/session/{id}/input/{iid}` (edit, promote to steer), `DELETE` (remove,
  returns its parts), `POST …/{iid}/move {before}` (reorder).

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

##### General polish, TUI and NSH

The user wants a polish pass across both clients now that the Void design is mostly complete. Beyond the items
above:

- [ ] A side-by-side review of both clients against the shared scenarios (`interface/scenarios/`) in dark and light,
  at TUI 80×24 / 120×40 / 160×48 and NSH 390 / 1024 / 1440 px; list every misalignment, inconsistent spacing,
  wording or state and fix them in pairs.
- [ ] Empty, loading, error and offline states on every surface (sessions, files, settings panes, pickers,
  new-session view).
- [ ] Keyboard-only paths and focus visibility in both; decide the focus-ring colour.
- [ ] Motion: one set of durations (flash, fold, spinner, modal open) shared through `theme.toml`, all off with
  reduced motion.
- [ ] Copy pass: sentence case, one verb per meaning (close / back / cancel), no developer notes in the UI.

##### iOS NSH polish

The phone app needs a lot of polish (code audit; nothing has been checked on a device since 11-02). Phone screens:
Connect, Sessions tab, Settings tab, the conversation page, and overlays (palette, pickers, switcher, new session,
rename, confirm, usage, alerts, image preview, annotate, Manager stage, edge drawers). In order:

- [ ] **Keyboard avoidance.** `keyboardFrame()` is used only by Connect (`connect.tsx:68`); the composer dock
  (`composer.tsx:364`) pads only `safeArea().bottom`, so the keyboard can cover the composer; rename, new-session,
  palette and search modals have the same gap. Add the keyboard height to the dock and anchor modals above it.
- [ ] **Desktop affordances in modals.** `ModalTitle` draws an `esc` chip (`modal.tsx:37`), footers and the composer
  show key hints (`composer.tsx:354`, `:536`), and there is no touch close button; modals use desktop padding
  (`p-6`, `top-[12%]`, `max-h-[76%]`). On touch: a 44 px close button, no key hints, bottom sheets.
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

#### 8.1 Both clients

- [ ] **Audit navigation, pickers and panel interactions** across the clients (the 11-02 audit covered tool
  groups, palette, child rows, banners, rail, footer, composer, starting context, waiting cards and notices).
- [ ] **Ctrl+R prompt history search** in both clients (in the TUI `ctrl+r` is bound to `app.reconnect`,
  `app.rs:8162`, so pick the binding first; Up/Down recall exists in `composer.rs:69`); verify cross-client per-project recall after restarting
  both, including multiline drafts and images.
- [ ] **Subagent parent card:** model/effort, elapsed time and completed token count; distinguish background
  completion from the start result.
- [ ] **Non-destructive project removal** from the rail (requested 11-03). Needs Core support: the existing delete
  action permanently deletes sessions.
- [ ] **Background shells and archive** (was TODO 3). The count is shown in the location strip (11-02).
  Auto-archive skips a tree with a live background job, but manual `archive_session` (`runtime.py`) does not
  refuse one; make it refuse with a clear message, as it does for a busy kernel, and show why in both clients.
- [ ] Review both clients side by side in light and dark; establish large-session performance budgets; test
  keyboard-only paths; decide the focus-ring colour.
- [ ] Agent cards show the file path but cannot open it (the editor reads project files only; global and built-in
  agents live elsewhere).
- [ ] Show the Claude limits in the timeline: a "buffered, no thinking" note for a Claude model and a clear image
  failure (the error text exists; check how each client renders it).

#### 8.2 NSH

- [ ] **NSH tests that import `src/` directly** (`App`, `i8`, `context-header`, `voice-settings`; `editor` now
  loads) cannot load `nexus/native` under `nsh test`, because `commands` reaches the statically imported microphone
  module. Fix
  that (lazy native lookup, as Mermaid now does) before relying on them.
- [ ] **Browser suite:** fix the mock event stream ending and forcing the app into reconnecting state (confirmed
  2026-11-04: many tests show "Disconnected from Core · reconnecting in 1 s" yet pass). The 2026-11-04 run: 157
  tests, 152 passed, 5 failed (the four load failures above and `i7.test.tsx`, item 0); 6 min 09 s. `nsh test --target web` needs an explicit Playwright
  Chromium path on this machine (`--browser <path>`). Then add coverage for: Enter/Tab steer and queue and the
  pending-input stack, steer numbering, new-session skills/MCP rows, the usage modal, Settings search, key entry,
  link/code rows and Agents edits, child-session picker navigation, project tab isolation.
- [ ] **Design parity:** new-session view, files, narrow layouts and states; sidebar search handling and live
  shimmer; sidebar rows show project or branch where available; verify Monaspace Argon prose rendering natively.
  Then build the macOS desktop bundle and complete files, Git and worktree UI (section 4).
- [ ] Make NSH tool-result images zoomable.
- [ ] Native mic: in-app capture stays web/TUI until the NSH runtime gains audio input; the mic button points to
  iOS keyboard dictation meanwhile.

#### 8.3 Settings

- [ ] Sign in to a real device-flow and a real browser-flow provider from each client's Providers pane.
- [ ] NSH browser coverage (after the mock-stream fix) and PTY / NSH screenshots at 80×24 and 160×48.
- [ ] State the required Claude Code version (2.1.248 or newer) and show "Claude Code is not installed" on the
  Providers page when `cli_path()` is empty.
- [ ] Real Settings panes for MCP servers, skills and hooks when those controls exist; they are deliberately absent.

#### 8.4 NSH Void design leftovers

- [ ] **Fold the starting context once a session has messages?** Default: leave it open and delete leftovers if
  any reappear; the alternative is folding to `Context · 5 items · ~5.4K tokens` in place. See
  [Decisions](#decisions-still-open).
- [ ] **Accent equals primary text,** so links look like plain text. Default: underline links in prose.

#### 8.5 TUI

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

### 9. Extensions, skills, MCP and hooks

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

### 10. Providers

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
- [ ] **Antigravity:** assess fit with the one-timeline rule before porting; use the Claude Agent SDK and the
  approach in https://github.com/NoeFabris/opencode-antigravity-auth rather than ZMX.
- [ ] Every provider needs wire fixtures, provider/auth docs and a verified real call; group OpenAI-compatible ones.
- [ ] Cross-provider conversion beyond text and tool calls (foreign reasoning is dropped); Copilot Responses
  encrypted reasoning replay.
- [ ] Revisit Responses `instructions` if the Codex backend changes (it accepts it, though the Codex CLI sends a
  developer message).

### 11. Worktrees

Separate from the Git page (section 4), and builds on it.

- [ ] Port worktree review and integration as control commands: review against the base, merge by fast-forward or
  merge commit with confirmation, remove the worktree and branch after merge. Commands in both clients; agents get
  them only when granted. Reference: old `nexus/agents/worktree_review.py`, `worktree_integrate.py`,
  `nexus/worktree_cleanup.py`, `tests/test_worktree_merge_permission.py` (in `~/repos/nexus-old`).
- [ ] Decide cleanup on archive/delete (archive must never implicitly delete a worktree) and how to leave one.

### 12. Reliability and safety

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

### 13. Event storage: harden finished sessions into messages and flush their events

User request (2026-11-04): the event log has grown very large; old events should not be kept. Only active sessions
need their events; everything else should be "hardened" into messages and full context, and the events flushed.

**Findings (2026-11-04, measured on a read-only copy of `~/.nexus/nexus.db`):**

- **The running daemon is old code.** It is the `uv tool` install built from the 10-30 wheel, running since 10-30.
  None of the 10-31 retention work (expirable wire bodies, `event_body`, `payload_state`, `storage_metrics`) is
  deployed, so only the old rule applies: prefix pruning after 14 days or 256 MiB. `log.floor` is 0: nothing has
  ever been pruned.
- **This machine's DB:** 177 MB plus a 7.7 MB WAL; 41,818 events, 200 messages, 5 sessions. (The 1.5 GB /
  1.37 M-event DB in `reports/tui-load-time-investigation.md` is not on this disk; the mechanisms are the same.)
- **Where the bytes are:** the `event` table is 165 MB of 177 MB; messages are 0.75 MB. By type:

  | Type | Rows | MB | Note |
  | --- | --- | --- | --- |
  | `wire_request` | 101 | **140.9** | 1.46 MB average, 5.8 MB max |
  | `wire_event` (raw SSE chunks) | 20,595 | 17.0 | |
  | `tool_args_delta` | 18,690 | 1.5 | |
  | `message.added` | 200 | 0.68 | |
  | everything else | small | < 2 | `text_delta` 1,140 rows |

  One session ("Explaining LLM Caching") holds 152.6 MB of it.
- **Why `wire_request` is huge:** every request stores its full body, and each body carries the whole history,
  including **base64 images from tool results** (5.07 of the largest 5.8 MB). Storage grows quadratically with
  session length. All 101 bodies differ, so whole-body SHA dedup saves nothing; zlib only gets 141 → 95 MB. The same
  images already live once in `~/.nexus/artifacts` (3.8 MB total).
- **Why it is kept:** every `Bus.publish` is durable (a carry-over from old Nexus, which derived all state from
  events), including token deltas and every SSE chunk; retention never triggered; and `auto_vacuum = 0`, so even
  deleted rows would leave the file the same size. Nothing is protected from pruning here (all turns terminal, no
  open waits or jobs).
- **What the committed but undeployed code would do** (simulated on a copy): all wire payloads expire to markers,
  but the file stays 178 MB (157 MB of free pages). With `VACUUM` and `auto_vacuum = incremental`: 15.7 MB.
  Deleting every session event, then `VACUUM`: **2.1 MB**.

**What actually needs events** (none of it needs old history for model context; messages, `session_snapshot`, the
`turn*` tables and `session_usage` already are the hardened record):

| Job | Needs history beyond active sessions? |
| --- | --- |
| Reconnect cursor / SSE `Last-Event-ID` | No: clients resync from `/api/snapshot`; a cursor below the floor gets 410 |
| `message.added` outbox (exactly once) | No: only messages with `event_id IS NULL` after a crash |
| Recovery of an interrupted turn (`stream_summary`, `wait_lifecycle`, bash jobs, `code.*`) | No: unfinished turns and sessions only |
| Turn footers, stats, usage | No: from the `turn*` tables (section 5) |
| Inspector Steps (status, TTFB, retries) | Yes today; mostly also in `turn_request` / `turn_attempt` / `turn_tool` |
| Inspector Requests and Raw | Yes today; diagnostic only |
| Inspector Questions card | Yes, unless moved to a table |
| Export (events section) | Yes; expired markers are already supported |

**Decided (user, 2026-11-04): compact finished sessions.** **Rules this touches.** The one-timeline rule holds (what the model had is messages plus snapshots, kept). Two
written promises change and need the user's sign-off and a ledger row: `AGENTS.md` "wire events are never dropped"
(becomes "dropped only when announced", with a compaction record), and the inspector's raw wire "for any past
session" (becomes "for active and recent sessions").

**Steps:**

- [ ] **13.0 Deploy what is committed.** Reinstall the tool from the checkout and restart the daemon (the first start
  runs the in-place migrations: 0.3 s on the copy). Done when `nexus doctor` no longer reports different code and
  `/api/info` `retention_status` shows wire payloads expiring. Restarting also rotates the daemon token, which was
  printed into a tool log during this investigation.
- [ ] **13.1 Reclaim space automatically.** New databases get `pragma auto_vacuum = incremental` (it must be set
  before the first table). `Retention.run_once` runs a bounded `incremental_vacuum(N)` whenever free pages exceed a
  threshold (for example 8 MB). Files: `store.py` (`prepare_database`, `Store.__init__`, `reclaim_space`),
  `retention.py`, `docs/daemon.md`. Tests: a new DB reports `auto_vacuum = 2`; after expiry and a pass the file
  shrinks; the pass stays bounded. Existing databases convert in 13.6.
- [ ] **13.2 Deltas are not stored** (section 5, step 9): `text_delta`, `reasoning_delta`, `tool_call_started` and
  `tool_args_delta` go through `Bus.publish(..., durable=False)`; `LivePartial` and `turn_request.partial` replace
  `stream_summary`. Here that removes 48% of rows. Both clients in the same change.
- [ ] **13.3 Stop the quadratic `wire_request`.** Store each request body's `input[]` items content-addressed in
  `event_body`, so a request is an envelope plus item SHAs (each history item is stored once per session), and
  replace `data:` image URIs with artifact SHA references before storing. Optionally fold a request's `wire_event`
  SSE chunks into one stored stream body instead of one row per chunk. Files: `payloads.py` (encode and
  `event_data` reassembly), `store.append_event`, the wire emitters in `llmhub/`, `export.py`, the inspector read
  path. Tests: a reassembled body is byte-identical to the original; N requests store about O(N) bytes, not O(N²);
  export output unchanged; readers handle both formats. Done when the 141 MB session's wire storage is a few MB
  while still retained.
- [ ] **13.4 Move the remaining history readers onto state tables.** Question/permission asked/replied into a small
  `wait` table (also serving the inspector's Questions card); background jobs into a `bash_job` table, replacing
  `open_bash_jobs` / `unfinished_bash_jobs` (which also removes the cold-start full scan in section 6); `code_used`
  becomes a session flag; inspector Steps reads the `turn*` tables (add `http_status` and `ttfb` columns if
  missing); drop the legacy `turn_end` fallback after one release. Files: `store.py`, `recovery.py`,
  `continuation.py`, `runtime.py`, `server.py`, `inspector/index.html`, `docs/inspector.md`. Done when
  `grep "from event"` hits only replay, the outbox and maintenance, and the recovery suites pass with the `event`
  table emptied for terminal sessions.
- [ ] **13.5 Harden and flush finished sessions.** A session is compactable when it has no running turn, no pending
  recovery, no open wait or job, no replay pin, no message without its `event_id`, and is archived or idle longer
  than a configurable window (default 24 h, alongside `auto_archive_idle_seconds`). Retention deletes all its
  events and writes one durable `session.compacted` record (the removed id range and counts); cursors that fall in
  a compacted range get 410 and resync. Global events stay. This replaces prefix-only `prune_events`, which stalls
  behind one protected session. A per-session "keep diagnostics" switch keeps raw wire for a session the user is
  debugging. Files: `retention.py`, `store.py` (`compact_session`), `eventlog.py` (cursor check), `export.py` (an
  "events compacted" section), the inspector ("earlier events compacted" notice), `docs/daemon.md`, `docs/api.md`,
  `docs/stream-events.md`, `docs/inspector.md`, the ledger, `AGENTS.md`. Tests: a compacted session renders the same
  in both clients from snapshot, messages and `/turn`; `/context` is unchanged; a cursor below the compaction gets
  410; a protected session is never compacted; announcements are rate-limited. Done when the steady-state `event`
  table holds only active and recent sessions.
- [ ] **13.6 Migrate an existing large database.** An offline `nexus db compact`, dry-run by default, that requires
  the daemon to be stopped: back up through the SQLite backup API (`nexus.db.bak-<date>`), run 13.1–13.5
  maintenance to completion, `VACUUM INTO nexus.db.new` with `auto_vacuum = incremental`, run `integrity_check` and
  compare the log generation and message, session, snapshot and turn counts, then swap atomically and keep the
  backup. Needs free disk of roughly the backup plus the new file. Files: `cli.py`, `store.py`,
  `docs/development.md`. Done when it is verified on a scratch copy of the real DB (expected here: 177 MB → 2–16 MB);
  run against the real `~/.nexus` only with the user's approval.

Risks: the inspector loses raw wire, deltas and step detail for compacted sessions; JSON exports of old sessions have
no events section; old clients reconnecting mid-response lose missed deltas; per-session deletion makes cursor
validity non-contiguous, so the floor and compaction semantics must be documented; 13.3 changes the stored wire
format, so readers must handle both during the transition.

## Decisions made (2026-11-04, user)

| Decision | Answer |
| --- | --- |
| Manager interaction (8.0) | **Call mode (A):** orb centred, the app dimmed behind it, Esc minimises the call (never ends it, never interrupts a session), an explicit End ends it |
| Scroll pinning (8.0) | **Only a session's first message** goes to the top of the screen; later sends keep the normal behaviour, still without yanking a reader who scrolled up |
| Pending rows (8.0) | **`S` / `Q`** in primary text, and NSH is aligned with the TUI's pending-row UI |
| Event compaction (13.5) | **Yes:** a finished (idle or archived) session's events are deleted, announced by a `session.compacted` record; continuing the session later writes new events as usual. Losing the inspector's raw wire, step timings and the export's events section for compacted turns is accepted |
| CI | Deferred for now |

## Decisions still open

| Decision | Default or recommendation |
| --- | --- |
| Do the Anthropic Messages and Gemini providers ship in a 0.10.x release? | — |
| Does `nexus update` install, or only report? | — |
| Can a parent card steer a child session? | — |
| Output caps: lines or bytes, and configurable? | — |
| Manager Q4: can `navigate` open Settings? | Open Settings to a section to show it; never change a value or start a sign-in. Until decided, it cannot |
| Show the Claude SDK's hidden additions in the starting context; is sending the account email acceptable? | — |
| Persist session grants? | — |
| Turn summaries: show "Thought for Ns" (`reasoning_seconds` is stored, not shown)? | Not shown today, by design |
| Turn summaries: the footer with effort wraps at 140 columns: drop or shorten effort, or accept? | — |
| Turn summaries: store `max_attempts` so a reloaded retry reads "n/max"? | Today reads "retry n in Ns" |
| Fold the starting context after the first message (NSH Void open question 1)? | Leave it open |
| Underline prose links because accent equals primary text (Void open question 3)? | Underline in prose |

## Archived plans

These plan files were merged into this one on 2026-11-04. Read the full text with `git show e438b61:<file>`; code
comments and ledger rows that cite them by name refer to that version.

| File | What it was | Where it went |
| --- | --- | --- |
| `TODO.md` | Post-release backlog: token gap, platform matrix, `nexus` launching the TUI, shell count | Sections 1, 7 and 8.1 |
| `EDITOR_GIT_PLAN.md` | File explorer, editor and Git page | Section 4 (steps 2–6 kept in full) |
| `TURN_SUMMARY_PLAN.md` | Turn summaries instead of loading event history | Section 5 (step 9 kept in full); design in `docs/daemon.md` |
| `TUI_PERFORMANCE_PLAN.md` | TUI render performance, phases 1–4, with measurements | Section 6; results in `nexus-tui/README.md` |
| `VOID_DESIGN_PLAN.md` | NSH Void design, second pass | Landed; leftovers in 8.4; ledger row 2026-11-01 |
| `MANAGER_PLAN.md` | Manager design and milestones | Design moved to [docs/manager-design.md](docs/manager-design.md); milestones in section 3 |
| `INTERFACE_PLAN.md`, `DESIGN_PLAN.md` | Earlier interface spec and design | Archived before: `git show fdb1860:INTERFACE_PLAN.md` |
