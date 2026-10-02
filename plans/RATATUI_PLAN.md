# Ratatui replacement: progress and completion plan

Last updated: 2026-10-02 (Asia/Kolkata), after the visual-matching pass.

## HANDOFF: read this first

State: a broad, working native client that looks and behaves much like Textual in
the default transcript, composer, sidebars, permission panel and pickers, but
**many Textual features are still missing or only approximately matched**. Do not
switch off Textual. `nexus chat` defaults to `--renderer auto` (native if its
binary exists, else Textual). Everything is uncommitted on `feat/ratatui-prototype`
in `/private/tmp/nexus-ratatui-prototype`.

How to work: use the project skill `.agents/skills/nexus-ratatui/SKILL.md` (and
its `reference/`). Textual is the reference; match it, and put shared pure logic
in `nexus/ui_support/` so both clients use it.

Last verified (2026-10-02): Rust 11 tests pass; Ruff clean; full offline suite
4,974 passed / 312 skipped, with 3 failures that were fixed or were timing-flaky
(see "Aggregate regression" below); Playwright captures of reference, permission,
picker, panel, light and narrow screens complete. Not rerun after the last small
fixes: the whole suite and the Textual timing-sensitive group.

### P0: correctness and trust (do first)

- [ ] (static audit done: every host result consumer in `nexus/ui/ratatui/` checked against `host/protocol.py` types, no further mapping-on-struct found; hand-driven run with a real provider still open) Run the app interactively end to end with a real daemon and a real provider
      (nobody has driven it by hand yet; all evidence is tests and browser
      terminals). Fix whatever breaks. The first manual run already found a crash
      (`DoctorResult` has no `get`), so expect more struct-vs-dict mistakes: grep
      `nexus/ui/ratatui/` and `ui_support/details.py`/`context_header.py` for code
      that treats host results (`DoctorResult`, `ContextInspectResult`, `*Result`)
      as mappings.
- [x] Make the update loop resilient (done: per-section guards in `project()`/`poll()`; failures become a labelled notice): an exception inside `project()` or `poll()`
      currently surfaces as a one-line `Error:` and may stop updates. Catch per
      section, show a labelled notice, keep the UI alive.
- [x] (reviewed 2026-10-02: the digest/discard edits are narrow and safe; paging note: the host exposes `has_more` but not `next_cursor`, so `cursor + len(page.diff)` can undercount if rows are dropped by the byte budget or decode errors. Not reproduced; fix by returning `next_cursor` from the host) Review the three host edits made by a subagent: `nexus/host/facade.py`,
      `nexus/host_support/worktree_projection.py` (`review_hex`), and
      `nexus/agents/worktrees.py` (`_inspect` for discarded children). They fix real
      bugs (review digest redacted to `***`; listing after discard) and also affect
      Textual. Also look at the suspected Textual paging bug in
      `tui_widgets.py` (`cursor + len(page.diff)` vs host 128 KiB pages).
- [x] (fixed: newest build wins; `nexus doctor` prints `native tui:`) Binary discovery prefers `rust/tui/target/debug` over `release`; a stale debug
      build can shadow a fresh release build. Fix the order or print which binary ran
      (`nexus doctor`).
- [ ] Rebase onto `main` (0.2.17; this branch still says 0.2.16) before any release
      candidate. Do not edit version files by hand.

### P1: Textual features not yet in the native client

Compare each against `nexus/ui/tui/` and `ui_support/tui_*.py`.

Transcript
- [ ] Inline diff under Edit/Patch rows with Textual's line numbers, hunks, context
      and "clipped" notice (`ui_support/tui_diff.py`); native shows a plain
      before/after split.
- [ ] Running-tool spinner and live tail animation (rows are static between
      snapshots; add a tick or a `spinner` frame sent by Python at ~8 Hz only while
      a tool runs).
- [ ] Keyboard focus navigation through the transcript (Tab to tool cards, thoughts,
      user cards; Enter/Space open or toggle; hover/focus highlight like
      `.tool-card:focus`).
- [ ] Empty-session hints (`EmptyHints`), connection-status banner, activity progress
      bar, update-available notice, `thinking_status` "Activity" row.
- [ ] Mouse text selection and copy in the transcript; scrollbar indicator; typing
      scrolls to bottom (`set_typing`).
- [ ] Markdown: code fence background and language label, block quote bar, headings
      colour/levels, table layout, list hanging indent, inline code styling
      (`app.tcss` `.timeline-assistant ...`). `markdown.rs` is minimal.
- [ ] Submitted-attachment chips: verify against Textual (`UserMessage` chips) and
      that clicking opens "Attached context".

