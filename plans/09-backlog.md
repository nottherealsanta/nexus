# 9. Backlog carried over from the previous plan

Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

**Audit 2026-11-11 (first pass, grep/spot-check, boxes not ticked):** of 147 unchecked items, about 22 look done
(9.19 and 9.20 entirely, via [02](done/02-terminal.md); most of the Git-safety items in 9.2; 9.12 Core order/move
API; 9.5 provider limits via [08](done/08-provider-usage-table.md); 9.13 MCP inventory row), about 57 are doable
offline (largest: 9.6 interface polish, 9.11, 9.13 hooks, 9.14 Anthropic/Gemini dialects, 9.16 reliability, 9.17
legacy fallback removal now that 0.12.0 shipped), and about 68 need a device, a paid call, a decision or are deferred.
Confirm each "done" against the code before ticking it.

**Core backlog pass 2026-11-11 (branch `plans/core-backlog`):** the offline Core items below were verified or done; each box says what
was checked. What remains needs a client UI, a device, a paid call or a user decision.

Everything else open in the previous plan is in the sections below, in full.
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

Every open item of the previous plan that is not covered by a section above, kept in full so nothing is lost. Item text is
verbatim from [the archive](../docs/archive/PLAN-2026-11-06.md); `file:line` references inside it point at the code as it was on
2026-11-04 and may have moved. Order inside each area is the old order, not a priority. Not scheduled: pull an area into a
numbered section above when it is started.

## 9.1 Real-world verification (old section 2)

