# Nexus plan

This is the one plan for Nexus 0.10.x: what has landed, and everything that is still to do. It replaces the
separate plan files and `TODO.md` (see [Archived plans](#archived-plans)). Current behavior is described in
[docs/README.md](docs/README.md); deliberate changes from old Nexus are in the
[porting ledger](docs/porting-ledger.md). The Manager's design is in
[docs/manager-design.md](docs/manager-design.md); its remaining work is tracked here.

Rules that every item below must keep:

- Preserve the visible, ordered context timeline: everything the model receives is shown in order, the UI shows
  nothing the model does not have, wire events are delivered live, diagnostic deletion is announced and clipping is announced. Do not rewrite context
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
| **Tests and lint** | **Red locally:** 7 TUI and 3 Core tests fail, TUI fmt and clippy fail, 2 NSH browser tests fail (11-06: 277/279 pass) | [0](#0-make-the-tests-green) first |
| CI | Red on `main`, NSH not in CI. **Deferred by the user (2026-11-04): do not work on CI for now** | [Deferred](#deferred-ci-user-decision-2026-11-04) |
| Turn summaries | Steps 0–6, 8 and 9 done; 7 partly done. Live partials verified with two TUI joins and shared client tests | Remaining step 7 verification and full NSH app checks — [5](#5-turn-summaries) |
| TUI render performance | Phases 1–3 done, phase 4 partly | Only if a profile asks for it — [6](#6-tui-render-performance-and-launch-time) |
| NSH Void design | Mostly complete; both passes done, since made square (radius 0) | Two open questions — [8.4](#84-nsh-void-design-leftovers) |
| Polish | Modal first pass, compact Settings navigation/focus and brighter Working shimmer done in both clients | Finish modal audit; scroll model, tab/subagent spinners, group fold, iOS — [8.0](#80-polish-user-review-2026-11-04) |
| Editor, explorer, Git page | Steps 0–1 done, step 2 in progress. The TUI app still edits in the plain `Composer` buffer; the vendored editor is used only by `examples/editor_demo.rs`. NSH's Files tab is now the TUI's expanding tree; no Git tab in either client | Wire the vendored editor into the TUI — [4](#4-file-explorer-file-editor-and-git-page) |
| NSH terminal (`nexus nsh tui`) | Launcher, NSH 0.4.0 CLI, parity pass and the 0.4.0 test-host fixes done; the web suite is green except the documented matcher gaps | Remaining visual differences listed in [8.2](#82-nsh) |
| Inspector follow-up, integrated terminal and browser (11-06) | Native inspector first pass landed; terminal/browser are planned, not built | Local baseline → inspector hardening → shared resource contract → interactive terminal → browser; scope/security/rendering gates in [14](#14-inspector-hardening-integrated-terminal-and-browser) |
| Manager | M1–M5 implemented; a real TUI GPT-Live conversation with delegated search/read tools worked (11-04). Audio/context polish built; the header indicator is a gray braille wave field with Stop-on-hover for Live and a dimmed app behind the panel (11-05); latest sound quality not yet listening-verified | Remaining spikes, real-model evals, native/iOS audio, long-call reliability and hands-free — [3](#3-manager) |
| Mermaid (Manager step 0) | Done in both clients | Real terminal image-protocol check, screenshots — [3.2](#32-mermaid-verification) |
| Settings | Compact two-pane dialog; gray selections, white active-pane bar, segmented values (←/→ in the TUI, Tab/Esc to headings); focused checks and screenshots done | Real sign-ins, provider/tool-grid review, native/full browser coverage — [8.3](#83-settings) |
| Providers | Codex, Copilot, Grok, OpenCode, Claude (Agent SDK) work | OpenAI and Ollama real calls; Anthropic Messages, Gemini — [10](#10-providers) |
| Event storage | Shared request components, live-only deltas with client checkpoints, finished-session compaction and incremental reclamation implemented; offline scratch copy 178.2 MB → 2.37 MB | Deployment and any real-home compaction remain explicit follow-up — [13](#13-event-storage-harden-finished-sessions-into-messages-and-flush-their-events) |

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
with that interpreter (python.org 3.14.5 works); `~/.nsh/bin/nsh` was 0.2.0 and is now the NSH 0.4.0 source build at
`3789a45` (`nsh --version` prints `0.4.0 (3789a45)`); the wasm test build needs `llvm-ar` (`rustup component add llvm-tools`, `AR_wasm32_unknown_unknown`),
because Apple's `ar` produces a broken archive.

## Landed

- **Interrupt order and resuming sub agents (11-06).** Seen in `ses_1a1ae77940ddcbfb915`. (1) `Runtime.interrupt` cancelled descendants before the session itself, so a parent waiting on `session` got the child's "interrupted" report as an ordinary result, took in a steer that had been queued an hour earlier, and sent another request before its own cancel landed. Its timeline read tool result → steer → `interrupted`. The session is now cancelled first: the parent stops inside the call, the steer stays queued, and the dangling `session` result names the interrupted child. (2) The orchestrator started a new build child instead of resuming the interrupted one, and shelved the old task. An interrupted or failed child's report, and the parent-interrupt result, now say the child is idle with its conversation and should be continued with `message_session`, and `orchestrate.md` prefers continuing a child whose task the work belongs to, and treats an interrupted task as unfinished. Two regression tests in `tests/test_children.py`; the ordering test fails on the old order with a second parent request. Core suite: 1190 passed.

- **Voice transcript provenance (11-06).** Both clients add the editable note `[Voice transcript — may contain errors.]` once when inserting nonempty final dictation. It is visible in the composer and retained verbatim in the context timeline and model input, including mixed typed/dictated messages. Empty results do not mark drafts; OS keyboard dictation cannot be identified automatically. Shared scenario: two TUI integration checks and eight NSH browser checks pass; composer/timeline screenshots compared in `reports/voice-transcript/`. Full TUI run retains 14 failures in Markdown, Manager, shell and timeline amid concurrent UI work; NSH type check retains the known `app.clock` and `Button.onFocus` errors. No real microphone or provider call was used.

- **Settings segmented values, the shared scroll bar, and the approved composer difference (11-06).** The user decided the three remaining NSH-vs-TUI differences. (1) NSH keeps its composer voice and send buttons; the TUI has none. This is a client-specific difference approved on 11-06 and recorded in the porting ledger. (2) Both clients now have a shared **Scroll bar** option (Settings › Appearance, default on, client scope in the shared manifest): a subdued `interactive.scrollbar` thumb appears in the timeline's right gutter only while the reader scrolls, dims, and hides after about a second. It is overlay drawing, so it takes no content space and does not shift layout. The TUI uses `SCROLLBAR_VISIBLE`/`SCROLLBAR_FADE` on its shared tick and skips programmatic settling; NSH ignores opening/auto-follow/restored offsets and suppresses the first 300 ms of layout settling. The colour is in `interface/theme.toml` with 256/ANSI-16/monochrome fallbacks and a `check_theme.py` presence check. (3) The TUI's Settings choice/toggle rows now draw NSH's segmented control instead of a right-aligned value; ←/→ move the focused segment (wrapping, so Enter still cycles a switch), Tab/Esc return to the rail, and the existing Esc page → rail → close stepping is unchanged. The old cycling-difference row is gone from 8.2 and the NSH README. Validation: TUI shell tests for the scroll bar and segmented controls pass; NSH `timeline-scroll.test.tsx` is 6/6 and `timeline-layout`, `terminal-parity` and `i7` pass except the pre-existing picker-footer wording check. `nsh check` retains only the known `app.clock` and `Button.onFocus` errors; shared manifest/theme checks pass. Total coverage depends on the full suites in the report. Remaining open decision: upstream-framed modals.

- **Native inspector, prompt-cache metrics and execution Gantt (11-06).** Both clients' inspector actions now open a read-only native modal over the existing authenticated API, with Timing, Conversation, Steps, Requests and Raw views. The Gantt shares an absolute axis across turns, model requests, tools, attempts and descendants; zoom, pan, selection/details, copy, refresh and older/newer pages are available in both. Cache counts come from the selected session's durable ledger, with input/cached/uncached and hit percentage; writes and unreported values explicitly say unknown. Shared scenario: 37 focused Core checks, two Rust inspector checks and four NSH browser checks pass; NSH web packaging and shared manifest/theme checks pass. Screenshots compared in `reports/inspector/`. Full Rust run has 15 failures outside inspector (Markdown, Manager, shell and timeline; concurrent UI work was present); NSH check retains the existing `app.clock` and `Button.onFocus` errors. These are refreshable snapshots, not auto-updating timelines; standalone browser Questions/kernel/live controls remain available. No real-home migration, provider calls or Core restart.

- **TUI Manager audio-output startup (11-06).** Live, pipeline speech and notification cues now share a system-buffer output opener, with format fallback restricted to the selected device. Reproduced the screenshot's failure on real hardware: Rodio requested 2,048 frames against a supported 15–960 range; the patched opener succeeds at 48 kHz stereo. Hardware regression check and 24 focused Live/speech/dictation tests pass (two other device tests remain ignored). No provider call or listening-quality verification; NSH's Web Audio path is unchanged.

- **NSH 0.4.0 test host, Settings Esc, Files tree and the scroll model (11-06).** The NSH suite is green except two
  documented tests. Root causes and fixes: (1) the app realm's `location` is `about:blank`, so `open`'s network
  stubs never saw the app's relative URLs and the browser hit the dev server's 404; the web `apiBase()` is now
  `document.baseURI`'s origin and the bare host/start fetches are absolute. (2) `src/dictation.ts` imported
  `Microphone` statically, so tests importing `src/` could not load `nexus/native`; it now looks the module up at
  call time (`nativeModule`), as Mermaid does. (3) A cleanly ended event stream reopened with a "Disconnected"
  flash and reloaded the selected history on every reconnect; it now reopens quietly at once, keeps the view live,
  and reloads history only for a genuinely new stream, a restart or a resync. Settings Esc now steps back one level
  at a time (provider/agent detail → page → section rail → close) with the TUI's segmented hint row. The Files tab
  is the TUI's expanding tree (`src/file-tree.ts`, kept expanded state, `.git` hidden, branch/project header). The
  timeline keeps per-session offsets and viewport height, restores a session where it was left, never yanks a
  reader who scrolled up (`src/scroll-follow.ts`), and anchors the first visible row when heights above it change.
  Also: the TUI's `▣` archived-filter glyph is shared in `theme.toml`, sidebar rows show the branch or linked
  checkout folder, the session-tab wrapper is explicitly `flex-row`, and the right sidebar's context bar falls back
  to the newest request and the catalog window like the TUI's composer. Tests updated for 0.4.0 semantics (body
  strings, missing `toBeCloseTo`, off-screen palette `waitFor`, live Core in the approval card, the TUI's image
  markers, stale tab/rename and model-picker fixtures). Validation: web suite **279 tests, 277 passed, 2 failed
  (9m 42s)** against 206/120/86 before; remaining failures are `child-navigation-parity.test.tsx` "choosing a
  sibling in the strip" (the sibling tab's press does not reach the app) and `void-second-pass.test.tsx` "the
  working row steps aside while this session waits" (pressing a busy session row in that multi-session fixture
  leaves no selection). File-tree and scroll tests pass; `nsh check` reports only the known `app.clock` and
  `Button.onFocus` errors; shared manifest/theme checks pass; the TUI builds and terminal cell frames were compared
  with `--dump-frame` at 120×36 and 80×24. No `nexus migrate`; all Cores used scratch `NEXUS10_HOME`s. The
  items that need a user decision (composer voice/send buttons, upstream-framed modals and the timeline scroll bar,
  Settings segmented controls) were left alone.

- **Resume archived sessions (11-06).** Both clients accept a draft while archived and ask **Resume archived session**, warning about slower, more expensive uncached large context and fresh code kernels. Cancel preserves the draft; Resume unarchives the root tree before sending. Explicit Unarchive also warns without sending. Shared scenario and focused client tests cover cancellation, ordered resume and tree targeting; screenshots compared in `reports/resume-archived/`. Full TUI and NSH type checks retain unrelated failures (see the report).

- **NSH 0.4.0 About and terminal parity (11-05).** The installed source CLI was still 0.3.0 at `09c1632` (`nsh --version`), so the pinned 0.4.0 revision `3789a45` was fetched and built; About now reads `NSH 0.4.0` and `settings-about.test.tsx` covers it. In the terminal backend the sidebar uses the shared `⌕` search control glyph from `theme.toml` (the TUI now reads that glyph there instead of hardcoding it), key hints use the TUI spellings (`ctrl+k`, `ctrl+,`) and the manifest's `keys.tui` bindings are live next to `keys.nsh`, the sidebar footer drops its decorative gear, the Settings active-pane marker is the `▌` cell glyph where a 2 px bar cannot be drawn, and the command palette's selected row carries the same `▌` focus bar as the TUI. `terminal-parity.test.tsx` checks the glyph, labels, the `ctrl+k` switcher and `ctrl+,` Settings bindings, and the footer. Validation: both new tests pass under `--target tui` and `--target web`; `timeline-layout`, `parity`, `chrome-polish` and the Rust shell/theme/palette tests pass (their documented pre-existing failures unchanged); the Core launcher tests pass; `nsh check` reports only the known `app.clock`, `toBeCloseTo` and `Button.onFocus` errors; shared manifest/theme checks pass. Terminal cell frames and matching Rust frames are in `reports/nsh-tui-parity/`. Full web suite: see the 83-failure note in 8.2 (mock request stubs return HTTP 404 under the 0.4.0 test host; `thought.test.tsx` fails the same way alone). No separate terminal layout was added; remaining visual differences are listed in 8.2.

- **Shared NSH terminal-style UI (11-05).** All NSH targets now use Unicode text control icons (sidebar/fold/tool glyphs generated from the shared theme), Monaspace Argon, full-width reading columns with fixed 8 px outer/inner gutters, and a tighter composer. Session search uses an ordinary row instead of overlapping absolute elements. Narrow web/desktop headers use one flexible title, like the Rust TUI, and no longer lose half their width to a redundant spacer. Phone navigation, touch sizes and mobile exclusions remain unchanged. Validation: six focused terminal-backend tests pass; web packaging and shared manifest/theme checks pass. Fresh NSH terminal preview launched on port 8795 and inspected without sending a prompt. Rust TUI suite: 772 passed, seven documented pre-existing failures, seven ignored. Web-target layout tests: three pure checks and the narrow-header check pass; two app checks fail with the existing HTTP 404 network-stub issue. Normal headless attempt timed out during startup. `nsh check` retains the existing `Button.onFocus` and test typing errors. Screenshots compared in `reports/nsh-tui-parity/`: Rust new-session, NSH terminal-cell rendering, wide/narrow packaged web. Native macOS/iOS not rebuilt or device-tested in this pass. No separate terminal-only NSH layout was introduced.

- **NSH terminal launcher (11-05).** Updated the native dependency and installed source CLI to NSH 0.4.0 (`3789a45`); added `nexus nsh tui` (plus `npm run tui`) to run the existing TSX app through upstream's terminal backend, without changing `nexus tui`. The launcher starts Core, checks CLI support, generates with Python directly and forwards backend flags. Relaxed shared Mermaid's `unicode-width` constraint to 0.2 so NSH can use 0.2.3 while the dedicated TUI retains 0.2.0. Validation: seven launcher tests, changed-test Ruff, shell syntax, native `cargo check`, 32 shared Mermaid tests and six host tests pass. The actual command is running in Terminal, connected to Core with the new-session context and composer visible; no prompt was submitted. Upstream warns that desktop title-bar commands and images are unsupported. `nsh check` builds the native crate but fails on the existing `Button.onFocus` error plus updated test typings (`app.clock` nullable and no `toBeCloseTo` matcher). No client layout changes or screenshots; terminal text and runtime log inspected.

- **Braille Manager indicator and panel focus (11-05).** Both clients replace the glowing header orb with a one-row, ten-cell gray braille field: three sine waves travel across it, lighting only a few dots, with idle/thinking/listening/speaking read through speed and brightness (reduced motion freezes it; ASCII keeps one state glyph). While GPT-Live is on, hovering the indicator shows **Stop** and clicking it reuses the existing `live/stop` path, leaving the control call, backend session and typed field open; in the TUI this is a real mouse-move hover. An open Manager panel now dims everything outside it (TUI 40% scrim over the buffer, NSH 40% viewport backdrop), and the consent notice gets the same NSH backdrop. Validation: TUI `--test manager` 35 pass and six indicator unit tests pass; NSH `manager.test.tsx` 49 pass and 18 isolated browser `manager-cards` pass (including hover/Stop and the backdrop); `check_manifest.py` and `check_theme.py` pass; Core `pytest` is 1169 passed, 1 skipped. Full TUI/NSH runs retain the documented shared-tree blockers: seven pre-existing TUI test failures, seven pre-existing TUI clippy errors, the existing NSH `Button.onFocus` type error and the pre-existing Core ruff error in `manager/context.py`. Screenshots in `reports/manager-braille/`: TUI idle/speaking/light fields, the hovered Stop label and the dimmed dropdown; NSH web wave and dimmed panel. The packaged web host does not read the `nexus.manager.dev` live flag (pre-existing), so NSH Stop is covered by the browser test rather than a screenshot.

- **Immediate first submission and stable context header (11-05).** Both clients show the first message immediately as `You · Sending…` below the retained context preview, keep chosen draft settings until confirmed message/history, and restore failed sends to the correct draft. TUI opens creation results before dispatching the prompt; NSH no longer waits for history before posting. Shared scenario: five TUI first-send checks (including 80×24), 76 behavior checks, four isolated NSH first-send checks and five context-header checks pass. TUI/NSH screenshots compared in `reports/first-prompt/`; release built. Full TUI before concurrent Manager edits: 765 passed, seven documented unrelated failures, seven ignored. Final focused TUI verification uses a first-send-only snapshot because concurrent Manager-indicator edits temporarily block compilation. Normal NSH tests retain the native-module loader blocker; `nsh check` retains the existing skill-picker `Button.onFocus` type error. No real-home changes or provider calls.

- **Incoming provider usage merge (11-04).** Preserved usage snapshots alongside storage, live partials, Manager and skill changes; resolved documentation conflicts and kept both usage dialogs centered in the shared large modal class. Validation: 104 focused Core tests, two TUI skill-picker tests, two TUI usage tests and shared manifest/theme checks pass. Server suite: 45 pass, one nested-code approval timing failure. Existing Manager Ruff error and NSH native `declarations` dependency mismatch remain; fresh screenshots were not captured during this merge.

- **Skill picker and nested details (11-04).** Both clients keep skills inline through 12, show only `Skills [N]` above 12, and open a borderless two-column picker (dark-gray cards, lighter gray focus) from the heading at any count. Inline names also open the nested Markdown detail view: full description, complete `SKILL.md`, and supporting file/folder names. Toggles remain separate; Esc restores picker position and focus. Shared scenario: two TUI tests and three isolated NSH browser tests pass; five existing NSH context-header tests and 17 Core skill tests pass. Matching renderer/browser screenshots and keyboard/scroll checks are in `reports/skill-picker/`. Full TUI still has the seven documented failures plus the concurrently changed Manager dropdown Esc test; full NSH check/build remains blocked by the native dependency mismatch, and the unrelated shared app scenarios hit HTTP 404 in the isolated harness. No live-home changes or provider calls.

- **Manager presentation refinement (11-04).** Both clients remove the dropdown outline, use solid opaque surfaces and inset input, and replace the header label with the one-row orb while visible. A Voice toggle defaults on: explicitly opening Manager or switching Voice on starts Live immediately; off preserves typing and the backend session. The orb now opens/closes the dropdown. Validation: 33 TUI Manager checks and 47 isolated NSH controller checks pass; both rendered frames compared, browser toggle/orb interactions pass without runtime errors, manifest/theme checks pass, and the TUI release is built. Native NSH remains blocked by the existing CLI/crate mismatch.

- **Manager top bar and dropdown (11-04).** Both clients move the orb into the first row and show caption/context excerpt/tool/cards with typed input in a notification-style dropdown. Audio, dropdown visibility and native read-only Manager session navigation are separate. Typed first sends do not request Live consent/audio. Esc closes the dropdown; automatic cards do not steal keys. Core ensures/reuses the backend session without a call and protects its prompt route. Validation: 32 TUI Manager tests and 45 isolated browser controller tests pass; 241 Core Manager checks pass with the known local two-peer WebRTC timeout, and the final service/store checks pass (42). Header/dropdown and debug navigation verified in the actual isolated browser UI; TUI/NSH frames compared. Full TUI suite retains seven unrelated failures. Isolated UI card suite: 12 pass, five fail (diagram fallback, test-driver HTTP stubs and fixture loading); native NSH remains blocked by the installed CLI/pinned crate mismatch. TUI release built; real Core restart deferred while another session is busy.

- **Event storage (11-04).** Request manifests share history, schemas, instructions and exact inline media components, with reference-counted cleanup and legacy readers. Token/argument deltas are live-only; Core checkpoints request partials and both clients merge them by request/sequence. Finished-session diagnostics compact atomically with an announcement and cursor resync floor; waits/jobs/code state survive in tables. New databases reclaim free pages incrementally; `nexus db compact` is dry-run by default, backup-first and verified before atomic replacement. Scratch copy: 178,216,960 → 2,367,488 bytes, 44,105 events removed, canonical counts/generation and integrity verified. Focused Core suite: 178 passed. Full Core: 1,158 passed, one skipped, the existing two-peer WebRTC failure. Shared partial tests: four TUI and four isolated NSH browser checks pass; two real TUI joins show identical restored text with zero delta rows (screenshots inspected). Changed-file Ruff passes; full Ruff retains the unrelated `manager/context.py:371` implicit-string-concatenation error. Full TUI retains its seven documented unrelated failures; full NSH build/check and app screenshots remain blocked by the native `nsh-core` declarations mismatch. No deployment or real-home compaction was performed by this task.

- **Manager details modal, immediate connecting and voice barge-in (11-04).** Both clients group the current caption, full bounded model context and formatted tool arguments/status in a scrollable modal below the orb; End stays outside. The orb appears immediately with “Connecting…” and Esc cancels pending starts without late replies reopening them. TUI restores the `nexus01` mic RMS > 0.12 echo-gate override. Validation: 31 TUI Manager tests, 8 audio tests, 46 isolated NSH browser tests, manifest/theme checks; 57 Core Manager/voice/Live tests pass, but the unchanged two-peer WebRTC connection test fails locally (also fails alone). Full TUI suite retains unrelated failures. Real talk-over interruption still needs listening verification.

- **Modal and Settings polish (11-04).** Both clients use three modal width classes and centred placement, with Esc in the footer. Settings sections have no blank gaps; selected rows retain a full-width gray background, and a white bar marks the focused pane (dark in light mode). Left returns to the heading without changing the value; Right advances, Shift+Right steps backward. Shared keyboard/geometry checks and before/after renderer/browser screenshots pass; the TUI build no longer warns about `composer_inner`. Full TUI: 746 passed, seven known failures, seven ignored. NSH verification uses an isolated browser build with native audio disabled; native/full-suite blockers and the remaining modal audit are in 8.0.

- **Manager audio, orb and context excerpt (11-04).** Continuous buffered TUI playback and contiguous NSH packet scheduling (60 ms cushion), 350 ms speaking-state hold, smooth 24-step TUI sprite phases with eased colours and bounded compressed transfers. Both focused stages show the latest three model-context lines and the real tool arguments/status, from redacted `manager.preview` events and restorable state. Core Manager/voice/Live tests: 56 pass, plus the retired-call preview isolation check; TUI Manager: 29 pass; Live: 7 pass; orb: 11 pass; isolated NSH browser Manager tests: 44 pass. Full TUI suite retains unrelated failures; native NSH build is blocked by CLI/pin mismatch. TUI render and isolated actual NSH web stage screenshots compared (unused native microphone stubbed in the web fixture). Core restarted with no active turns; TUI release built. Real audio quality is not yet listening-verified.

- **Live microphone startup and silent audio (11-04).** TUI now explicitly starts its cpal microphone stream, reports startup failures, and gates the mic only on audible samples consumed by the speaker (plus a 200 ms echo tail), rather than the continuously queued silent WebRTC track. DeviceSink shutdown notices are suppressed for Live and pipeline playback. Five TUI audio tests and 43 isolated NSH Manager browser tests pass; real microphone verification is pending.

- **Brighter, faster Working shimmer (11-04).** Both clients use the same travelling highlight and 1.5 s cycle from `interface/theme.toml`, with secondary-text base and a white/dark highlight peak; reduced motion remains static. TUI shimmer and live-group checks pass (full suite: 746 passed, 7 documented unrelated failures, 7 ignored); a browser preview confirms the moving white peak and static reduced-motion state.
- **Provider usage snapshots (11-04).** Core stores the latest provider usage and refreshes it periodically (default 10 minutes, configurable in Settings); both clients show the saved snapshot and offer manual refresh. An already-open modal does not yet update after a background refresh; provider integration and screenshots remain to be checked.

- **GPT-Live sideband certificate trust (11-04).** A real diagnostic reached call creation but the sideband failed with `CERTIFICATE_VERIFY_FAILED` on the python.org interpreter. The WebSocket now uses HTTPX’s verified CA context, and Core publishes startup failures to both clients and status. All 16 Live tests and targeted Ruff checks pass; an unauthenticated TLS handshake succeeds. A new real call remains to be checked.

- **Manager Live failure and orb (11-04).** Both clients preserve Live mode after audio fails, keep the real error and typed input, and never fall into Dictation. Focused stages leave 30% of the background visible. TUI now connects the existing smooth RGBA orb renderer on Kitty graphics terminals, with a centred cell fallback elsewhere. Validation: 28 TUI Manager tests and 11 orb tests pass; 43 NSH Manager browser tests pass in an isolated app without the native crate (the installed CLI cannot build the pinned native dependency). TUI stage/orb preview inspected; NSH screenshot blocked by that dependency. Full TUI suite retains 8 unrelated failures (Markdown, six shell cases, timeline selection). Real-service audio remains unverified.

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

**Next implementation sequence (2026-11-06, user request):** stabilize the local baseline (0), finish the
native inspector as a usable diagnostic surface (14.1), then build a real interactive terminal (14.3)
and an integrated browser (14.4) on a shared resource contract (14.2). This is a plan, not approval to
restart the running Core, install browser engines, expose remote shells or change mobile scope.

| Order | Deliverable | Exit gate |
| --- | --- | --- |
| 0 | Local baseline and short renderer spikes | Failures classified/fixed under section 0; browser/terminal rendering and security decisions recorded before committing to a backend |
| 1 | Inspector hardening | Live refresh without losing position, readable diagnostics, trustworthy cache/timing evidence and bounded loading in both clients |
| 2 | Shared workspace resource contract | Ownership, access, lifecycle, reconnect, input arbitration and model-context rules tested in Core |
| 3 | Interactive terminal MVP | Open a shell in the chosen workspace, run an interactive program, resize, detach/reconnect and explicitly terminate it in both clients |
| 4 | Integrated browser MVP | Both clients view and operate the same Core-owned page, including a local development server, without sharing a personal browser profile |
| 5 | Agent handoff and cross-tool polish | Explicit evidence capture and approved agent actions; inspector traces and resource cleanup verified end to end |

The terminal precedes the browser implementation because it establishes resource ownership and streaming
with fewer browser-specific uncertainties. Run the browser feasibility spike early; do not discover an
unworkable TUI or NSH rendering path after building its backend. Detailed tasks and acceptance checks are
in [14](#14-inspector-hardening-integrated-terminal-and-browser).

### 0. Make the tests green

Fix these locally before new work: they are real regressions or stale tests, independent of CI (which is
deferred).

- [ ] **TUI: not started in a branch yet.** An agent began investigating the failing tests on 2026-11-04 and was
  stopped before committing anything. CI's `tui` job stops at the first failing test binary (the markdown lib test),
  so the shell and timeline failures are hidden there; run `cargo test --no-fail-fast` locally.
- [ ] **TUI formatting:** `cargo fmt` in `nexus-tui` (`live.rs`, `manager.rs`, `dictation.rs`, `render.rs`,
  `tests/manager.rs`). This is CI's `lint` failure.
- [ ] **TUI clippy (`-D warnings`):** `if_same_then_else`
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

### 3. Manager

Design: [docs/manager-design.md](docs/manager-design.md) (section numbers below refer to it). What is built:
[docs/manager.md](docs/manager.md). **Not complete. A real TUI GPT-Live conversation worked on 2026-11-04:** the user confirmed speech/replies, and the stored Manager episode shows real `copilot/gpt-6-luna` delegation through `find_sessions` and `read_session`. The subsequent buffered-audio/orb/context polish is built and tested, but its sound quality still needs a listening check. Other clients and the longer acceptance checks below remain unverified. Decisions D1–D10 and Q1–Q3, Q5–Q7 are settled; Q4 is open.

#### 3.1 Milestones

| # | Delivers | State | Done when |
| --- | --- | --- | --- |
| M0 | Spikes: (a) STT → Luna → TTS latency, (b) Luna through the loop with control-style tools, (c) stage mock-ups, (d) a real GPT-Live call over the Codex sign-in | Scripts written (`nexus-core/scripts/spikes/pipeline_latency.py`, `luna_tools.py`), **not run**; (d) verified in a real TUI conversation, (c) partly reviewed | Findings and screenshots written into the design; effort setting and stage look agreed |
| M1 | State, search and view | Core done | Both clients' own view-reporting tests (`POST /api/client/view`) run and pass |
| M2 | Typed Manager on the stage | Core and clients implemented; latest TUI Manager tests: 29 pass; isolated NSH Manager browser tests: 44 pass (11-04). Full native NSH build blocked by CLI/pin mismatch | A typed Manager runs a whole day of real use; the interaction model is reworked (8.0) |
| M3 | Pipeline voice | Core done | Real microphone and speaker in the TUI and NSH: tap to talk, orb on real levels, thinking cue, playback, cancel; latency recorded (target under 1.5 s from end of speech to first audio) |
| M4 | Attention and acting | Core done | The injection and mishearing suite passes; the approval/question flow works by voice end to end |
| M5 | GPT-Live | Core/clients implemented; real TUI speech, replies and Manager delegation verified (11-04); latest audio polish awaits listening check | iOS and native NSH mic/speaker loop; M0 spike (d) decides the route and the meaning of `speakable`/`commentary`; reconnect/pause polish; a 30-minute real call in each client surviving interruption, reconnect and a long delegation |
| M6 | Hands-free and polish | Audio/orb/context polish started; hands-free and TUI echo cancellation remain unfinished | VAD and barge-in, TUI echo cancellation, iOS check, settings, notices, optional API-key route for GPT-Live; a week of daily use, then consider `enabled = true` |

Detail for each:

- [ ] **M0 (a)** `pipeline_latency.py`: record a WAV, `/api/voice/transcribe`, a one-shot `copilot/gpt-6-luna`
  call, `SpeechService.speak(text)`; log each stage at efforts `none`, `low`, `medium`; write results into
  design section 5.
- [ ] **M0 (b)** `luna_tools.py`: Luna through `nexus/loop.py` with stub `overview`, `find_sessions`, `show` and
  `navigate` over a seeded scratch home; record tool choice accuracy, spoken phrasing, whether ids are read aloud.
- [ ] **M0 (c)** stage mock-ups: orb, a `board` card and a `diagram` card from a fixture in both clients behind a
  dev flag; screenshots side by side; agree the look.
- [x] **M0 (d)** a real GPT-Live call over the Codex sign-in with client delegation (2026-11-04). User confirmed working speech/replies; the stored episode includes real Luna `find_sessions` and `read_session` calls. This does not complete M5’s long-call, reconnect or cross-client checks.
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

- [x] NSH's Files tab is the TUI's expanding tree, keeping expanded state (2026-11-06): `src/file-tree.ts`
  flattens depth-first through expanded folders and skips `.git`, as `App::file_rows` does; `loadFiles` keeps one
  Core listing per directory and `toggleFileDir` expands or collapses in place. The project name and current branch
  head the pane, folders carry the shared fold glyphs and indent two cells per depth, ignored files stay faint, and
  a file's Git letter sits at the row's end. Expanded folders survive tab switches and reset on a project change.
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
- [x] **Step 9: live partial; stop saving deltas.** Implemented and checked (see Event storage in Landed).
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
  - Delta fields gain `request_id`, `part_index` and `seq`. A joining or reconnecting client reads `/turn`, takes the
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
- [x] Old clients reconnecting mid-response will lose the deltas they missed (the message still arrives): noted it
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

##### Session heading row (2026-11-06)

- [x] TUI and NSH: an open session's page starts with a black one-row heading with the full title of its tab
  (wide layouts, root of the viewed session; not Settings or New session). TUI verified in a frame; NSH written
  but not yet run (no browser for `nsh test`). Ledger row added.

##### Modals and Settings: first pass done in both clients (2026-11-04)

Completed:

- [x] Three modal width classes: small 60 / 500, medium 72 / 600, large 88 / 740
  (TUI cells / NSH pixels), clamped to the window gutters. Standard height cap: 70%.
- [x] Centre modals, including the command palette. Row menus remain anchored popovers;
  the file editor keeps its dedicated workspace geometry.
- [x] Remove duplicate Esc hints from modal titles; use the full title width. Input dialogs
  use the shared footer helper, and the TUI model-info panel stays inside its picker.
- [x] Bound NSH scrollable bodies so titles, controls and footers fit in short windows.
- [x] Remove blank rows between Settings sections in the TUI and the matching NSH list gap.
- [x] Keep the current section and setting selected with a neutral gray background across
  the whole row, including the right-aligned value. A **white left bar identifies the
  focused pane**; the inactive selection keeps its gray fill. Light mode uses a dark bar.
- [x] **Value rows are segmented controls, as NSH draws them** (2026-11-06; supersedes the
  2026-11-04 Left-returns rule). ←/→ move the focused segment in the TUI (wrapping, so
  Enter still cycles a switch); Tab or Esc returns to the section rail without changing a
  value, and Esc steps page → rail → close. Detail pages still reserve Left for their
  heading. Shift+Right steps a value backward, and model effort can cycle from the last
  level back to default. Keys and colors live in the shared manifest/theme.
- [x] **A shared auto-hiding timeline scroll bar** (2026-11-06): `interactive.scrollbar`,
  Settings › Appearance › Scroll bar (default on), shown only while scrolling and hidden
  after about a second in both clients.
- [x] Record the shared rules in `interface/design-language.md`, update both client READMEs
  and the porting ledger, and add `interface/scenarios/settings-focus.json` plus the
  browser regression check `nexus-nsh/e2e/modal-layout.mjs`.
- [x] Remove the obsolete `composer_inner` wrapper and use the active geometry helper in
  its tests; the normal TUI build no longer emits the unused-function warning.

Verification performed:

- TUI: focused modal, Settings, composer-geometry and agent-effort checks pass. The full
  `cargo test --no-fail-fast` run has **746 passed, seven known failures, seven ignored**;
  the remaining failures are listed in item 0. Debug build succeeds without the warning.
- NSH: six real-browser palette/Settings layout checks pass at 1024×768 dark, 1440×900 dark
  and 672×432 light. The shared keyboard scenario checks Left-to-heading, no value mutation,
  the white marker, retained gray selection and moving to the next heading afterward.
- Screenshots compared: TUI `--dump-frame` at 80×24, 120×36 and 160×48; NSH at the browser
  sizes above, with additional 1024×768 light and focused/unfocused pane views.
- Shared manifest and theme checks pass (144 commands, 576 contrast checks); touched Rust
  files pass formatting checks. This does **not** establish that repo-wide fmt/clippy is green.
- NSH checks use an isolated actual browser build with unused native audio stubbed. The normal
  native build is blocked by the installed CLI/pinned-crate mismatch (`nsh-core` lacks
  `declarations`). The `i7` mock-based suite returns HTTP 404 under that runtime. Isolated
  type checking still reports existing nullable `app.clock` errors and missing native-module
  declarations; no new Settings/control type errors remain. Native and full-suite verification
  are still open.

Remaining:

- [ ] Give long TUI confirmation dialogs a scrollable body so they can obey the same 70%
  height cap; they currently retain the existing height fallback to keep confirmation text visible.
- [ ] Finish the footer audit across every modal: consistent helper, ordering and
  close/back/cancel wording, including provider and nested-page hints.
- [ ] Review provider detail with pending sign-in links/codes and the full tool grid at small
  and large sizes in both clients; exercise the remaining keyboard-only paths.
- [ ] Capture real PTY/terminal modal screenshots (the TUI evidence above is the real renderer's
  `--dump-frame` output), and repeat NSH verification with the supported native toolchain.

##### Working shimmer: done in both clients (2026-11-04)

- [x] Brighter travelling highlight over secondary text, with a white peak in dark mode and
  a dark peak in light mode. Both clients use the shared `[motion.shimmer]` recipe:
  12 frames × 125 ms = **1.5 seconds per pass**. Reduced motion stays static.
- [x] TUI shimmer/live-group checks pass; the actual NSH component's browser preview confirms
  a moving white peak and an unchanged reduced-motion label. Screenshots inspected in both.

The new tab and subagent spinners below remain separate, unfinished work.

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
| Esc | **Minimise** in the first 2026-11-04 decision; **changed the same day: Esc ends the whole call** (stop speech, cancel the turn, drop the recording/field/card) and never interrupts a session | Must mean something per state (today's problem) | Not used |
| End the call | An explicit **End** under the orb (click, `alt+shift+m`, or saying "that's all") | Same | Releasing the key ends the turn; the call is implicit |
| Talk / interrupt | `space` (or `alt+m` from anywhere) talks; talking over the Manager stops its speech (barge-in) | `alt+m` | Hold `alt+m` |
| Proactive lines | Arrive in the minimised orb as a caption; never take the stage | Same | Toast |

- [x] Chosen: **A, call mode** (user, 2026-11-04). Esc is **end the call**, not minimise (user, 2026-11-04; supersedes the first decision).
- [x] For the chosen model, write the state table in `docs/manager-design.md` 10.1 and 10.6: states (no call,
  focused: listening / typing / thinking / speaking / card; minimised: idle / speaking / proactive; consent) and
  what Esc, `alt+m`, `alt+shift+m`, Space, Enter, digits and a click do in each. Keep four actions distinct: stop
  speaking, cancel the Manager's turn, dismiss the card, end the call.
- [x] Hard rule whatever the model: while a call is open, Esc never reaches the session interrupt.
- [x] One quiet hint line under the orb with the current state's keys, including how to end the call.
- [x] Build it in both clients together (TUI `manager.rs`, `stage.rs`, `orb.rs`; NSH `src/ui/manager/stage.tsx`,
  `orb.ts`), with the manifest updated, `check_manifest.py` run, and tests in `tests/manager.rs` and
  `manager.test.tsx` for every state's Esc. Screenshots of focused and minimised calls in both clients.

Landed (2026-11-04): call mode is in both clients. A focused call is a large centred orb over a 30% scrim, with
the caption, hint and End under it and a card below. **Esc ends the whole call in every state, focused or
minimised** (stop speech, cancel the turn, drop the recording and typed field, clear the card, tear the call down);
it never minimises and never reaches the session interrupt. This supersedes the earlier same-day "Esc minimises"
decision. Minimise remains only as the palette action `manager.minimise`; `/manager` opens a call quietly in the
corner. The four actions are distinct (`alt+s` stop speaking, `alt+c` cancel the turn, `alt+shift+d` dismiss the
card, Esc / End / `alt+shift+m` / "that's all" end the call). The hint line says `esc end` in every state.

- [x] **Esc ends the call** in both clients, every state, with the state table, manifest escape rule, hints, docs and
  tests updated; tests cover focused idle/listening/typing/thinking/speaking/card and minimised idle/speaking/card.
- [x] **Hang fixes:** the TUI no longer joins a microphone capture thread on the UI thread (a stuck device open
  froze keys and frames); Core releases the Manager lock while GPT-Live opens its WebRTC peer and signs the call,
  so `stop`/Esc and status answer at once, and a start that loses its call returns `409 live_cancelled`. Regression
  tests: `dictation.rs::retiring_a_stuck_capture_never_blocks_the_ui_thread`,
  `test_manager_live.py::test_stopping_the_call_does_not_wait_for_a_slow_voice_start`.
- [x] **Manager context modal:** the header Manager button opens a read-only, scrollable debug modal in both clients
  with the hidden episodes and recap, each turn's state snapshot/delta and the recent log; Core formats it
  (`GET /api/manager/context`), clipping is announced, and the inspector keeps the full log. It is not a timeline
  or chat. Tests in `test_manager_context.py`, `tests/manager.rs` and `manager.test.tsx`.

TUI `cargo test --test manager` and the NSH `manager.test.tsx` suite pass; `nsh check` and `check_manifest.py`
pass. Screenshots of focused and minimised calls were captured for the first landing; the context modal screenshots
are in the Manager screenshots set (see the summary in the change that added them).

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

- [x] Implement the model in NSH (2026-11-06): one follow decision (`src/scroll-follow.ts`) leaves follow on any
  upward scroll above the end, so streaming content never yanks a reader; only scrolling back to the end or the jump
  resumes it. Viewport height and end-of-list state are per session; a session opens at its end the first time and at
  its saved offset afterwards (applied again as rows measure). Height changes above the viewport keep the first
  visible row anchored. Tests: `timeline-scroll.test.tsx` covers scrolled-away restore, the no-yank rule and the
  pure follow decision. The first-send pin with a spacer is still to do.
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

- [x] **NSH tests that import `src/` directly** (`App`, `i8`, `context-header`, `voice-settings`; `editor` now
  loads) cannot load `nexus/native` under `nsh test`, because `commands` reaches the statically imported microphone
  module. Fixed 2026-11-06: `src/dictation.ts` looks the microphone up with `nativeModule("nexus/native")` at call
  time (as Mermaid does) and treats a missing module as no capture, so the tests that import `src/` load.
- [x] **Browser suite:** fixed 2026-11-06. The 0.4.0 web test host did not answer `open`'s routes for the app's
  requests: the app realm's `location` is `about:blank`, so relative URLs could not be resolved by the test's
  network shim and went to the browser, which returned the dev server's `HTTP 404`. `apiBase()` now uses
  `document.baseURI`'s origin on the web, and the bare host/start fetches are absolute too. A stream that ends
  without an error reconnects quietly instead of flashing the read-only state between two streams, and a
  reconnect only reloads the selected history for a genuinely new stream. `nsh test --target web` still needs an
  explicit Playwright Chromium path on this machine (`--browser <path>`). Before the fix (11-06, 0.4.0): 206
  tests, 120 passed, 86 failed (8 min 45 s); the count and the remaining failures are in the Landed entry.
  Remaining coverage to add: Enter/Tab steer
  and queue and the pending-input stack, steer numbering, new-session skills/MCP rows, the usage modal, Settings
  search, key entry, link/code rows and Agents edits, child-session picker navigation, project tab isolation.
- [ ] **Design parity:** files (done: expanding tree, kept expanded state), sidebar rows now show the branch, the
  linked checkout's folder, or the project name in the one-project list. Left: new-session view and narrow-layout
  states in the terminal compared frame by frame; sidebar search handling and live shimmer; verify Monaspace Argon
  prose rendering natively. Then build the macOS desktop bundle and complete files, Git and worktree UI (section 4).
- [ ] **Terminal-backend parity (`nexus nsh tui`).** Done in the 11-05 pass: the shared `⌕` search control glyph
  (the TUI no longer hardcodes it), TUI key spellings in the terminal (`ctrl+k`, `ctrl+,`), the TUI keys bound
  alongside the NSH ones, the sidebar footer without a decorative gear, the Settings focus bar as `▌` cells, and the palette's selected row with the shared `▌` bar. Done in the 11-06 pass: the Files tree, the TUI's
  `▣` archived-filter glyph (now shared in `theme.toml` and read by the TUI too), branch/project labels in the
  sidebar rows, Settings Esc stepping back one level at a time (page → rail → close, with the TUI's segmented hint
  row), the timeline's per-session scroll restore with no yank while streaming, the shared auto-hiding timeline
  scroll bar, and Settings value controls that are segmented in both clients.
  Remaining visual differences: (a) the composer keeps the voice and send buttons where `nexus tui` has none —
  client-specific and approved by the user 2026-11-06 (ledger row), with the buttons kept in NSH; (b) upstream
  frames modals, which still needs a decision. The scroll bar and Settings segmented-control differences are
  resolved. A live run against a scratch `NEXUS10_HOME` is still blocked by NSH's native discovery (it reads
  `~/.nexus/daemon.json` and its FS grant is bound to the real home); the terminal frames came from
  `nsh test --target tui` screenshots.
- [ ] Make NSH tool-result images zoomable.
- [ ] Native mic: in-app capture stays web/TUI until the NSH runtime gains audio input; the mic button points to
  iOS keyboard dictation meanwhile.

#### 8.3 Settings

- [ ] Sign in to a real device-flow and a real browser-flow provider from each client's Providers pane.
- [x] Focused shared Settings navigation/layout coverage and renderer/browser screenshots;
  see 8.0 for the checked sizes and native-audio stub limitation.
- [ ] Complete NSH suite coverage after the mock-stream/toolchain fixes, real PTY screenshots,
  and provider sign-in/detail and tool-grid verification.
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

#### 8.6 Project rail: user-defined order

The user wants project tiles draggable in both TUI and NSH, with order persisted and synchronized. **Research
(2026-11-04):** there is already a stable *derived* order: `App::rail_project_ids` in `nexus-tui/src/app.rs` and
`railProjects` in `nexus-nsh/src/project-rail.ts` sort by project creation time then ID. The timestamps are stored,
but no user-defined order was found in the project model (`nexus-core/nexus/store.py`). Other project lists use
recently updated order, so changing that timestamp or the general project listing would have unrelated effects.
The TUI immediately activates `Target::RailProject` on mouse-down; its drag handling is for sidebar resizing and
selection, not project movement. NSH's rail uses a pressable `View`; its `PanResponder` is currently for resizing
the sidebar, not the project tiles. Both clients derive collision-disambiguated rail initials in input order, so
sorting those labels by the new display order would unexpectedly change labels when projects are moved.

Proposed design (not implemented; scope choice below is still open):

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
- [ ] **ZMX mode (subagents only, off by default):** port the Claude Code and Antigravity workers from `nexus01`. See
  [ZMX_plan.md](ZMX_plan.md) (user direction 2026-11-04; replaces the earlier SDK and `opencode-antigravity-auth` idea).
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

**Implementation complete (2026-11-04).** Request deduplication, live-only deltas with partial
checkpoints in both clients, finished-session compaction and space reclamation are implemented.
Steps **13.1–13.3, 13.5 and 13.6** are checked below; the verification record is in Landed.

Remaining follow-up:

- **13.0:** deploy/restart and verify the installed daemon; not performed by this task.
- **13.4:** remove the legacy recovery fallbacks after one release; state-table readers are implemented.
- Complete full NSH app validation/screenshots once its native dependency mismatch is resolved.
- Apply offline compaction to the real home only with the user's approval. Scratch-copy verification
  passed: **178.2 MB → 2.37 MB**, with canonical records preserved and integrity checked.


User request (2026-11-04): the event log has grown very large; old events should not be kept. Only active sessions
need their events; everything else should be "hardened" into messages and full context, and the events flushed.

**Original findings (2026-11-04, before this implementation; measured on a read-only copy of `~/.nexus/nexus.db`):**

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
written promises change under the user's approved decision, recorded in the ledger: `AGENTS.md` "wire events are never dropped"
(becomes "dropped only when announced", with a compaction record), and the inspector's raw wire "for any past
session" (becomes "for active and recent sessions").

**Steps:**

- [ ] **13.0 Deploy what is committed.** Reinstall the tool from the checkout and restart the daemon (the first start
  runs the in-place migrations: 0.3 s on the copy). Done when `nexus doctor` no longer reports different code and
  `/api/info` `retention_status` shows wire payloads expiring. Restarting also rotates the daemon token, which was
  printed into a tool log during this investigation.
- [x] **13.1 Reclaim space automatically.** New databases get `pragma auto_vacuum = incremental` (it must be set
  before the first table). `Retention.run_once` runs a bounded `incremental_vacuum(N)` whenever free pages exceed a
  threshold (for example 8 MB). Files: `store.py` (`prepare_database`, `Store.__init__`, `reclaim_space`),
  `retention.py`, `docs/daemon.md`. Tests: a new DB reports `auto_vacuum = 2`; after expiry and a pass the file
  shrinks; the pass stays bounded. Existing databases convert in 13.6.
- [x] **13.2 Deltas are not stored** (section 5, step 9): `text_delta`, `reasoning_delta`, `tool_call_started` and
  `tool_args_delta` go through `Bus.publish(..., durable=False)`; `LivePartial` and `turn_request.partial` replace
  `stream_summary`. Here that removes 48% of rows. Both clients in the same change.
- [x] **13.3 Stop the quadratic `wire_request`.** Store each request body's `input[]` items content-addressed in
  `event_body`, so a request is an envelope plus item SHAs (each history item is stored once per session), and
  replace `data:` image URIs with SHA references to independent event-body components before storing
  (keeps exact URI spelling and remains self-contained if artifact files are deleted). Optionally fold a request's `wire_event`
  SSE chunks into one stored stream body instead of one row per chunk. Files: `payloads.py` (encode and
  `event_data` reassembly), `store.append_event`, the wire emitters in `llmhub/`, `export.py`, the inspector read
  path. Tests: a reassembled body is byte-identical to the original; N requests store about O(N) bytes, not O(N²);
  export output unchanged; readers handle both formats. Done when the 141 MB session's wire storage is a few MB
  while still retained.
- [ ] **13.4 Move the remaining history readers onto state tables.** State tables and inspector Steps/Questions landed. Legacy `stream_summary` and `turn_end` recovery fallbacks remain for pre-checkpoint requests during the transition; remove after one release. Full NSH visual verification remains blocked by the native dependency mismatch.
  Original scope: Question/permission asked/replied into a small
  `wait` table (also serving the inspector's Questions card); background jobs into a `bash_job` table, replacing
  `open_bash_jobs` / `unfinished_bash_jobs` (which also removes the cold-start full scan in section 6); `code_used`
  becomes a session flag; inspector Steps reads the `turn*` tables (add `http_status` and `ttfb` columns if
  missing); drop the legacy `turn_end` fallback after one release. Files: `store.py`, `recovery.py`,
  `continuation.py`, `runtime.py`, `server.py`, `inspector/index.html`, `docs/inspector.md`. Done when
  `grep "from event"` hits only replay, the outbox and maintenance, and the recovery suites pass with the `event`
  table emptied for terminal sessions.
- [x] **13.5 Harden and flush finished sessions.** A session is compactable when it has no running turn, no pending
  recovery, no open wait or job, no replay pin, no message without its `event_id`, and is archived or idle longer
  than a configurable window (default 24 h, alongside `auto_archive_idle_seconds`). Retention deletes all its
  events and writes one durable `session.compacted` record (the removed id range and counts); cursors that fall in
  a compacted range get 410 and resync; the implemented floor conservatively resyncs every older cursor. Global events stay. This complements prefix-only `prune_events`, which stalls
  behind one protected session. A per-session "keep diagnostics" switch keeps raw wire for a session the user is
  debugging. Files: `retention.py`, `store.py` (`compact_session`), `eventlog.py` (cursor check), `export.py` (an
  "events compacted" section), the inspector ("earlier events compacted" notice), `docs/daemon.md`, `docs/api.md`,
  `docs/stream-events.md`, `docs/inspector.md`, the ledger, `AGENTS.md`. Tests: a compacted session renders the same
  in both clients from snapshot, messages and `/turn`; `/context` is unchanged; any cursor below the conservative compaction floor gets
  410 (even an interleaved retained row); a protected session is never compacted; announcements are rate-limited. Done when the steady-state `event`
  table holds only active and recent sessions.
- [x] **13.6 Migrate an existing large database.** An offline `nexus db compact`, dry-run by default, that requires
  the daemon to be stopped: back up through the SQLite backup API (`nexus.db.bak-<date>`), run 13.1–13.5
  maintenance to completion, `VACUUM INTO nexus.db.new` with `auto_vacuum = incremental`, run `integrity_check` and
  compare the log generation and message, session, snapshot and turn counts, then swap atomically and keep the
  backup. Verified on a scratch copy: **178,216,960 → 2,367,488 bytes**, 44,105 events removed; canonical counts, log generation and integrity match. No live-home write. Needs free disk of roughly the backup plus the new file. Files: `cli.py`, `store.py`,
  `docs/development.md`. Done when it is verified on a scratch copy of the real DB (expected here: 177 MB → 2–16 MB);
  run against the real `~/.nexus` only with the user's approval.

Risks: the inspector loses raw wire, deltas and step detail for compacted sessions; JSON exports of old sessions have
an explicit `events_compacted` marker and only retained events; old clients reconnecting mid-response lose missed deltas; per-session deletion makes cursor
validity non-contiguous, so the floor and compaction semantics must be documented; 13.3 changes the stored wire
format, so readers must handle both during the transition.

### 14. Inspector hardening, integrated terminal and browser

**Status:** planning only, except the native inspector first pass already recorded in Landed.
User requested a starting plan and an integrated browser and terminal (2026-11-06). Here **terminal**
means a real interactive shell inside Nexus, not the existing `nexus nsh tui` launcher or a list of bash
jobs. **Browser** means viewing and interacting with actual pages inside Nexus, not another web-search
tool or merely opening a URL in an external browser. These interpretations are working assumptions;
confirm them at the first design review.

#### 14.0 Boundaries and first review

- [ ] **Inventory and baseline.** Recheck the current tree rather than relying on the dated health table.
  Run focused inspector tests, local Core/TUI/NSH checks and section 0's failing suites; record which
  failures are pre-existing, concurrent or introduced. Fix the relevant blockers before implementation;
  keep CI deferred. Use scratch homes and scripted providers, never a migration of the real home.
- [ ] **Read/reuse deliberately.** Start with `docs/inspector.md`, `docs/api.md`, `docs/daemon.md`,
  `docs/tools.md`, `docs/mcp.md`, `nexus-core/nexus/bash_jobs.py` and `nexus-browser/README.md`.
  Review `ZMX_plan.md`: its external-CLI subagent workers may reuse PTY primitives, but this terminal
  does not enable ZMX mode, change vendor permissions or turn a user shell into an agent backend.
  Preserve the current bash-job and MCP browser-use contracts.
- [ ] **Rendering spikes before architecture lock-in.** Prove a PTY running an alternate-screen program
  in both clients, and a real browser page with scrolling, typing and a click in both clients. Evaluate
  maintained terminal parsers/renderers and Chromium automation/streaming; measure whether NSH's actual
  renderer/native bridge can host them, rather than assuming DOM widgets work in TSX. Record dependency,
  licensing, packaging and platform constraints; do not write a terminal emulator or browser engine.
- [ ] **Approve scope and differences.** Both TUI and NSH must provide the same operations and resource
  state. NSH web/macOS are the proposed first targets alongside the Rust TUI; mobile inclusion or
  deferral is still a user decision, not an inherited file-editor exclusion. Prove terminal graphics
  browser rendering and define a usable non-graphics-terminal path. If a TUI text/screenshot fallback
  loses capabilities, stop for explicit approval and a ledger row before implementing that difference.

#### 14.1 Make the inspector dependable first

The current modal is a read-only, manually refreshed projection. Keep it working while improving it;
do not remove the standalone browser inspector until its useful functions have native equivalents.

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

#### 14.2 Shared workspace resource contract

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

#### 14.3 Integrated terminal MVP

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

#### 14.4 Integrated browser MVP

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

#### 14.5 Agent handoff, inspector links and release gate

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

**First implementation slice:** after section 0 and the scope/security review, harden inspector refresh
and stable paging with one seeded long-running tree. In parallel, run disposable PTY/browser rendering
spikes. Then land resource metadata/access/lifecycle tests before adding shell or browser input. Do not
bundle the entire roadmap into one unreviewable change or block the terminal MVP on optional agent control.

## Decisions made (2026-11-04, user)

| Decision | Answer |
| --- | --- |
| Manager interaction (8.0) | **Top-bar dropdown (latest user revision):** keep Manager label, one-row glowing audio orb, notification cards and typed input, one Open Manager session action for the persistent backend session. Audio is independent; Esc closes the dropdown. Supersedes the earlier centred call stage and Esc-ends-call decision |
| Settings focus (8.0) | **Value rows are segmented; the TUI moves them with ←/→ and Tab/Esc returns to headings.** Keep full-row gray selections and the left bar in white for the focused pane (dark in light mode) |
| Scroll pinning (8.0) | **Only a session's first message** goes to the top of the screen; later sends keep the normal behaviour, still without yanking a reader who scrolled up |
| Pending rows (8.0) | **`S` / `Q`** in primary text, and NSH is aligned with the TUI's pending-row UI |
| Event compaction (13.5) | **Yes:** a finished (idle or archived) session's events are deleted, announced by a `session.compacted` record; continuing the session later writes new events as usual. Losing the inspector's raw wire, step timings and the export's events section for compacted turns is accepted |
| CI | Deferred for now |

## Decisions made (2026-11-05, user)

| Decision | Answer |
| --- | --- |
| Manager header indicator | **Gray braille wave field, not the orb.** Three travelling sine waves, a few dots lit, state read through speed and brightness. While the live voice is on, hover shows **Stop** and click stops the voice (the call, session and typed field stay). An open Manager panel dims everything outside it |

## Decisions still open

| Decision | Default or recommendation |
| --- | --- |
| Browser/terminal product scope (14) | Proposed: actual integrated interactive tools, with Core-owned resources and shared TUI/NSH operations. Confirm at the first design review |
| Browser/terminal mobile support (14.0) | Proposed first slice: desktop/web NSH and Rust TUI. Mobile deferral needs explicit approval; the file-editor mobile exclusion does not automatically cover these features |
| Browser rendering in terminals without image support (14.0) | Spike first. Any text/screenshot fallback that loses capabilities requires explicit user approval and a ledger row before implementation |
| Remote browser/terminal access (14.2) | Require an approved authenticated attachment model; do not enable powerful new resources on a tokenless non-loopback server by default |
| Browser profile persistence and agent control (14.4–14.5) | Start with isolated profiles and user control. Decide profile lifetime, sensitive-page capture and scoped agent takeover before adding persistent sign-ins or raw agent input |
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
| Project order (8.6): rail only, or also the Projects list and project picker? | Rail only; leave their recent-order behavior unchanged until the user decides |

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