Details and sessions sidebars
- [ ] Modified-files rows: expand/collapse with diff preview, click and keyboard,
      "+a -r across N files" (data is already projected; add `expanded` state and an
      operation).
- [ ] MCP refresh control (`↻`) and live doctor refresh; update notice.
- [ ] Sessions sidebar to match `SessionSidebar` (`tui_panels.py`): `SESSIONS N`,
      status words (working now / needs input / finished), relative times, archive
      action, project and day grouping, first-1000 truncation notice, scroll.
- [ ] Session tabs to match `SessionTabs`: verify look, close button, overflow,
      active styling, status dot; currently approximated.

Composer and input
- [ ] Editor height should grow with content (Textual `max-height: 22`); native is a
      fixed 8-row composer region.
- [ ] As-you-type completion (inline list under the composer, `tui_list.py`) rather
      than Tab-only popup; style the popup like Textual; argument providers exist in
      `ui_support/completion.py`.
- [ ] Model picker: group header rows, fuzzy ranking in the Rust filter (Python
      already ranks), Ctrl+S sort toggle, effort step styling (`tui_model_picker.py`).
- [ ] Agent picker as the inline `AgentPickerPanel`, new-session picker
      (`new_session.py`), sessions dialog, archived dialog (`tui_archived.py`).
- [ ] Voice: floating `VoiceStrip` overlay above the composer, level meter, partial
      preview, recording marker in the runtime row; verify cancel races.
- [ ] Logs drawer: Textual docks it right (36 cols); native uses the lower half of
      the transcript. Match placement, title bar and close button.
- [ ] Leader (Ctrl+X) hint line styling and timeout behaviour.
- [ ] Ctrl+P: Textual opens a command palette widget; native opens a `/help` menu.
      Port palette behaviour (fuzzy search over commands and shortcuts).

Dialogs and screens (all currently generic panels/menus)
- [ ] Tool details modal (`ui/tui/tool_details.py`), Context modal with the
      "Edit <category>…" button, Tools modal (grouped families, token column,
      swatches), Skills/MCP extension modals (toggle, lock, counts),
      shortcuts screen.
- [ ] Settings console: full-page two-pane layout (areas on the left, editor on the
      right), Appearance and Layout panes with reset buttons, agent editor form
      fields (model, fallbacks) instead of a raw file, validation display. Workflow
      logic is verified; presentation is not.
- [ ] Providers and first-run setup screens, worktrees screen (review pages,
      acknowledge, integrate, discard, force-discard confirmations), usage screen,
      agent transcript page, archived browser: presentation unverified.

### P2: verification we still owe

- [ ] Side-by-side screenshots for every screen above, dark and light, at about 60,
      120 and 200 columns, saved under `artifacts/ratatui-parity/` and reviewed by
      eye. Add states to `tests/ratatui_browser_demo.py` and the check script.
- [ ] A populated context-header screenshot (the fixture header was off-screen).
- [ ] Add ratatui `TestBackend` buffer snapshot tests (text grids) for key screens so
      layout regressions fail in CI; the Playwright check only captures PNGs and
      asserts almost nothing.
- [ ] Full interaction audit of focus handling, small-terminal prompts, long and
      multi-question prompts, resize during modal, reconnect during streaming,
      session switch during tool run.
- [ ] Extend `tests/test_ratatui_pty.py` for the new composer/prompt layout, mouse
      clicks on tabs/sessions/prompt choices, and the leader key.
- [ ] Performance: measure end-to-end token latency with a real daemon, frame draw
      time, backpressure on a slow pipe; implement the proposed per-turn patch
      protocol (schema 2) if the 1 MiB-per-token resend matters in practice (see the
      measurements section below). `_project_turn` still builds a legacy `lines`
      text that is discarded; remove it and its cache accounting.
- [ ] Rerun the Textual timing-sensitive test group and the full suite after every
      shared-code change (`timeline.py`, `tui_panels.py`, `tui_context_header.py`,
      `tui_widgets.py` were refactored to share helpers).

### P3: distribution and replacement gates

- [ ] No pure-Python fallback wheel: platforms outside the native matrix (Windows,
      others) need Rust to build from source. Decide: keep Textual as fallback
      (current `auto`), publish a binary-less wheel, or mark the Rust bin optional.
- [ ] CI: add a check that native launches with Textual uninstalled (current
      `CIBW_TEST_COMMAND` only runs `--version`); pin Rust >= 1.88 on macOS runners;
      path-gate the four-runner wheel matrix so it is not run on every PR; verify
      cp314 and musllinux jobs, the `macos-15-intel` label, rustup in containers.