- [ ] **Real daemon with both clients:**
  - model memory with no default model: a draft with no model asks for one (Core's draft-context returns 40 "no
    model selected", so the preview is empty until one is chosen; consider a provider-independent estimate); a
    remembered model whose provider is signed out is shown, not replaced; a pre-change session with no model asks;
  - sign-in from each client's Providers pane, one device-flow and one browser-flow provider;
  - steer/queue ordering, and interrupt returning pending steers and queued messages to the composer oldest first;
  - project tab isolation across projects (browser end-to-end coverage too);
  - `wait: false` child sessions with a real provider.
- [ ] **OSC 7501 program status** in a supporting terminal: reconnect, suspension and several sessions at once
  ([docs/development.md](../docs/development.md#terminal-program-status)).
- [ ] **Audio:** completion and input-needed cues through a real audio device in the TUI and a browser;
  dictation loading/start/stop cues and microphone behavior in both clients; STT microphone capture, a real
  Parakeet download and transcription, and the opt-in `nexus/voice/benchmark.py` (speed, RAM).
- [ ] **Static-only UI changes from 11-03 need a runtime pass:** sticky header isolation, square NSH surfaces and
  the 48 px rail, the shared tab widths, collapsed-paste preview and NSH's Ctrl+X leader. Screenshots of both
  clients at 80×24 / 160×48 and NSH at 390 / 1024 / 1440 px, dark and light.
- [ ] **Drawing modal:** a hand-drawn stroke with the real mouse in Ghostty (injected SGR bytes are checked).
- [ ] **iOS:** a real simulator runtime and a signed device build of the current app; a smaller first message page
  on phones if session opens still feel slow over a cellular tailnet.

## 9.2 File explorer, editor and Git page (old 4.1)

**11-08 follow-up:** initial paired Git UI and one-mode editor are not release-complete. The twelve recorded TUI shell/editor failures are resolved; the full TUI suite passes (857 passed, 10 ignored). See [test cleanup](../reports/test-cleanup-2026-11-08.md). Matching source/Find captures were inspected in [editor parity](../reports/test-editor-parity/README.md); full interaction/performance/visual parity gates remain separate from automated-suite cleanup. Existing unapproved gaps remain open: TUI horizontal scrolling versus NSH soft wrapping, Find below versus above the source, header controls/status wording, and active-match distinction.

Desktop and web only; never mobile. Decisions: the editor stays small (highlighting, undo/redo, no tabs, language servers or Vim mode); Git scope is worktrees, changes and branches with actions (no merge, stash, rebase, conflict UI or hunk staging); both are silent to the model; no new auth. Files and diffs open in a centred modal.

- [~] TUI `FileEdit` now uses the vendored `CodeBuffer` and compiles; pointer/paste/find/replace, behavior tests and screenshot verification remain. Historical starting point (2026-11-04): `src/code_editor.rs`
  (453 lines) is declared in `lib.rs` and used only by `examples/editor_demo.rs`; the app edits through `FileEdit`
  (`ui.rs:273`, drawn by `draw_file_edit`, `render.rs:530`, keys in `file_edit_key`, `app.rs:7560`: Ctrl+S, Tab
  inserts four spaces, Ctrl+Z/Ctrl+Y, arrows and pages) with no highlighting, folds or diff view. Map TUI keys to the crate's `apply(action)`; its crossterm 0.29
  input feature stays off. Copy through OSC 52 and paste from bracketed paste (`arboard` removed).
- [~] TUI find/replace handling is implemented but not fully verified; NSH has the paired two-row Find/Replace controls. Original requirement: find and replace on `set_marks` (today `/` searches only the read-only view, `file_search_key`,
  `app.rs:7783`; check what `ctrl+f` at `app.rs:8080` does while editing) with `editor.find` (`ctrl+f`): plain text and a case toggle, the same
  layout as NSH's find bar (`cmd+f`, next/previous, count, Esc closes it before the modal).
- [~] Both clients now open files in one editable mode; the shared `editor.edit` command was removed. TUI behavior verification remains incomplete. A
  clean file reloads when it changes on disk; a modified one shows "changed on disk".
- [ ] Check the web bundle serves `nsh-syntaxes.bin` (syntaxes load on first use; plain text until then).
- [ ] Measure the TUI binary before and after each language beyond the starting 13 (Rust, Python, JavaScript,
  TypeScript, Go, C, HTML, CSS, YAML, JSON, TOML, Shell, Markdown); anything else opens as plain text.
- [ ] Tests: TUI unit tests for the vendored additions and key mapping, PTY screenshots at 80×24, 120×40 and 160×48,
  `--bench` typing on a 1 MiB file; NSH save and stale-file paths in the browser harness.
- [ ] Sidebar tabs: Git is implemented and present in the shared manifest, but the TUI's full Git tab needs a sidebar at least 33 cells wide and is omitted at the default 28-cell width. Resolve narrow-sidebar access/parity in both clients. Automated coverage checks the width-dependent layout and clicks every manifest tab with a 40-cell sidebar; passing tests do not mark this UI limitation complete.
- [~] New file, new folder, rename and delete are implemented in both clients through shared commands, row menus and prompts. Core confinement/count and focused client tests pass; new-file/delete dialog screenshots compared in `reports/files-operations/`. Broader rename/folder interaction coverage remains. Delete asks first,
  gives the item count for a folder, and is permanent.
- [x] Core file mutations and folder counting implemented with confinement, symlink/race checks, shared tool/API file lock, global workspace events and focused tests: `POST /api/project/{id}/files?session=` with
  `{action: "create_file" | "create_dir" | "rename" | "delete", path, to?}`, the same confinement and no-symlink
  rules as `files.py`; publishes `files.changed`. Limits stay UTF-8 and 1 MiB; binary and oversized files say why
  they cannot open.
- [~] Watcher: `watchfiles` added, leased Files/editor service and both client renew/release paths implemented (60 s lease; 20 s renew), bounded ignore-aware global invalidations and visible polling fallback. Core watcher tests pass; final cross-client scenario checks pending. Original requirement: runs while Files or Git is open, on a 60 s lease;
  250 ms debounce; `files.changed` with `source: "disk"` and `git.changed` for `HEAD`, `index` and `refs`; skips
  Git-ignored paths. If it cannot start (e.g. inotify limits), fall back to a 3 s status poll while the tab is open
  and show that the view is polling.
- [~] `nexus-core/nexus/git_ops.py` and protected project Git routes are implemented, with temporary-repository and API tests. Initial paired UI compiles; full safety-flow/parity verification remains. Uses `worktrees._git` (it reports stderr; `files._git` drops it);
  `worktrees.ENV` already sets `GIT_TERMINAL_PROMPT=0`. `worktree` must be a registered worktree of the project's
  repo; one lock per repo, as `worktree_lock`. Timeouts 120 s, 300 s for network actions; clients show progress and
  never give up first.
- [x] (verified 11-11: `docs/api.md` mutation matrix row, keyed `required`, `mutation_outcome_unknown`; no task or control tool reaches it) Add the route to the mutation matrix in `docs/api.md`: an external effect, so a crash leaves 409
  `mutation_outcome_unknown` and it is never retried blindly. Control-plane only: no task or control tool reaches it.
- [x] (verified 11-11: `test_project_git_live_guard_and_keyed_action` asserts every `git.changed` has `session_id` null; `file_watch` and the action route publish through `bus.publish` with no session) Events `git.changed` and `files.changed` carry no `session_id`, so nothing enters a session's log or
  timeline; confirm against `bus.py`.
- [~] (Core verified 11-11: pull `--ff-only`, push never forces and proposes an upstream, switch never stashes, `branch -d` local only, `-D` needs `force_confirmed`, no amend, discard only regular files, in-progress operations block; the second `-D` warning, the banner and "never delete a branch checked out elsewhere" UI are client work) Safe defaults: pull is `--ff-only` (a diverged branch shows Git's error); push never forces and says when it
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
- [x] (verified 11-11: same test, 409 `worktree_live` with the fresh list, then success once confirmed) Race protection: the request carries `confirmed_live`; if another session started working, Core returns 409
  `worktree_live` with the new list and the dialog reopens.
- [x] (verified 11-11: `worktrees.ENV` sets `GIT_TERMINAL_PROMPT=0`, every Git call uses it; no credential UI exists) Credentials: fetch/pull/push run as Core with Core's environment; without an SSH agent they fail and show
  Git's message. No credential UI.
- [x] (verified 11-11: `tests/test_git_ops.py` 12 tests incl. `--ff-only` on a diverged branch with a bare remote, unmerged `branch -d` refusal, path confinement; `test_server.py` `worktree_live`; dirty-worktree refusal in the worktree tests) Core tests on temporary repos with a bare remote, no network: path confinement, refused dirty worktree,
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

## 9.3 Turn summaries (old 5)

- [ ] **Real-daemon checks:** a real session (`nexus chat`) shows usage and model on every `turn_request` row, and
  `curl` on a real session returns the documented `/turn` shape. The step 1 migration and backfill run once at the
  next daemon start (0.4 s on a copy; usage for 578 of 587 requests).
- [x] (11-11: `docs/development.md` has no stale event-history text left; the floor row is now documented in `interface/design-language.md`; NSH and the TUI use the same words) **Step 7 remainder:** `docs/development.md` where it mentions event history; `interface/design-language.md`
  for the floor row ("the turns above have no recorded details…").

## 9.4 TUI performance and launch time (old 6)

- [x] **Cold Core start no longer scans adopted event history.** `open_bash_jobs` reads the small `bash_job` table after one-time legacy adoption. A restart regression empties diagnostics and verifies open-job reads issue no event query (11-07).
- [x] **Skip unsupported terminal image probes.** `TERM=dumb`, `linux`, `vt100` and `ansi` bypass the query, preserving half-block fallback; other terminals retain capability detection. Unit test passes (11-07).
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

## 9.5 Agent loop, tools and context (old 7)

- [x] **Tiered fuzzy matching in `edit`** (curly quotes, dashes, trailing whitespace, CRLF), and atomic ordered `edits[]`. Exact matches take precedence; ambiguity refuses; failed later replacements write nothing. Tool suite: 16 passed (2026-11-07).
- [x] **Soft nudge at about 80% of `steps`**, and low default effort for `scout`/`quick`. Notice is canonical visible context, emitted once without disabling tools; targeted loop tests pass (2026-11-07).
- [ ] **Code mode:** measure comparable real-provider tasks for correctness, requests, tokens and elapsed time
  ([docs/agents.md](../docs/agents.md#choosing-code-mode)). The live `~/.nexus/agents/build.md` override needs the
  same guidance as `builtin_agents/build.md`.
- [ ] **Explain the 14.7K vs ~3.9K token gap** (was TODO 1). A Claude Haiku 5.5 "hi there" turn read 14.7K input
  (47% cached) against a `~3,965 tok` starting context. Check, cheapest first: restart the daemon (it predated the
  empty working directory and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`) and repeat; capture Claude Code 2.1.314's
  request with a local server ([docs/llm-providers.md](../docs/llm-providers.md#claude-agent-sdk)); check whether
  chars ÷ 4 undercounts the 15 tool schemas plus `StructuredOutput`; look for a user-level Claude Code setting or
  memory loaded despite `setting_sources=[]`. Whatever it is, show it in the starting context or remove it.
- [ ] **NSH after a long streamed turn** keeps its main thread busy 5–7 s rendering it, so clicks wait. Remaining
  cost is the live Markdown row and browser layout.
- [ ] **A lighter context endpoint** (budget only) if `GET …/context` cost shows up again; Core rebuilds the whole
  request body per call (5–12 ms warm, 30–130 ms first read). No Core-side cache: a stale one would show context
  the model does not have.
- [x] (11-11: `POST /api/draft/context` returns `details`: system prompt, AGENTS.md record, full tool schemas, skills, MCP listing; the system prompt equals the first request's; clients can drop their own text) Return per-section starting-context detail text from Core for session drafts.
- [x] Allow `/btw` during a running turn in both clients (implemented in working tree; section 23 tracks remaining verification).
- [ ] NSH browser coverage for opening the original session from a handoff.
- [ ] Decide whether to persist session grants; expose grant listing and clearing in the clients.
- [ ] A Settings section for provider limits; verify `/api/usage` with a real account.

## 9.6 Interface polish and iOS (old 8.0)

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
- Pending-row (steer/queue) hover actions: moved to [31-pending-row-actions.md](done/31-pending-row-actions.md) on 2026-11-11.
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

## 9.7 Both clients (old 8.1)

- [ ] **Audit navigation, pickers and panel interactions** across the clients (the 11-02 audit covered tool
  groups, palette, child rows, banners, rail, footer, composer, starting context, waiting cards and notices).
- [ ] **Ctrl+R prompt history search** in both clients (in the TUI `ctrl+r` is bound to `app.reconnect`,
  `app.rs:8162`, so pick the binding first; Up/Down recall exists in `composer.rs:69`); verify cross-client per-project recall after restarting
  both, including multiline drafts and images.
- [ ] **Subagent parent card:** model/effort, elapsed time and completed token count; distinguish background
  completion from the start result.
- [x] **Background shells and archive** (was TODO 3). Manual archive now refuses a tree with a live background job before changing state. Both clients show the Core error. A real background-job regression test passes (2026-11-07).
- [ ] Review both clients side by side in light and dark; establish large-session performance budgets; test
  keyboard-only paths; decide the focus-ring colour.
- [ ] Agent cards show the file path but cannot open it (the editor reads project files only; global and built-in
  agents live elsewhere).
- [ ] Show the Claude limits in the timeline: a "buffered, no thinking" note for a Claude model and a clear image
  failure (the error text exists; check how each client renders it).

## 9.8 NSH (old 8.2)

- [ ] **Design parity:** files (done: expanding tree, kept expanded state), sidebar rows now show the branch, the
  linked checkout's folder, or the project name in the one-project list. Left: new-session view and narrow-layout
  states in the terminal compared frame by frame; sidebar search handling and live shimmer; verify Monaspace Argon
  prose rendering natively. Then build the macOS desktop bundle and complete files, Git and worktree UI (section 4).
- [ ] Make NSH tool-result images zoomable.
- [ ] Native mic: in-app capture stays web/TUI until the NSH runtime gains audio input; the mic button points to
  iOS keyboard dictation meanwhile.

## 9.9 Settings (old 8.3)

- [ ] Sign in to a real device-flow and a real browser-flow provider from each client's Providers pane.
- [ ] Complete NSH suite coverage after the mock-stream/toolchain fixes, real PTY screenshots,
  and provider sign-in/detail and tool-grid verification.
- [ ] State the required Claude Code version (2.1.248 or newer) and show "Claude Code is not installed" on the
  Providers page when `cli_path()` is empty.
- [ ] Real Settings panes for MCP servers, skills and hooks when those controls exist; they are deliberately absent.

## 9.10 NSH Void design leftovers (old 8.4)

- [ ] **Fold the starting context once a session has messages?** Default: leave it open and delete leftovers if
  any reappear; the alternative is folding to `Context · 5 items · ~5.4K tokens` in place. See
  [Decisions](decisions.md).
- [ ] **Accent equals primary text,** so links look like plain text. Default: underline links in prose.

## 9.11 TUI (old 8.5)

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

## 9.12 Project rail order: verification (old 8.6)

The feature landed on 2026-11-06 (Core order, move API, drag and menu in both clients). The old plan said not to mark it landed until verified; this is that verification.

- [~] (Core verified 11-11: `test_project_order_status_and_snapshot_share_one_saved_order` and `test_project_order_survives_a_restart`; client reducers and verification pending) **Core order and synchronization.** Persist a user-defined ordering of visible project IDs in Core, initially
  equivalent to creation time then ID. Append new projects; remove deleted projects; never expose the Manager's
  hidden project. Preserve existing project `created`/`updated` and revision semantics. Include the order in the
  consistent `/api/snapshot` and publish a durable global event on changes, with reducers in both clients; ensure
  snapshot watermark and event ordering cannot lose a move. Keep older snapshots/servers usable with the existing
  creation-order fallback, and surface unsupported or failed mutations rather than pretending they succeeded.
- [~] (Core verified 11-11: move by neighbour/end, validation 400s, replay does not overwrite a later move, concurrent moves apply to the current order; client reconciliation pending) **Move API.** Define and document an atomic, idempotency-keyed move by project ID and intended neighbor
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

## 9.13 Extensions, skills, MCP and hooks (old 9)

- [~] (Core 11-11: `/api/agent`, `/api/extension` and `/api/skill` already return `problems` rows for the clients to show, display still open; the changed-file notice is in the tool result and tested (`changed on disk`); user tools get `MEMORY_MB` (default 2048, `RLIMIT_AS`, Linux-enforced, not verified on macOS); `[tools] disabled` accepts project-only names and a session skips names its project lacks, instead of dropping every entry; `test_reload_stress_does_not_accumulate` passes. Open: the client display, a real-model session) Show agent and user-tool errors in client pickers and Settings; a visible changed-file notice for a running
  tool; process memory limits; validate project-only names in `[tools] disabled`. Exercise a user tool in a real
  session and run the reload stress test.
- [ ] Default-on settings for skills; exercise skills with a real model; decide whether and how `allowed-tools` is
  enforced without changing a cached prompt or granting permissions.
- [~] (Core 11-11: read-only `GET /api/mcp/{name}/resources|resource|prompts|prompt` and the project-scoped twins, fenced, bounded, tested against the stdio fixture; no model tool and no client UI yet, those need a decision on how a model should reach them) MCP resources and prompts; Settings screens for adding/editing servers. Output stays bounded, fenced as
  untrusted and visible as received.
- [ ] Exercise browser-use (the pinned `uvx` package, `nexus-browser/`) in a real model session, including server
  failure, screenshots and its LLM-backed tools.
- [x] (verified 11-11: `test_a_disabled_global_name_does_not_hide_an_enabled_project_server` and `test_enabling_a_global_server_after_a_project_entry_takes_the_name_back` pass) Verify the new-session inventory prefers an enabled project MCP row over a same-named disabled global row
  (the `browser-use` case).
- [~] (Core 11-11: `SessionEnd` fires on archive (background, payload `reason`), `bash(...)` matchers see every part of a compound command, `PostToolUse` fires with `is_error` after a denied approval, all tested; open: Settings editing, timeline rows, a real-model blocking hook) Hooks: edit them in Settings, render hook rows in both timelines, fire `SessionEnd` on archive; test
  `bash(...)` matchers on compound commands and `PostToolUse` after a denied approval; a blocking hook with a real
  model.

## 9.14 Providers (old 10)

The ZMX item is section 6.

- [ ] **Real OpenAI and Ollama calls** (also in section 2).
- [ ] **Claude (Agent SDK) remaining:**
  - [x] Record the installed Claude Code CLI version in wire metadata, without adding prompt text. Bounded cached lookup invalidates on binary change; 24 offline Claude tests pass (11-07).
  - decide whether to show the SDK's hidden additions (`sdk_adds`: two system lines, `StructuredOutput`, the
    account's email in the first user message, an `# Environment` message) in the clients' starting context, and
    whether sending the email is acceptable; the 420 tokens of fixed overhead are unexplained;
  - confirm the context window the clients show for `claude-haiku-5-5` (the CLI logs `unrecognized_model` but
    serves it);
  - [x] Confirm cancellation kills the worker process group and simulated CLI child: a real subprocess regression leaves no delayed child output (11-07; no provider call).
  - re-check in NSH; mark the provider `verified` after a multi-tool turn with a real model and add wire fixtures.
- [ ] **J4 Anthropic Messages:** the Anthropic SDK as a plain client (Nexus owns the loop); preserve signed thinking
  blocks for lossless replay; cache-control breakpoints and tool-result media.
- [ ] **J5 Gemini:** a dialect that preserves replayable reasoning.
- [ ] **ZMX mode (subagents only, off by default):** port the Claude Code and Antigravity workers from `nexus01`. See
  [ZMX_plan.md](zmx-design.md) (user direction 2026-11-04; replaces the earlier SDK and `opencode-antigravity-auth` idea).
- [ ] Every provider needs wire fixtures, provider/auth docs and a verified real call; group OpenAI-compatible ones.
- [ ] Cross-provider conversion beyond text and tool calls (foreign reasoning is dropped); Copilot Responses
  encrypted reasoning replay.
- [ ] Revisit Responses `instructions` if the Codex backend changes (it accepts it, though the Codex CLI sends a
  developer message).

## 9.15 Worktrees (old 11)

- [~] (Core and API 11-11: `GET /api/project/{id}/worktree/review`, keyed `POST .../worktree/integrate` (ff or `--no-ff`, pinned to the reviewed head, optional removal and `branch -d`), 9 tests, ledger row, redesigned on Git rather than the old file journal; open: client commands and dialogs in both clients, and agent exposure (the `worktree` control tool is ungated today, so an integrate action there needs an approval design first, see decisions)) Port worktree review and integration as control commands: review against the base, merge by fast-forward or
  merge commit with confirmation, remove the worktree and branch after merge. Commands in both clients; agents get
  them only when granted. Reference: old `nexus/agents/worktree_review.py`, `worktree_integrate.py`,
  `nexus/worktree_cleanup.py`, `tests/test_worktree_merge_permission.py` (in `~/repos/nexus-old`).
- [~] (11-11: archive and session delete never touch a worktree, documented and unchanged; what to offer when leaving one stays open) Decide cleanup on archive/delete (archive must never implicitly delete a worktree) and how to leave one.

## 9.16 Reliability and safety (old 12)

- [ ] Monty: an OS-level sandbox if wanted; checkpoint recovery; forced worker crashes; cold-start latency; the
  Linux `/usr/bin/env` path. (Workers already start through a launcher with an empty environment.)
- [~] (11-11: `bash.started` records the pid; startup recovery probes each unfinished job read-only by pid, group leader and start time, and says running, gone or unknown in the notice and the closing `bash.finished`; it never signals; 3 real-process tests. Not done: re-adopting a surviving job so Nexus can stop it, which would need a durable output spool) Supervise and report background bash jobs across daemon crashes; never kill a process from a stored PID
  alone.
- [ ] Durable pending directory moves and worktree-in-use checks for external processes; archive transitions
  appear only in the root log today.
- [ ] Archive: bulk/project operations or timestamps, if needed.
- [~] (11-11: the JSON event log stops at 256 MiB of event data or 300 s of held snapshot with `events_clipped` per session; messages and snapshots are never clipped; tested. General artifacts not done) Export holds a long SQLite read snapshot for very large sessions; consider general artifacts and export
  clipping.
- [ ] Whether read-only agents need sandboxing or restricted command execution beyond the file tools, including
  races with external writers.
- [ ] Retention: deferred partial indexes still cost one initial table scan each. Space reclamation and event
  compaction are section 13.

## 9.17 Event storage (old 13)

Decided 2026-11-04: finished sessions' events are compacted, announced by a `session.compacted` record. The offline scratch copy went 178.2 MB to 2.37 MB; deployment to the real home is the explicit follow-up.

- [ ] **13.0 Deploy what is committed.** Reinstall the tool from the checkout and restart the daemon (the first start
  runs the in-place migrations: 0.3 s on the copy). Done when `nexus doctor` no longer reports different code and
  `/api/info` `retention_status` shows wire payloads expiring. Restarting also rotates the daemon token, which was
  printed into a tool log during this investigation.
- [~] **13.4 Move the remaining history readers onto state tables.** State tables and inspector Steps/Questions landed. The legacy `stream_summary` and `turn_end` recovery fallbacks were removed 11-11 (0.12.0 shipped; recovery reads `turn.ended` and the `partial` checkpoint only; the recovery suites and the full Core suite pass). The `grep "from event"` done-test was not re-run. Full NSH visual verification remains blocked by the native dependency mismatch.
  Original scope: Question/permission asked/replied into a small
  `wait` table (also serving the inspector's Questions card); background jobs into a `bash_job` table, replacing
  `open_bash_jobs` / `unfinished_bash_jobs` (which also removes the cold-start full scan in section 6); `code_used`
  becomes a session flag; inspector Steps reads the `turn*` tables (add `http_status` and `ttfb` columns if
  missing); drop the legacy `turn_end` fallback after one release. Files: `store.py`, `recovery.py`,
  `continuation.py`, `runtime.py`, `server.py`, `inspector/index.html`, `docs/inspector.md`. Done when
  `grep "from event"` hits only replay, the outbox and maintenance, and the recovery suites pass with the `event`
  table emptied for terminal sessions.

## 9.18 Inspector hardening (old 14.1)

The native inspector first pass landed 11-06; these are the remaining gates.

- [~] **Shared navigation and readable views.** (Clients landed in TUI and NSH: Questions view, per-view state kept across refresh/view/session switches, tree selector, `q`/`kind`/`status`/`type` filters, `summary` rows with expandable `detail` and on-demand `/inspect/record`. Pending: links from Questions into the kernel/job modal.) Preserve selection, zoom, expanded details and scroll
  while refreshing or switching back. Add a session/tree selector and search/filter by request, tool,
  status and event type. Render structured summaries with expandable exact JSON rather than making
  long JSON dumps the only way to understand Conversation, Steps or Requests. Bring pending/replied
  Questions and links to existing kernel/job details into the native inspector. Keep canonical model
  context distinct from diagnostic partials and resource views.
- [~] **Incremental live data.** (11-07: both clients coalesce invalidation from the existing SSE stream; no token-delta or known unrelated-tree refresh, closed inspectors do no work, snapshot resync invalidates. Existing stream owns durable cursor replay. NSH inspector suite: 11 passed. Reconnect/overflow scenario gate remains open.) Use existing session events to invalidate only affected inspector data;
  coalesce refreshes and cancel/ignore stale requests on navigation or close. Reuse durable cursors and
  explicit snapshot resync on overflow/retention changes. No second unbounded event log, no repeated
  full-tree fetch per token, and no background refresh while the inspector is closed. Keep a visible
  paused/live/disconnected state and manual Refresh; an offline view must not look current.
- [ ] **Stable, bounded loading.** (11-11 Core tests added: Questions paging under new writes, Raw paging while retention deletes rows between pages; the existing test covers the other views. Remaining: client memory/render checks.) (Core landed: inspect API v2 with opaque cursors, byte/lane/tree bounds, `omitted`, `/inspect/record`, `/inspect/head`. Clients landed: cursor stack paging, `omitted` and notes shown, at most 60 rows drawn around the selection. Client memory/render-latency regression checks still pending.) Replace offset-only paging with documented stable boundaries/cursors
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

## 9.19 Terminal and browser: shared resource contract (old 14.2)

Supports section 2 (terminal). The browser parts are deferred.

**Verified 11-11:** every item below is ticked on the evidence in [02](done/02-terminal.md) (its boxes are ticked) plus a re-run of the six terminal suites (46 passed). The Linux/other-platform, native AppKit and remote reconnect matrix stays open in 02.

- [x] **Resource model and authority.** Define Core-owned terminal/browser resources with opaque IDs,
  project/workspace identity, creator, optional session association, status, timestamps and explicit
  create/attach/detach/close actions. Clients are viewers/controllers, not process owners. A session
  changing directory must not silently move an existing shell or browser; new resources use the chosen
  workspace and existing ones show their actual location/association.
- [x] **API and streaming design.** Specify authenticated resource metadata/control endpoints and a
  versioned data stream, separately from the conversation SSE log. Include sequence numbers, bounded
  replay/checkpoints, explicit gap/resync messages, backpressure and read-only attachment. Keep raw
  PTY output and browser frames out of durable session-event rows. Input actions need IDs/acknowledgments
  so reconnect/retry never repeats a command, click or form submission.
- [x] **One active writer.** Allow multiple viewers but give only one user/client/agent explicit control
  at a time. Show who owns control; handoff/revocation is explicit and auditable. Reject stale resize,
  pointer and keyboard input from an old attachment. A disconnect releases control according to the
  documented lease policy; it never silently transfers control to an agent.
- [x] **Lifecycle, limits and security.** Decide idle limits, concurrent-resource limits, archive/delete
  behavior and graceful shutdown before coding. Closing a panel is detach, not kill; explicit Close
  terminates a resource. For the first version, a Core restart may end processes/contexts but must mark
  them ended/interrupted, never pretend to resume them. Test orphan cleanup. Guard stream origins,
  attachment authorization and cross-project access; do not expose a remote shell or authenticated
  browser over the existing tokenless non-loopback mode without an approved access model.
- [x] **Visible context boundary.** A terminal/browser panel is a workspace resource, not another chat
  timeline. Opening it or viewing its output does not automatically feed the model. Explicitly attached
  output, page extracts and screenshots become normal visible messages/tool results with provenance,
  clipping and untrusted-content treatment. Separate UI resource-management APIs/control tools from
  workspace task actions; do not add task tools that mutate Nexus sessions, models or workspace state.
- [x] **Shared shell placement.** Design a shared workspace panel with Terminal/Browser tabs, a resource
  list, split/expand/collapse and focus return, using the existing file-panel/layout machinery where
  appropriate. Keep the session composer draft and scroll unchanged when switching. Put commands and
  shortcuts in `interface/manifest.toml`; arbitrate terminal/browser input versus global shortcuts, IME,
  mouse, copy/paste and Esc without sending a keystroke to both the resource and Nexus.

## 9.20 Integrated terminal MVP (old 14.3)

The detail behind section 2.

- [x] **Core PTY service.** Add a dedicated resource service (proposed
  `nexus-core/nexus/terminals.py`) for shell spawning in the selected workspace, terminal dimensions,
  environment, process groups, binary-safe I/O and resize. Preserve UTF-8 split across chunks and
  arbitrary terminal control bytes. Set a defined terminal type; support signals, exit codes and
  graceful-close/force-stop. Do not repurpose `bash_output`'s combined text buffer as a PTY.
- [x] **Shared terminal semantics.** Choose a maintained VT parser/screen checkpoint representation
  and compatible renderers for NSH and Rust. Verify colors, Unicode/wide characters, cursor movement,
  scrollback, alternate screen, bracketed paste and interactive programs (`less`, an editor, a shell
  prompt). Bound scrollback and queued bytes; announce discarded history. Disable or gate terminal
  escape features that write the clipboard, access files, or launch URLs without the user's action.
- [x] **User interaction.** Open/list/rename/switch/close terminals; show cwd/workspace, running/exited
  status and active controller. Support selection/copy/paste, resize, Ctrl+C and focus handoff. Warn
  before terminating a running shell/process. Detach/reconnect restores a bounded screen checkpoint
  plus subsequent bytes without replaying input. First version is user-operated; agent raw typing is
  deferred until the authority model in 14.5 is settled.
- [x] **Exit gate.** Tests run real PTYs against an isolated workspace: shell input/output, interactive
  alternate screen, resize, split byte sequences, high-volume output, two clients, control contention,
  disconnect/reconnect, exit, close and Core shutdown. Compare both clients' screens and input behavior.
  A terminal is not done if it only renders line-oriented command output.

## 9.21 Integrated browser MVP (old 14.4), deferred

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

## 9.22 Agent handoff and release gate (old 14.5)

Terminal evidence capture and agent-control policy apply to section 2 as well.
**Terminal slice implemented (11-08):** explicit immutable selection preview and draft insertion in both clients;
exact-tool-linked Timing lanes, sanitized lifecycle/control invalidation (including restart), and a documented
user-only interactive-control policy. Real-Core input/capture smoke, paired screenshots, 58 focused Core tests,
full TUI suite and focused NSH suites pass. Browser portions stay deferred; cross-platform/native/remote and
final consolidated green-suite gates remain open. See [terminal report](../reports/terminal-work-2026-11-08.md).

- [~] **Explicit evidence capture first.** Add user actions to attach selected terminal output, a page
  extract or a screenshot to the current draft. Preview the exact payload, source resource and omitted
  bytes/lines before sending. The user's normal send commits it into canonical context; live panel
  refreshes must never silently replace content already sent to a provider.
- [~] **Agent control only after policy review.** Classify terminal commands, raw keystrokes, browser
  navigation/form actions and credential-bearing operations. Prefer existing approved `bash` and MCP
  paths where they suffice. A raw interactive shell cannot reliably enforce per-command allow patterns;
  do not present those rules as protection or turn approval of one command into indefinite shell access.
  Define grant scope/revocation, destructive actions and takeover behavior, with visible tool arguments,
  outcomes and failures in the ordinary timeline. Browser content and terminal output remain untrusted
  data, never instructions or implicit permission.
- [~] **Diagnostic integration.** Link resource actions and captured evidence to session/turn/tool IDs
  when they actually belong to one. Add recorded browser/terminal spans to the inspector Gantt without
  inventing model calls or token usage. Keep full streams private/bounded outside the timeline; retain
  only the explicit model-visible payload and bounded diagnostic metadata under the existing policies.
- [~] **Release gate.** Core store/API/access/lifecycle tests, shared scenarios, TUI tests, NSH browser
  tests, manifest/theme checks and both builds pass for the changed paths. Run matching screenshots,
  failure/reconnect tests, high-output/large-page profiling and a cross-client handoff demo. Document
  unsupported platforms/capabilities and approved differences. Update both client READMEs, API/daemon/
  tools/inspector/MCP docs and the ledger where behavior changes. Land in vertical slices, each with a
  usable backend and both clients; no mock terminal/browser can be marked MVP complete.