- [ ] Missing-toolchain error should mention the minimum Rust version.
- [ ] Installer (`install.sh`, `install.ps1`) and `nexus update` behaviour with a
      native binary; hosted wheel matrix results.
- [ ] Only then: remove Textual/textual-diff-view from runtime dependencies, retire
      the legacy launch path, update every user-facing doc, keep the web app and
      shared contracts in sync, and commit with a Conventional Commit subject.

### P4: code health

- [ ] `rust/tui/src/main.rs` (~1,000 lines) mixes input, leader, mouse and actions;
      split into modules (`input.rs`, `mouse.rs`, `actions.rs`) per AGENTS rule 3.
      `render.rs` is also growing (top bar, sidebars, composer, dialogs): move
      chrome into `chrome.rs` / `dialogs.rs`.
- [ ] Replace string matching on snapshot lines in the coalescing code with a typed
      `one_shot` flag in the snapshot.
- [ ] Hit-testing duplicates layout arithmetic; derive both from one function.
- [ ] Keep `docs/ratatui-parity.md`, `docs/module-map.md`, `docs/decisions.md` and
      this plan current with every change; web docs when behaviour is user-visible.

### Known issues and gotchas

- `FORCE_COLOR` in the shell breaks two CLI help tests; use `env -u FORCE_COLOR`.
- The Textual `reference` fixture applies `.reference-demo` CSS that the live shell
  does not; confirm visuals against the live `app.tcss` rules too.
- The `◆ Agent` label did not appear in the comparison fixture (turn has no agent
  metadata); verify it with a real session.
- Tool and Task rows have no animation; running state is shown only as text.
- Preferences keys are shared with Textual; do not rename them.
- A subagent report said native review paging looped until a 1,000-page limit; it
  is fixed, but confirm with a very large worktree diff.
- Worktree is dirty with other in-progress work; do not revert unrelated files.

## Current snapshot (2026-10-02)

The native prototype has grown into a broad, working Ratatui client, with the
host contract and Python reducer still providing canonical state. It is not yet
ready to replace Textual: interaction coverage, visual matching, a final full
regression, and hosted distribution checks remain open (see HANDOFF above for the
current, prioritised list). The worktree is intentionally left uncommitted on
`feat/ratatui-prototype` for continuation.

Most recent checks recorded:

- 54 focused workflow, voice, and documentation tests passed after the latest
  voice settings extraction and model/voice adjustments.
- Native Rust tests (seven) passed; the minimum supported Rust version also
  passed the same tests in Linux ARM64.
- The latest controlling-PTY test passed, including completion selection,
  permission response, settings save, secret-form cancellation, and terminal
  mode restoration.
- The latest Playwright comparison captured Textual and Ratatui at wide and
  narrow sizes, with draft input, and completed successfully. This verifies the
  comparison path and basic rendering; the screenshots still show visual gaps.
- Earlier broad runs passed: 4,850 offline tests (312 skipped, four
  deselected) and 96 separate Textual timing-sensitive tests. Those runs were
  before the latest model/voice corrections, so rerun the aggregate suite before
  treating the branch as regression-checked.
- macOS ARM64 and Linux ARM64 CPython 3.13 wheels built, installed cleanly, and
  launched the native binary. Hosted runner coverage and post-latest-change
  wheel builds remain open.

No commit, merge, publication, or default-renderer switch has been made.

## Objective and current state

Completely replace the Textual terminal surface with Ratatui, keeping the
existing host contract, features, context visibility and interaction design.
Start with a prototype, then reach parity before switching the default.

- Worktree: `/private/tmp/nexus-ratatui-prototype`.
- Branch: `feat/ratatui-prototype`, based on `6e359ff`.
- Changes are currently uncommitted. The original checkout is preserved.
- Launch: `PYTHONPATH=. /Users/santa/repos/nexus/.venv/bin/python -m nexus.ui.ratatui.prototype --workspace "$PWD"`.
- CLI integration: `nexus chat --renderer ratatui`.
- **Textual is still the default and remains a runtime dependency. The replacement is not complete.**
- Full feature parity, visual parity and the entire platform matrix are not verified.

The detailed implementation ledger is [docs/ratatui-parity.md](../docs/ratatui-parity.md).
This plan records current work and next steps; update the ledger alongside it.

## Architecture implemented

Python owns the daemon client, canonical reducer, host commands and workflow
state. Rust owns terminal input/rendering. Versioned JSONL snapshots travel to
Rust stdin; typed actions return on stdout; rendering uses stderr. Input comes
from the controlling terminal rather than the IPC pipe.

The original input-reader failure was fixed with Crossterm's `use-dev-tty` and
`libc` features. Real controlling-PTY checks verify the bridge and restoration.
The native runtime imports no Textual modules. Shared session lifecycle and
project/date grouping were extracted into neutral `ui_support` modules; the old
surface retains compatibility imports.

Packaging uses a setuptools-rust binary with the existing setuptools backend,
Cargo.lock and source-distribution inclusions. No PyO3 boundary is introduced.

## Implemented

- [x] Continuous idle subscription, canonical replay, bounded reconnect and cross-project client routing.
- [x] CLI native renderer option and installed/source executable discovery.
- [x] Markdown, durable event order, compact tool rows and full labelled tool details.
- [x] Split diffs, thinking expansion, turn collapse and child-agent transcript pages.
- [x] Submitted attachment cards opening every content block.
- [x] Clickable system/tools/AGENTS/skills/MCP sections, extension toggles and context locks.
- [x] Grapheme editor, selection, undo/redo, multiline movement, persisted history and paste.
- [x] Wrapped editor viewport keeping the cursor visible on long lines.
- [x] Queue/steer/interrupt, cancellation with returned queue, failed-send draft recovery.
- [x] Keyboard enhancement negotiation, Ctrl shortcuts and Ctrl+X leader.
- [x] Slash/file completion menu with keyboard selection; common command argument completions.
- [x] Durable nested permission/questions, disabled-choice checks, keyboard/mouse choices and free text.
- [x] Searchable session/model/agent/effort menus, model favorites/recents and refresh.
- [x] Session tabs with open/close clicks, docked sidebars and independent sessions scrolling.
- [x] Shared project/local-date session grouping, archive/trash/restore and archive previews.
- [x] Host-backed inspection commands, context copy and desktop clipboard operations.
- [x] Prepared attachments, eight-item guard, previews and clipboard image input.
- [x] Individual attachment removal preserving existing numbered references.
- [x] Settings scopes/categories, editor, autosave, hash conflicts, reset/delete and default-agent setup.
- [x] Provider setup/login/key/code flows and default-model selection.
- [x] Secret form Escape cancels; explicit Ctrl+S saves.
- [x] Local dictation capture, paced partial previews, final-only insertion, cancellation and download consent.
- [x] Worktree review pagination, digest acknowledgement, integration/discard and force-discard confirmation tokens.
- [x] Saved dark/light theme and panel preferences compatible with existing preferences.
- [x] `/verbose` controls full tool previews independently of context preview.
- [x] Reusable native-wheel CI configuration and release collection/gating.
- [x] Repeatable browser terminal comparison with deterministic reference events.

## Verification completed

Evidence applies to the revision and scope of each run. The focused tests and
PTY/browser checks below include the latest model/voice changes; the broad
aggregate suite and packaged wheels do not.

- Offline suite: **4,841 passed, 312 skipped, four timing-sensitive checks excluded**.
  This preceded the latest visual/navigation/completion changes.
- Focused native workflow/projection/launch/integration/docs checks passed after subsequent edits.
- Latest focused workflow/voice/docs run: **54 passed** after voice settings extraction.
- Docs and layering checks: **140 passed** after extracting shared session grouping.
- Rust editor/schema/Markdown/layout tests: **seven passed** on macOS with the current compiler.
- The same seven Rust tests passed in a Linux ARM64 container with **Rust 1.88**, the declared minimum.
- Latest controlling-PTY test passed: submit, selecting the second completion, permission answer,
  settings save, secret-form Escape cancellation, quit and canonical/echo/signal restoration.
- Real daemon/scripted-provider integration proves complete tool output and live/replay projection equality.
- Latest real Playwright terminal comparison passed for both clients at wide/narrow widths, including draft input. Visual parity remains open.
- macOS ARM64 wheel built and installed into an isolated environment; native `--version` worked.
- Linux ARM64 CPython 3.13 wheel built in manylinux, repaired to
  `manylinux_2_28_aarch64`, clean-installed and passed native `--version`.
- Ruff passed for changed native code and tests.

Local minimum-Rust macOS linking hit an incompatibility between the old compiler's
linker and this machine's macOS 27 SDK. The current compiler builds successfully;
minimum-Rust source compatibility was verified in Linux instead. Hosted macOS
runner behavior remains unverified.

Browser commands and artifacts:

```sh
cargo build --locked --manifest-path rust/tui/Cargo.toml
PYTHONPATH=. /Users/santa/repos/nexus/.venv/bin/python tests/playwright_ratatui_check.py
```

Screenshots: `artifacts/ratatui-parity/{textual,ratatui}{,-draft,-narrow}.png`.
The first comparison exposed incorrect transcript ordering, excessive tool row
height, missing composer framing and different details presentation; these were
adjusted. **Screenshots do not yet establish visual parity.**

## Remaining work, in order

### 1. Finish and audit interactions

- [x] Audit every Textual command, Ctrl shortcut and Ctrl+X sequence against native behavior (2026-10-02; see the continuation below).
- [ ] Finish automatic/fuzzy completion behavior and additional argument providers.
- [ ] Audit model picker grouping/sort/fuzzy ranking and atomic model-plus-effort selection.
- [ ] Finish focus navigation and check narrow sidebar overlay behavior visually. Native shortcuts now open transient inspection at 110–169 columns when both panels cannot fit.
- [ ] Verify active session title/status and cross-project archive actions. Closing the last tab now opens the new-session chooser while retaining the current session until selection.
- [x] Add paged live logs, problem/routine folding, bounded retention, separate cursors and drawer scrolling.
- [ ] Make context header/preview grouping and collapse match the reference.
- [ ] Verify all prompt choices remain reachable in small terminals and long/multi-question journeys.
- [x] Audit attachment references, previews and every submitted content type (host-backed journeys; chip rendering not verified).
- [x] Verify settings nested navigation, conflicts, validation errors and reset/delete using the real host.
- [x] Verify provider setup/authentication journeys with deterministic host fixtures (no real OAuth).
- [x] Exercise worktree review/acknowledgement/integration/discard against actual temporary Git worktrees.
- [ ] Verify voice progress/configuration and cancellation races; real audio hardware is not yet checked.

### 2. Visual and performance parity

- [ ] Compare representative empty/transcript/tool/diff/permission/settings/provider/voice/worktree screens.
- [ ] Match turn cards, spacing, summary alignment, context preview and sidebar presentation.
- [ ] Check both themes and narrow/medium/wide sizes with real terminal screenshots.
- [ ] Benchmark streaming and large histories; measure Python projection/IPC as well as Rust drawing.
- [ ] Bound every snapshot/action/render cache and announce any clipping; do not silently omit context.
- [ ] Avoid resending or reparsing unchanged history during streamed tokens.

### 3. Distribution and final regression

- [ ] Run the hosted wheel matrix for Linux/macOS x86-64/ARM64, Python 3.13/3.14 and Linux libc variants.
- [ ] Verify repaired wheels include executable/assets and clean installs launch without Textual.
- [ ] Verify installer/update behavior and source builds with clear missing-toolchain errors.
- [ ] Validate CI/release workflow configuration on hosted runners; no publication is requested.
- [ ] Rebuild final macOS/Linux wheels after the latest changes.
- [ ] Rerun the full offline suite and the excluded relevant Textual/shared-controller regressions.
- [ ] Run final Rust, PTY, Playwright, Ruff, docs and layering checks.

### 4. Complete the replacement

- [ ] Make Ratatui the default for `nexus chat` once the gates above pass.
- [ ] Remove Textual/textual-diff-view from runtime dependencies; retain reference-only dependencies in dev if needed.
- [ ] Remove or explicitly retire legacy production launch paths and update all user-facing docs.
- [ ] Preserve web behavior and shared command/context contracts.
- [ ] Update this plan and the parity ledger with final evidence and honest remaining platform limitations.
- [ ] Commit the reviewed work on the task branch using Conventional Commits.

## Tracking rules

Keep implemented behavior separate from verified behavior. Do not mark parity or
completion based solely on unit tests or wheel `--version`. Preserve unrelated
work and the separate worktree. Update matching `docs/` pages with code changes.
Do not publish, merge or bump versions as part of this migration unless requested.


### Continuation after the tracking snapshot

- Extracted the shared shortcut/leader reference into `ui_support/shortcuts.py`;
  native `/hotkeys` now shows the full same table.
- Added `ui/ratatui/logs.py`: paged polling, separate cursors, bounded/deduplicated
  rows, stale-session rejection, clipping notices and problem/routine folding.
- Logs drawer supports Ctrl+A folding, PageUp/PageDown/wheel scrolling and
  Escape dismissal. Paging and stale-session tests pass.
- Corrected medium-width details shortcuts and narrow leader shortcuts.
- Last-tab close starts a new-session chooser rather than leaving empty tabs.
- Focused workflow/docs checks and seven Rust tests passed at this point. Later
  PTY and Playwright rechecks also passed; see the current snapshot above.

### Projection performance follow-up

A synthetic 1,000-turn history initially took about 160 ms to project before
encoding. Reusing sanitized projections of unchanged reducer turn objects
reduces a repeated projection to about 0.6 ms (cold projection remains about
164 ms). Cache retention is bounded to 4,096 turns and 8 MiB of serialized
presentation data, resets on session switch, and invalidates turn replacements
and expansion/verbosity state. A regression test checks reuse, replacement and
control-safe cached text. This is a local microbenchmark, not a complete
streaming/terminal performance claim; wire encoding and Rust cache costs remain
to be measured. The broad regression run completed; details are recorded below.


Rust debug-build cache microbenchmark: 1,000 Markdown blocks / 24,001 rows took
about 188 ms cold and 8.3 ms with one changed block. Run it with
`cargo test --manifest-path rust/tui/Cargo.toml -- --ignored --nocapture`.
Release build and end-to-end token latency remain unmeasured. Context inspection
now refreshes as the durable cursor advances, with a session-generation guard.


### Regression and voice/model audit follow-up

- Offline suite completed: **4,850 passed, 312 skipped, four deselected**;
  timing-sensitive Textual group separately completed: **96 passed**. Both
  runs preceded the latest model-effort/voice corrections. The later focused
  workflow/voice/docs run passed 54 tests; rerun the full aggregate suite to
  close this gap.
- Native model selection now offers explicit effort selection and commits it
  through the shared guarded model-plus-effort controller path.
- Voice on/off now persists host-backed configuration, respects enabled state,
  readiness and bounded capture duration, and supports configured auto-send.
- Final voice text receives composer word spacing; stale request/session/panel
  results cannot insert or auto-send. Partial text is never inserted.
- Extracted neutral voice settings with hash-conflict retry, retaining Textual
  compatibility imports; existing Textual voice tests pass.

### Aggregate regression and command audit (2026-10-02)

- Full offline suite: **4,947 passed, 312 skipped, four deselected, 2 failed**.
  Both failures are `test_package_entrypoint_help_supports_directory_and_module_execution`,
  caused by `FORCE_COLOR=3` in the dev shell colouring argparse help; they pass
  with the variable unset. Not a regression. Rust (seven tests) and Ruff passed.
  The separate Textual timing-sensitive group was not rerun in this pass.
- Every `SPECS` command has a native branch. Fixed edge cases that differed from
  Textual: `/sessions <prefix>` prefix match and "No session matching" notice,
  `/fork` ignoring a non-numeric argument, `/diff` multi-ref rejection and
  "No changes", `/theme` invalid-argument usage error, `/tools` and `/tasks`
  empty notices (tasks prefer `task` over `description`), `/copy` without a
  preview. Tests in `tests/test_ratatui_actions.py`.
- Added a test that every row of `SHORTCUTS` (Ctrl letters) and every
  `LEADER_SHORTCUTS` letter is bound in `rust/tui/src/main.rs`. Ctrl+P opens the
  native `/help` command menu rather than a palette widget.

### Visual matching, journeys and default renderer (2026-10-02, later)

- Native rendering rebuilt to follow Textual: prompt cards, thought/agent/tool/
  footer rows, Task cards, word-boundary wrapping, context header chips, details
  sidebar, top bar, composer box, inline permission panel, dialog-styled pickers,
  light and dark palettes. Screenshots: `artifacts/ratatui-parity/`
  (reference, permission, picker, panel, light, narrow). Visual parity is close
  but **not verified** for voice, worktree, settings and provider screens, nor a
  populated context header or file-row expansion.
- Shared pure helpers now back both shells (`ui_support/details.py`,
  `context_header.py`, `completion.py`, `model_choice.py`, timeline row/footer).
- Subagent journeys against the real host (settings, providers, worktrees with
  real git, attachments): see `tests/test_ratatui_journeys.py`. They fixed a host
  bug that turned the worktree review digest into `***` and a listing failure
  after discard (`nexus/host/facade.py`, `host_support/worktree_projection.py`,
  `agents/worktrees.py`); review those host edits separately.
- Streaming: compact snapshots, identical ones skipped, queued backlog
  coalesced. Per-turn patches (schema 2) remain a proposal.
- `nexus chat --renderer auto` (default) picks native when its binary exists,
  else Textual. Textual stays a runtime dependency because there is no
  binary-less wheel for platforms outside the matrix (docs/decisions.md).

Still open before Textual can be removed: hosted wheel matrix and installer/
update checks, a no-Textual CI import check, real-hardware voice, Textual's agent
form fields in native settings, and a final screenshot review of the unchecked
screens above.

## Next actions when work resumes

1. Rerun the full offline suite and the separate Textual timing-sensitive
   tests after the latest model/voice changes; then run Ruff and docs/layering
   checks if any fixes are needed.
2. Continue parity work using [docs/ratatui-parity.md](../docs/ratatui-parity.md):
   audit commands, shortcuts, completion, settings/provider flows, prompts,
   attachments, worktrees and voice against Textual, fixing gaps with focused
   tests.
3. Compare real terminal screens for representative transcript, tool, diff,
   permission, settings, provider, voice and worktree states. Address spacing,
   turn cards and context preview, and check both themes at medium as well as
   narrow and wide widths.
4. Measure end-to-end stream latency and large histories; review bounded
   projection, IPC and render caches and clipping behavior.
5. Rebuild the wheels after the final code changes and validate the hosted
   Linux/macOS architecture and Python matrix plus installer/update behavior.
6. Only after the parity and regression gates pass, make Ratatui the default,
   remove Textual from runtime dependencies, update the user-facing docs and
   commit the reviewed change. No merge or publication is part of the current
   request.


## Performance and distribution measurements (2026-10-02)

Verification only; no Rust or `nexus/ui/ratatui/*.py` source was changed. Scratch
outputs live under `/tmp/ratatui-perf*` (benchmark script `/tmp/ratatui-perf/bench.py`,
Cargo target `/tmp/ratatui-perf-target`). Machine: macOS arm64, rustc 1.97.1,
CPython 3.13. All numbers are single-machine microbenchmarks, not terminal latency.

### Projection, encoding and parse cost per streamed token

Synthetic history: N completed turns (user message, 15-line Markdown reply, one
`Read` tool call with 40 lines of output; tool detail only in the transcript page, not
in blocks) plus one live turn replaced on each of 200 appended tokens (as the reducer
replaces turn objects). Measured with `project(...)` and the same
`json.dumps(..., ensure_ascii=False)` the bridge uses, with the turn cache enabled.
The reducer `ingest` cost is not included.

| Turns | Cold project | Snapshot size | Per token: project (median / p95) | Per token: JSON encode (median / p95) | Rust `serde_json` parse into `Snapshot` (median, 50 runs) |
| --- | --- | --- | --- | --- | --- |
| 100 | 34.6 ms | 108 KiB | 0.23 / 0.31 ms | 0.32 / 0.35 ms | 0.24 ms |
| 1,000 | 331 ms | 1,076 KiB | 0.82 / 0.98 ms | 3.33 / 3.49 ms | 1.18 ms (max 2.6 ms) |

Rust cache (`cargo test --release -- --ignored --nocapture`, 1,000 Markdown blocks,
20,001 rows): cold 53.9 ms, one changed block 3.6 ms (the debug build recorded earlier
was 188 ms / 8.3 ms). The release binary builds in about 10 s (incremental) and the
wheel build, including a fresh Cargo target, took about 12 s.

Rust parse time came from a throwaway crate in `/tmp/ratatui-perf/parse` that includes
`rust/tui/src/bridge.rs` unmodified, so it uses the real `Snapshot` type. The
real binary was not driven end to end (it needs a controlling TTY).

Finding: **unchanged history is resent and reparsed on every snapshot.** Only 860 bytes
of a 1,076 KiB snapshot (0.08%) belong to the changing turn. Python's turn cache removes
the re-projection cost, but the whole `blocks` array is JSON-encoded, written to the pipe,
and deserialized into fresh owned `Content` values in Rust on every event, and
`main.rs` parses every queued line (no coalescing of backlog: the `try_recv` loop calls
`serde_json::from_str` for each line, then `revision` filtering only skips stale ones).
Rough per-token cost for 1,000 turns is 0.8 + 3.3 + 1.2 + 3.6 (Rust cache diff) = about
9 ms of CPU across both processes, and about 1 MiB written per token (about 50 MiB/s at
50 tokens/s). That is tolerable on this machine for this history size, but it scales
linearly with history bytes, so a 10,000-turn or tool-output-heavy session (with verbose
mode, tool `detail` is embedded in blocks) will be an order of magnitude worse. Poll ticks
(every 1-3 s, 0.2 s while dictating) also send full snapshots even when nothing changed.
Not verified: stream behaviour with a real daemon producing tokens at provider rates,
slow-pipe back-pressure, and terminal draw time per frame.

Also observed: `_project_turn` still builds the legacy `lines` text (including
`sections_to_text` for each tool a second time) that `run()` then discards
(`snapshot["lines"] = []`); this inflates only the cold projection and the cache byte
accounting (the cache stores and sizes `lines` too).

Proposed minimal design (not implemented): make the snapshot an incremental protocol with
a keyed block list. (1) Python keeps `sent: dict[turn_id, digest]` (the existing cache entry
identity, plus flags) per client. (2) Each snapshot carries the small fields and an ordered
`order: [turn_id,...]`, plus `turns: {id: blocks}` only for turns that are new or whose cache
entry changed since the last send. A full snapshot is still sent on connect, session
generation change, theme or verbosity/expansion change, or `resync` request from Rust
(sequence gap). (3) Rust keeps a `HashMap<turn_id, Vec<Content>>`, applies the patch, and
only re-wraps changed turns in its existing `Cache::update_content`. (4) Coalesce: while
more lines are queued, apply patches without drawing and draw once. (5) Skip sending when
the snapshot is byte-identical to the last (poll ticks). Keep schema `1` for full snapshots
and add schema `2` for patches so mismatched binaries fail fast. Cheaper first step if
that is too large: only do (4) and (5) plus dropping the unused `lines` field.

### Packaging checks

- YAML: all five workflow files parse with PyYAML (`ci`, `install`, `native-wheels`,
  `pr-title`, `release`). `actionlint` and `zizmor` are not installed, so semantic linting
  of the reusable-workflow wiring is **not verified**; I read the expressions by hand and
  found no error. `release.yml` correctly skips `publish` unless every native-wheels job
  succeeded, and its `ref` falls back to `inputs.tag` on manual dispatch.
- Local wheel (`uv build --wheel`, Cargo target in `/tmp`): `nexus_harness-0.2.16-cp313-cp313-macosx_11_0_arm64.whl`,
  2.1 MB, contains `*.data/scripts/nexus-ratatui` (Mach-O arm64, mode 755, 1.76 MB), the 10
  `nexus/ui/ratatui` files and no Rust sources or Cargo files. The platform tag is
  per-Python (`cp313-cp313`), so cp313 and cp314 wheels from cibuildwheel have distinct names.
- Fresh venv install: `nexus-ratatui --version` prints `Nexus Ratatui · bridge 1`. After
  uninstalling `textual` and `textual-diff-view`, `--version`, `binary_path()` and
  `import nexus.ui.ratatui.prototype` still work and `textual` is not in `sys.modules`.
  `rich` is imported, but only transitively by `httpx._main`; this is third-party, not
  Textual. `nexus chat --renderer ratatui` skips the Textual availability check by design.
- sdist (`uv build --sdist`) contains `MANIFEST.in`, `rust/tui/Cargo.toml`, `Cargo.lock`
  and the six `.rs` files and no `target/`; a wheel built from that sdist alone contains
  the binary. A local build leaves ignored `build/` and `*.egg-info` in the tree (removed).
- Missing toolchain (`PATH=/usr/bin:/bin`, no cargo): the build fails with setuptools-rust's
  "error: can't find Rust compiler ... install rustup (https://rustup.rs) ... ensure it is
  on the PATH" and a note that a prebuilt wheel avoids the need. The message is clear but
  does not state the minimum Rust version (1.88); an older compiler would be reported by
  Cargo's own `rust-version` error (not exercised). `install.sh` surfacing of this error
  was not exercised.

### Problems and risks found

1. **No pure-Python fallback wheel.** The release publishes only platform wheels plus the
   sdist. Windows (and any platform outside the four runners, musl variants not yet run)
   now builds from source and needs Rust, where it previously installed a pure wheel;
   `install.ps1` and `nexus update` would hit this. Decide between publishing a
   `py3-none-any` wheel without the binary (Textual stays default) or marking the Rust bin
   optional in setuptools-rust (not verified that `RustBin` supports `optional`).
2. **CI does not prove "no Textual".** `CIBW_TEST_COMMAND` runs only `nexus-ratatui --version`
   in an environment with all runtime dependencies installed. Add a Python import check
   that asserts `textual` stays out of `sys.modules` once Textual is no longer required.
3. **Unverified on hosted runners:** `native-wheels.yml` (rustup install inside
   manylinux and musllinux containers, `curl` availability in the musllinux image,
   macOS runners' Rust version >= 1.88, cp314 builds, `macos-15-intel` label). Only Linux
   rustup pins 1.88.0; macOS wheels use whatever Rust the runner has.
4. **CI cost.** `ci.yml` now calls the full four-runner, two-Python cibuildwheel matrix on
   every pull request (60-minute timeout each) and `ci-ok` depends on it. Consider running
   it only on `rust/**`, `pyproject.toml`, `MANIFEST.in` or workflow changes, and on release.
5. Per-token full-history resend, described above.
6. Version skew: the packaged branch reports 0.2.16 in `pyproject.toml`, while `main`
   is at 0.2.17; rebase before building release candidates.

Open from the plan's section 2/3 after this pass: hosted wheel matrix, installer/update
behaviour, terminal-rendered frame timings, and the snapshot patching proposal.
