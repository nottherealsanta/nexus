# Plan: Grok Build look and interaction for the Nexus terminal client

Status: in progress. Foundation, projection/tool signals, native Sessions dashboard, Mermaid fallback, appearance controls, input handling and prompt anchoring are implemented. Targeted Rust/Python/native PTY checks pass (see ledger). The combined run, release rebuild and performance measurements are recorded in the ledger. All 24 geometry-corrected screenshot-matrix PNGs were pixel-reviewed (see the final ledger); the web check failed twice and is unresolved, and the animate-only idle scenario is not measured, so this is **not an all-green acceptance**. Decisions D1–D4 were settled with the user on 2026-10-14 (§2).

Scope: the native Ratatui client (`rust/tui`, `rust/widgets`) and the Python
projection that feeds it (`nexus/ui/ratatui/`, `nexus/ui_support/`). The web
client is to be deprecated and gets no new work from this plan (AGENTS.md). The
desktop GPUI client is out of scope.

Reference source: `references/grok-build/` (`github.com/xai-org/grok-build`, revision `559751fdcec02d413e4c57c8832ab275e4f44980`; this is the actual revision used by the isolated Mermaid port, superseding the earlier planned `2bdd1d6`). Paths below
that start with `grok:` are relative to `references/grok-build/crates/codegen/`.
The reference is Apache-2.0. The one file this plan copies (`mermaid.rs`) keeps
its copyright header and gets an entry in a `NOTICE` file (see §10).

## 0. How this plan differs from the brief

### Implementation progress (current handoff)

**Final acceptance review (latest; supersedes everything below where they differ):**

- **Pixel review: 24 of 24 PNGs opened and viewed** (`artifacts/ratatui-parity/grok-{dashboard,mermaid,groups,context}-{dark,light}-{50,90,140}-ascii.png`, regenerated after the geometry fix). Checked fit, contrast, right edge and footer. Findings: every capture fills its grid with no clipped or overflowing footer; the footer shows `Build · model · usage` at all widths (provider and `not exposed` appear from 90 columns); the dashboard stacks the peek below at 50/90 and goes side-by-side at 140; the light dashboard background and footer, fixed in the earlier review, are correct; text contrast is legible in dark and light; the Mermaid flowchart draws as box art and the unsupported `pie` falls back to a labelled code block; groups show red `x` failures and `· 2 failed`; at 50 columns the group header truncates with `…` (`Read 3 …`) while keeping `2 failed`; the dashboard time column (`1m`) sits flush against the right edge at 50 columns without clipping. The thin last-row strip is now geometry-corrected. Minor and not blocking: the scrollbar track is drawn on the right edge in the transcript scenes and the dashboard scenes, and the 90-column bottom strip is slightly narrower than the 140 one, as designed.
- **What the screenshots are:** the Rust binary run in a PTY and captured through xterm at identical dimensions, with a `.geometry.json` sidecar validated for all 24 (grid, PTY dimensions and screen fit all correct). They are **not genuine OS screenshots**.
- **Verification now:** Rust `rust/tui` 396 passed, 2 ignored, plus 16 integration tests; `rust/widgets` all passed; `design-mockups` 20 passed; `ruff check nexus tests` passed; targeted fake-runtime and pending tests pass. The full offline suite (6264 passed, 335 skipped, 6 deselected) was run before the last pure-Rust and dashboard changes; no Python production code has changed since. Release rebuilt, optimized, 8.43 s, after the final production changes.
- **Web check: FAILED, unresolved.** `tests/playwright_web_check.py` was attempted twice and timed out both times in the archive sidebar check (line 762). A static audit points to an unrelated latent web `archivedCount` race, which is **not proven**. Not fixed here (web is out of scope); the failure is recorded as open and **no all-green claim is made**.
- **Performance:** the three paired median results below stand (fixture limited to old warm static blocks). Max cold startup is about 2 s outside steady state, so no absolute max < 50 ms claim.
- **Remaining acceptance work:** resolve or disprove the web check failure, and finish the opt-in animate-only idle probe. `NEXUS_TUI_PERF_ACTIVITY=live|static|ascii` was added to `tests/ratatui_performance_check.py`, but all three parent runs timed out during shutdown (`process.wait(timeout=5)` after Ctrl+Q); no activity CPU numbers were produced. Its `finally` block killed and reaped each probe. The default benchmark remains the source of the valid paired measurements above; the experimental activity mode must be repaired before it can be used as acceptance evidence.
- **File manifest additions:** `tests/browser_serve.py` (loopback xterm server and geometry helper) and `rust/tui/src/render/sessions.rs` (the dashboard renderer) belong to this plan's changed files.

Previous acceptance-run results (parent; superseded by the review above wherever they differ):

- **Full offline suite:** 6264 passed, 335 skipped, 6 deselected in 489.58 s. The host facade fake-runtime test is fixed (a proper `_load_config` stub plus the new `SessionDashboard` fake conventions). A real `UnboundLocalError` escape in `setup_save` (the `escape_controls` branch import) is fixed by removing that import. Old expectations for the comma group / neutral context are updated.
- **Rust `rust/tui`:** 394 passed, 2 ignored, plus 14 integration tests (after a new narrow-header suffix regression test). The narrow group failure suffix now shows the full `2 failed` at 50 columns (parent inspected the text).
- **Release rebuild:** successful, 8.42 s.
- **Screenshot matrix:** 24 reproducible captures = 4 scenes (dashboard, groups, mermaid, context) × 50/90/140 columns × dark/light, with `NEXUS_ASCII=1` and a paired terminal-text dump per capture. Captures were verified non-blank only. Actual pixel review is delegated and **not done**; there is **no visual acceptance claim**.
- **Harness environment:** `NEXUS_TUI_BINARY` and `NEXUS_TUI_TRACE_FILE` are honoured; the binary and harness SHAs are recorded.
- **Before/after performance** (`tests/ratatui_performance_check.py`): baseline is a clean isolated `HEAD` archive (no dirty-tree revert), stored under `artifacts/grok-acceptance/head`; 3 alternating paired runs with the same harness. The current tree includes unrelated dirty changes, so this is **not isolated attribution**. Median p95, baseline → current: draw 0.678 → 0.722 ms; key→frame 17.392 → 17.544 ms; wheel→frame 17.507 → 17.423 ms; snapshot→frame 17.931 → 17.502 ms. Idle CPU 0.00% in all six 1 s samples. Cold-startup max was about 2 s in every run, so **no absolute max < 50 ms claim** is made. Streaming CPU medians 6.89% (baseline) vs 6.56% (current) are noisy. The recorded fixture uses old warm static blocks, not the new animate-only idle scenario, so this is **no proof of full idle-animation acceptance**. Data: `artifacts/grok-acceptance/head-[123].txt` and `current-[123].txt`.
- **Remaining:** actual visual review of the screenshots; timings for a new animated scenario as appropriate. The web check was not run.

Earlier parent verification (superseded by the final results above; kept as history):

- `cargo test --manifest-path rust/tui/Cargo.toml -q` (parent, final run of this continuation): **393 passed, 2 ignored**, plus **14 integration tests passed**. A new PTY click test passes.
- Final combined run (parent): targeted Python projection/dashboard/completion/docs/layering tests plus seven native PTY suites, **394 passed in 58.29 s**; cargo **393 passed, 2 ignored**, plus **14 integration tests passed**.
- Earlier continuation: Python dashboard and completion tests, **72 passed**.
- Targeted Python projection/dashboard/signals/docs/layering plus seven native PTY suites: **402 passed**. Native PTY subset separately: **22 passed**.
- `cargo test --manifest-path rust/widgets/Cargo.toml -q`: passed (all test binaries).
- `tests/test_host_facade.py`: **91 passed, 1 failed**; remaining failure is the unrelated fake runtime missing `_load_config` in `test_facade_handle_dispatches_every_verb`.
- Fixed and regression-tested Mermaid unsupported/streaming fallback: the Markdown dependency silently discards Mermaid without its feature, so fallback explicitly uses its ordinary code renderer and restores the language label.
- Fixed local disclosure losing signal, inspector, elapsed/failure/live fields; the final Rust run includes its regression test.
- Native Appearance checkbox popup and prompt-anchor adapter are now wired; multiline prompt/frozen-offset regressions pass.
- Implemented this continuation:
  - **Dashboard lifecycle.** When a replacement overlay closes, the dashboard invalidates its in-flight work. Async tests cover this. (This replaces the earlier "polling continues beneath overlays" gap.)
  - **Double-click is 300 ms** (D1, §5.1), on the same semantic block and the same operation. A double-click on a tool toggles its disclosure locally. A single click only selects the row. `i` opens the full inspector. (This replaces the earlier 400 ms behaviour.)
  - **Explicit inspector.** It propagates to output rows. Local disclosure matches on semantic kind and id and ignores presentation metadata; a regression test covers this.
  - **Settings hover** is wired from the actual hit ranges, with disabled-row and modal guards tested. (This replaces the earlier "hover incomplete" gap.)
- Screenshots: captures ran with `NEXUS_RATATUI_CAPTURE=ratatui-local,ratatui-settings,ratatui-tool,ratatui-markdown`. Only the generation of the screenshots is reported. **No visual acceptance** has been done.
- Performance (`tests/ratatui_performance_check.py`, measured): draw p95 1.517 ms, max 30.936 ms; key→frame p95 17.243 ms; wheel→frame p95 18.057 ms; snapshot→frame p95 19.450 ms, max 2072.817 ms (initial cold setup). Idle CPU 0.00% over 1 s; streaming CPU 14.44% over 3.05 s (150 patches, 300 wheel events, 50 keys). There is no before baseline, so "idle CPU unchanged" is **not** claimed, and no max-latency pass is claimed. These are measurements, not acceptance.
- Release build: rebuilt successfully (optimized, 8.91 s); the final executable is fresh.
- Screenshots: four captures (tool, settings, markdown, local) were generated and have **not** been visually reviewed.
- (Superseded: the full offline suite has since been run, see above.) The web check was not run. No commits were made.
- Remaining: visual review and acceptance of the screenshots, a before/after performance baseline, and the full suite. Screenshots must validate the intended visual result before the plan is complete.

Historical intermediate verification (kept for traceability; failures below are superseded by the final results above):

- [x] Foundation widget roles exist as methods (not constants); do not describe them as a completed role-constant design.
- [x] Projection/tool verb signals exist (`nexus/ui/ratatui/verb_groups.py`, `nexus/ui_support/tool_signal.py`).
- [x] Live Sessions dashboard shell is wired (`rust/tui/src/render/sessions.rs`, `picker.rs`, `nexus/ui/ratatui/bridge.py`, `nexus/host_support/session_preview.py`). Behaviour is not verified in a real terminal.
- [ ] Native keys and animation are wired in code (`animate_activity` in `config.rs`/`main.rs`, `transcript/look.rs`) but are **unverified**: no PTY or screenshot run.
- [ ] Rust `rust/tui` suite: 374 passed, 2 failed, 2 ignored. The Mermaid dependency swallows the fallback (a diagram that should fall back to a code block is lost); fix is ongoing.
- [ ] Python suite: 372 passed, 6 failed. Failures: signal clipping (2), module map (2, addressed by the `docs/module-map.md` rows added since), layering (2). Fixes ongoing; re-run needed to confirm.
- [ ] Host tests: 485 passed, 1 failed. The failure is the pre-existing, unrelated fake `ModelsList` mismatch.
- [x] Mermaid is an isolated Rust module, registered in `main.rs` and called from `markdown.rs`; integration is incomplete because of the fallback defect above.
- [ ] **Not done:** PTY runs, screenshot review, `tests/ratatui_performance_check.py` and the web check have not been run. No claim of a finished implementation is made, and no commits.

Historical pending list at the earlier handoff (superseded where the final parent ledger above records implementation; not a current absence claim):

- [ ] Full follow behaviour (§8A): `rust/tui/src/session.rs` has prompt anchoring and tail space, but the `Follow` enum and `scroll.rs` module of §8A.2 do not exist as specified, and the end-to-end follow/pin behaviour is unverified.
- [x] Double-click (§5.1): implemented with a 300 ms window; see the current ledger above. (Historical text said no `clicks.rs` or `ClickTracker` existed.)
- [ ] Real settings checkbox for **Animate activity** (§3.2): the toggle is a `tui.json` key and a key binding (`a` in `main.rs`); no checkbox control in the Settings page.
- [x] Hover states for settings controls and rows: now wired from hit ranges (`rust/tui/src/settings_page/input.rs`, `mod.rs`); see the current ledger above. (Historical text said there was no hover handling.)

Files changed this continuation, in addition to the earlier manifest: `rust/tui/src/settings_page/input.rs`, `rust/tui/src/settings_page/mod.rs`, `tests/test_ratatui_completion.py`.

These checkboxes track only the evidence listed above; they are not phase completion.

The brief was checked against both codebases before this plan was written. Where they
disagree, this plan follows the code or the user's later answers. The differences that
change the work:

| Brief says | What the code shows | Effect on this plan |
| --- | --- | --- |
| Adopt Grok's 10-frame Braille spinner at 80 ms | Nexus **already** uses that exact 10-frame set at 80 ms (`rust/widgets/src/glyphs.rs`, `rust/widgets/src/anim.rs::frame`, `rust/tui/src/render.rs::SPINNER`). Grok uses 8 frames (`⠋⠙⠹⠸⠼⠴⠦⠧`) at ~133 ms (`grok:xai-grok-pager-render/src/glyphs.rs:148`, `SPINNER_DIVISOR = 4` at 30 fps). | Keep Nexus's spinner. Port only the wave/pulse **brightness** animation, which Nexus lacks. |
| "Wave-shimmer text animation" across labels like `Thinking...` | Grok animates **rail and bullet brightness** with a `sin²` wave per row (`wave_brightness`, `pulse_brightness` in `grok:xai-grok-pager-render/src/theme/tokyonight.rs:345`; `WAVE_SPEED = 0.15` in `grok:xai-grok-pager/src/scrollback/wrappers/entry_renderer.rs:21`). The only per-character shimmer is the welcome logo. | Port the row wave for rails and bullets. A per-column label shimmer is a Nexus addition, built on the same function (§3.2). |
| Update `nexus/view/fold.py` to coalesce tools into verb groups | `fold.py` handles delta deduplication only. Tool grouping already exists in `nexus/ui/ratatui/prototype.py` (`flush_activity`, ~line 404) and is drawn as the `tool_group` block kind in `rust/tui/src/transcript.rs:926`. | Restyle the existing grouping and move it into its own module (§4.1). Which tools group stays as it is today (D2). The reducer is not touched. |
| Tool groups show failures in red and stay expanded | `docs/ratatui-parity.md:551` records the opposite decision: "Tool groups carry no ✓/✗ or · N failed". Grok shows a red `· N failed` suffix (`verb_group.rs`, `into_label`). | The brief is the newer instruction, so this plan **reverses** the documented decision and records why in `docs/decisions.md`. |
| Strip `call_id`, raw parameters, timestamps from tool details | AGENTS.md: "never hide information from the user that the agent can see". `tool_details.py` exists to meet that rule, and its web port must stay in step. | Double-click shows a new **relevant-only view** (D1, §5.2). The full labelled details are not deleted; they stay reachable with the `i` key. `tool_details.py` is not cut down. |
| `/sessions` is a modal in `render/dialogs.rs` | Sessions is a 40-column **sidebar** drawn by `chrome.rs::session_sidebar` (`docs/ratatui-parity.md:448`), with project chips, worktree grouping, filter and an archived menu. | Keep the sidebar. Add a separate full **Sessions dashboard** view (§6). |
| Grok's dashboard is two panes, list on the left and preview on the right | Grok is list-first, with a **peek panel below** the list (`grok:xai-grok-pager/src/views/dashboard/layout.rs`; the peek takes at most 3/8 of the height, the list keeps at least 12 rows). Rows are grouped by state (`NeedsInput`, `Working`, `Idle`, `Inactive`, `Completed`, `Failed`) or by directory. | Side-by-side at ≥ 110 columns (the brief's request), and Grok's stacked peek below that (§6.2). |
| Grok's `mermaid.rs` covers flowchart, sequence, class, state, ER, gantt, pie, mindmap | `grok:xai-grok-markdown/src/mermaid.rs` (5,699 lines) covers **flowchart/graph (with subgraphs), stateDiagram, classDiagram, erDiagram, sequenceDiagram**. Gantt, pie and mindmap exist only in Grok's SVG engine. | Those five types draw as Unicode diagrams when they fit. Everything else shows as an ordinary code block (D4, §7). |
| Graphical Mermaid via SVG/PNG, Kitty/iTerm2 | Grok renders PNGs lazily in a child process and opens them in the OS viewer, never inline (`grok:xai-grok-pager/src/app/mermaid_worker.rs`). | **Dropped** (D4): no images, no new crates. |
| Double-click within 350 ms | Grok uses 300 ms (`MULTI_CLICK_TIMEOUT_MS`, `grok:xai-grok-pager/src/app/agent_view/mod.rs:426`). | 300 ms, same block, same operation (§5.1). |
| Restyle the composer with rounded borders and a title chip | **User instruction (2026-10-14): the composer stays as it is, without the left colour border.** | §8: the only composer change is removing the agent-coloured `┃` rail. |
| Tokyo Night palette | Nexus dark is a neutral grey with a peach accent (`#0B0B0B` background). | **Dropped** (D3): no new themes. Phase 1 adds internal colour roles that map to today's colours, so nothing looks different. |

## 1. Goals and non-goals

Goals
1. Transcript activity reads as Grok Build does: one quiet summary row per run of
   tool calls, a live wave on what is running, and thinking that folds to `Thought for 3.2s`.
2. Double-clicking a tool expands it, showing only the information relevant to that tool.
3. A full-screen **Sessions** dashboard. The name "Sessions" and the `/sessions`
   command stay.
4. ```` ```mermaid ```` fences draw as Unicode diagrams when the diagram fits the
   width, and otherwise show as a normal code block.
5. Every Nexus-only feature keeps working with today's colours (§9).
6. A sent prompt jumps to the top of the transcript, and the transcript scrolls well
   past the last message (§8A).
7. Headers are grey (inactive ones darker). The agent colour appears only on the agent
   name in the composer, subagent names, and the activity bar. The transcript has no
   `◆ <agent>` label (§8B).
8. Context headers start closed, showing only the name and a darker token count (§8C).

Non-goals
- No change to the session log, the reducer's event model, or replay (rule 5 still holds).
- No new web features. If shared Python output changes, the web client must still
  pass its checks (`tests/playwright_web_check.py`).
- No composer redesign (user instruction).
- No new themes (D3).
- No Mermaid images (D4).
- No change to which tools join a group (D2).
- No Grok features outside the brief (ACP, voice modes, `/gboom`, the dock, leader cluster).

## 2. Settled decisions (user, 2026-10-14)

- **D1. Double-click on a tool expands it, showing only relevant information.**
  - A double-click on a tool row (standalone, or a member inside an expanded group)
    toggles that tool's **relevant-only view** (§5.2).
  - A single click on a tool row no longer toggles it. Otherwise a double-click would
    toggle twice and end where it started. A single click selects the row instead: the
    raised background and the same focus that Tab gives.
  - Group headers, thoughts, prompt cards, chips, links and copy buttons keep today's
    single-click behaviour. Only tool rows change.
  - Enter and Space on a focused tool do what a double-click does.
  - The expanded view carries no metadata rows (no call ID, timestamps, status words,
    bundle or raw JSON). The full labelled details are not removed from Nexus: `i` on a
    focused tool opens them, as AGENTS.md requires. The key is listed in the shortcuts help,
    not inside the view.
- **D2. Grouping membership stays as it is now.** Every tool and thought joins the
  activity run, except `task`/`subagent` rows and the standalone `change` blocks
  (`STANDALONE_TOOLS` = edit tools and `question`). Bash keeps folding into groups.
  Only the look changes (§4).
- **D3. No themes yet.** No Tokyo Night and no palette change. Phase 1 adds internal
  colour roles only, each mapped to an existing colour.
- **D4. No Mermaid images.** Draw the diagram with Unicode in the terminal if it fits.
  Otherwise, including unsupported diagram types, parse failures and streaming fences,
  show it exactly like any other code block.

## 3. Phase 1: colour roles, glyphs and animation primitives (`rust/widgets`)

### 3.1 Colour roles (`rust/widgets/src/theme.rs`, `rust/tui/src/render.rs::Palette`)
Add roles that the new drawing code names, each mapped to an **existing** colour, so the
default look is pixel-identical (D3):

| Role | Maps to (dark and light) | Used for |
| --- | --- | --- |
| `gray_dim` | `border_strong` | tree connectors, meta punctuation |
| `gray` | `quiet` | member rows, folded thoughts |
| `gray_bright` | `muted` | verb-group header text |
| `rail` | `border` | thinking and group guide bars |
| `running` | `text` (wave runs `muted` → `text`) | live headers, spinner, wave peak. **Never the agent colour** (§8B). |
| `header` | `muted` | active context header titles and rails (§8B) |
| `header_inactive` | `quiet` | empty/inactive context headers (§8B) |
| `thinking` | `quiet` + dim + italic | reasoning body |

- Dim and italic are left off under `Mode::Mono` and on legacy consoles (Grok's `body_emphasis_patch`).
- The two palettes must stay in step. Add a unit test that every new role exists in both
  for dark, light and mono.

### 3.2 Animation (`rust/widgets/src/anim.rs`)
Add pure functions that take a tick, so tests can use a fake clock:
```rust
/// sin²(tick·speed + row/rows·2π) in 0..=1 (Grok's wave_brightness).
pub fn wave(tick: u64, row: u16, rows: u16, speed: f32) -> f32;
/// sin²(tick·speed) in 0..=1 (Grok's pulse_brightness), for "waiting on you" cues.
pub fn pulse(tick: u64, speed: f32) -> f32;
/// Per-column wave across a label: each grapheme's colour is mix(base, peak, wave(tick, col, period, speed)).
pub fn shimmer_spans(text: &str, tick: u64, base: Color, peak: Color) -> Vec<Span<'static>>;
```
- The tick comes from the existing 80 ms frame clock (`anim::frame`), so no new timer is needed.
- Constants: `WAVE_SPEED = 0.15`, `WAVE_ROWS = 32` (Grok's `animation.wave_rows`),
  `PULSE_SPEED = 0.08` (Grok's `USER_WAITING_PULSE_SPEED`).
- `Mode::Mono`, `NEXUS_ASCII=1` and a new Settings → Appearance toggle **Animate activity**
  (`animate_activity`, default on, saved in `tui.json`) turn colour animation off. With it
  off, the spinner glyph still advances, so the "running" signal remains.
- Redraw budget: `render::animating` already drives redraws while anything runs. The wave
  must add none when nothing is running. Check with `tests/ratatui_performance_check.py`.

### 3.3 Glyphs (`rust/widgets/src/glyphs.rs`)
Add fields. Each one has an ASCII fallback and is covered by the existing width test in `singles()`:

| Field | Unicode | ASCII | Use |
| --- | --- | --- | --- |
| `rail` | `│` | `\|` | thinking body, expanded group guide |
| `rail_heavy` | `┃` | `\|` | live thinking rail (Grok's `accent_bar`) |
| `tree_mid` | `├─` | `\|-` | member row inside an expanded group |
| `tree_end` | `╰─` | `` `- `` | last member / live tail row |
| `pending` | `◆` | `*` | waiting on the user (pulses) |
| `failed` | `✗` | `x` | failed tool / group suffix |

`open` and `closed` (`▾` / `▸`) already exist. Replace the ad hoc `›` / `⌄` in
`transcript.rs` `tool_group` with them.

### 3.4 Tests
- `anim::wave` / `pulse` stay in range and are periodic. `shimmer_spans` keeps the text
  unchanged and has one span per grapheme.
- Glyph width test passes for the new fields. The colour-role parity test passes.
- Existing render snapshot and PTY tests pass unchanged. That shows D3 held: no visible change.
- `cargo test --manifest-path rust/widgets/Cargo.toml`, `cargo test --manifest-path rust/tui/Cargo.toml`.

## 4. Phase 2: verb groups and thinking

### 4.1 Projection (`nexus/ui/ratatui/`)
- Move `flush_activity` and its category table out of `prototype.py` into a new module,
  **`nexus/ui/ratatui/verb_groups.py`**. New terminal behaviour goes in its own module
  (rule 3). It gets a row in `docs/module-map.md`.
- **Membership is unchanged (D2).** The run is delimited by visible replies and by
  `task`/`subagent` rows and `change` blocks, exactly as in today's loop. Write a
  regression test that pins today's membership before moving the code.
- Vocabulary: keep today's categories and add Grok's present tense while any member is
  running. Use Grok's nouns where Nexus has only a generic one:

  | Tools | Past / present | Noun |
  | --- | --- | --- |
  | `bash`, `run` | Ran / Running | command(s) |
  | `read` | Read / Reading | file(s) |
  | `write` | Wrote / Writing | file(s) |
  | `apply_patch` | Applied / Applying | patch(es) |
  | `grep`, `glob` | Searched / Searching | pattern(s) (today: "time(s)") |
  | `ls`, `list` | Listed / Listing | dir(s) |
  | `webfetch` | Fetched / Fetching | website(s) |
  | `websearch` | Searched / Searching | website(s) |
  | `McpSearch` | Searched / Searching | MCP tool(s) |
  | `McpCall` | Called / Calling | MCP tool(s) |
  | thoughts | Thought | time(s) (unchanged) |
  | other | `<Tool>` (title-cased, as today) | call(s) |
  | external worker | `<owner> ran` | tool(s) (unchanged, from `tool.provenance`) |

- Label: `Ran 2 commands, Read 3 files, Thought 2 times`. This uses Grok's comma
  separator, replacing today's ` · `. A finished group adds ` · 1.8s` wall time, and any
  failure adds ` · 1 failed` in `error`.
- Group identity stays as today: `turn.id + ":activity:" + first durable member id`, so
  streaming additions keep local disclosure.
- New fields on the `tool_group` block (bridge `Content`):
  - `started_ms`: epoch ms of the first member's start, so Rust draws the live elapsed
    time without a Python round trip.
  - `elapsed_ms`: when finished.
  - `failures`: the field already exists and is unused.
  - `running_label`: the present-tense label.
- Auto-fold lifecycle:
  - While running, the group header animates. **Only the live member** shows beneath it,
    as a tree tail (`╰─ ⠋ Read src/main.rs`). This replaces `preview_limit = 1`, which
    has the same meaning drawn differently.
  - When assistant text starts after the run, or the turn ends, the group folds to its
    one-line header. Exceptions: a failed member stays visible as its own row under the
    header (red `✗`), and a member the user opened stays open.
  - A user's explicit expand or collapse always wins over auto-fold (today's semantics).
- Docs: `docs/ratatui-parity.md:551` and the "Totals describe the work" paragraph change
  (failures are now shown, and the separator changes). Record the reversal in `docs/decisions.md`.

### 4.2 Thinking
Reducer data already exists: `BlockView.elapsed_ms`, derived from `thinking.delta` /
`thinking.end` timestamps, so replay reproduces it.
- **Running:** `⠋ Thinking…`.
  - The spinner sits in `SPINNER_SLOT`. The label uses `shimmer_spans` in `running`.
  - Live elapsed time `(2.4s)` comes from a new `started_ms` field.
  - The body shows a live tail of the **last 5 wrapped rows**, behind a `┃` rail that
    pulses with `wave(tick,row)`, in dim italic `thinking`.
  - Earlier rows are announced with `│ … 37 earlier lines` (clipping is always announced).
- **Finished:** `▸ Thought for 3.2s`, with `Thought` in `gray_bright` bold and
  ` for 3.2s` in `muted` (Grok's `header_line`). Times use Grok's `format_time` (`3.2s`
  under a minute, then `1m20s`). Inside a run that has tool members, it stays a member,
  counted as today (D2).
- **A run with no tool members is not a group.** Today a reply preceded only by reasoning
  shows a `› Thought 1 time` group header. Instead, draw the thought rows themselves
  (`▸ Thought for 1.1s`). This is Grok's `RunScan::folds` rule: thoughts never count
  toward folding. Membership is unchanged; only the header is skipped.
- **Expanded** (click, as today): the whole trace behind a dim `│` rail. The whole-line
  `**heading**` stays bold.
- This replaces today's amber `Thought: 671ms` row. Update `docs/ratatui-parity.md` §Transcript.

### 4.3 Rust drawing (`rust/tui/src/transcript.rs`)
- `tool_group` header: `[spinner|▸|▾] <label>`. The text is `gray_bright` bold (Grok's
  header style), shown in `running` while live, with the wave on the glyph.
- Expanded members use `├─` / `╰─` connectors in `gray_dim`, and the member heading in `gray`.
- `thought`: the new drawing described in §4.2. Rows go through `wrap`, so CJK and wide glyphs stay correct.
- Column alignment rule (`docs/ratatui-parity.md` "Transcript column"): every row's first
  character stays aligned with the prompt text (column 5). Connectors go in the existing 4-column gutter.
- Keep the `"\u{e000}"` spinner-slot contract. Wave colouring is applied at draw time in
  `render.rs`, where the slot is already replaced, so cached rows are not rebuilt every tick.

### 4.4 Tests
- New `tests/test_ratatui_verb_groups.py`:
  - membership identical to today (pinned before the move);
  - each label in past and present tense;
  - `· N failed`;
  - opened members stay open through auto-fold;
  - group identity stays stable while streaming.
- `tests/test_ratatui_projection.py`: update the label expectations (comma separator, `pattern(s)`).
- Rust unit tests in `transcript.rs`:
  - connector layout;
  - column-5 alignment;
  - a failed row stays visible under a folded header;
  - the thinking live tail is capped at 5 rows, with the clip notice.
- Replay: the same blocks give the same rows (no clock state in the projection).

## 5. Phase 3: double-click and the relevant-only tool view (D1)

### 5.1 Click tracking (`rust/tui/src/clicks.rs`, new)
```rust
pub const MULTI_CLICK: Duration = Duration::from_millis(300);
pub struct ClickTracker { last: Option<(Instant, u16 /*line*/, u16 /*col*/, String /*operation key*/)> }
impl ClickTracker {
    /// true on the second click: same operation key, within MULTI_CLICK, within 2 cells.
    pub fn is_double(&mut self, now: Instant, line: u16, col: u16, key: &str) -> bool;
}
```
- Hook it into the `MouseEventKind::Up(Left)` branch of `main.rs`, inside the existing
  "no text was selected" branch, so a drag-select never toggles. Copy-button and link hits
  are handled first and never count as clicks.
- If the resolved operation belongs to a **tool row** (a standalone `tool`, or a member's
  `operation` / `output_operation`):
  - first click: set focus on that row (the same state as keyboard focus in `render::targets`);
  - second click: run today's toggle path (`cache.disclosure.toggle(...)`, else the host `operation`).
- Any other operation (group header, thought, prompt card, chips) keeps today's single-click path.
- Keyboard: Enter/Space on a focused tool toggles it, and `i` opens the full details (§5.3).

### 5.2 Relevant-only view (`nexus/ui_support/tool_signal.py`, new)
A pure function `tool_signal(tool: ToolCallView) -> SignalView`. It returns typed body
rows, and the projection sends them as the tool's expanded body (`local_detail`, or
`detail` when not local). Rows show only what the tool did and produced. It never shows
Call ID, Requested/Started/Finished timestamps, Status, Bundle, Model iteration,
Executed, or a parameter dump.

| Tool | Row (collapsed) | Expanded on double-click |
| --- | --- | --- |
| `read` | `Read path/to/file.rs (lines 1–120)` | The excerpt with line numbers. Long reads show the first 5 and last 3 lines (Grok's `FIRST_LINES`/`LAST_LINES`), and the middle is announced (`… 112 lines`). |
| `write` | `Wrote path (84 lines)` | The content written, as added lines. |
| `bash` / `run` | `$ cargo test -p nexus-tui · 0.8s` | `$ <command>`, then stdout/stderr (`escape_controls`). A leading `cd <workspace> && ` is removed only when it equals the session workspace. A red `exit 2` / `timed out after 120s` line appears only on failure. |
| `grep` / `glob` / `ls` | `Searched "render_mermaid" in rust/ · 6 matches` | `path:line: snippet` rows (or paths for glob/ls). |
| `websearch` / `webfetch` | `Searched web for "…"` | `Title — url` rows, or the fetched excerpt as Markdown. |
| `McpCall` / MCP / other | `server · tool (key=val)` | Only the non-empty parameters the call was made with, as `key: value`, then the result (Markdown when it looks like Markdown, else labelled rows). |
| any failed call | row in `error` | The error text first, then the output above. |

- Edits stay `change` blocks with their diffs (D2). `task`/`subagent` rows keep their
  existing one-row form and child page.
- Every body is bounded (`SECTION_ROWS`, `VALUE_LIMIT`), and any clip is announced (`… 240 more lines`).
- Nested `members` (diffs attached to a tool) still render under it.

### 5.3 Full details stay reachable
- `nexus/ui_support/tool_details.py` is unchanged. Its web port `ui/web/js/tool-details.js` stays in step.
- `i` on a focused tool opens the existing labelled-sections view (Overview, Parameters,
  Result, Metrics, …) in the details panel. `/verbose` behaves as today.
- Record in `docs/decisions.md`: "Expanding a tool shows only what the tool did and
  produced. Metadata lives behind `i`, so nothing the agent saw is unreachable."
- Add `i` to the shortcuts list (`nexus/ui_support/shortcuts.py`) and `docs/cli.md`.

### 5.4 Tests
- `tests/test_ui_tool_signal.py` (new):
  - one case per row of the table;
  - no metadata labels appear in any output;
  - the `cd` stripping rule removes only an exact workspace match;
  - the failure line appears only on failure;
  - clipping is announced.
- `tests/test_ui_tool_details.py` is unchanged and must still pass (the full view is intact).
- `rust/tui` `clicks.rs` unit tests: the timing window, distance, a different key resets, a drag never counts.
- `tests/test_ratatui_pty.py`:
  - a single click on a tool focuses it and does not expand it;
  - a double-click expands it to the relevant-only view, and another double-click collapses it;
  - `i` opens the full details;
  - a single click still toggles a group header and a thought;
  - a drag selection never toggles.

## 6. Phase 4: Sessions dashboard

### 6.1 Naming and entry points
- Title **Sessions**. Header: `Sessions  3 active · 14 total`.
- `/sessions` opens the dashboard, and Escape returns to the conversation.
- The 40-column sidebar stays exactly as documented (`docs/ratatui-parity.md:448`). Its
  title gains a `⤢` control that opens the dashboard.
- `/archived` opens the dashboard filtered to Archived.
- The dashboard replaces the transcript region, not a floating dialog. This mirrors
  Grok's `ActiveView::AgentDashboard`. Sidebars stay.

### 6.2 Layout (`rust/tui/src/render/sessions.rs`, new; kept out of the 1,000-line `dialogs.rs`)
- **≥ 110 columns:** list on the left (≥ 56 columns), preview on the right.
- **60–109 columns:** list on top, preview below it (Grok's rule: the preview takes at
  most 3/8 of the height, the list keeps ≥ 12 rows, and the preview hides below that).
- **< 60 columns:** list only. Enter opens the session.
- Grouping (`g` toggles, Grok's `Grouping`):
  - **State** (default): sections *Needs input*, *Working*, *Today*, *Earlier*, and
    *Archived* when the filter includes it.
  - **Project**: reuses `session_groups.py` (project · day, worktrees under their repository).
- Row layout: `<status> <title>  <chips>  <age>`.
  - **status** comes from `session_status.py` (`working`/`input`/`done`/`idle`): `⠋`
    working (spinner), `◆` needs input (pulses, `PULSE_SPEED`), `●` finished since last
    viewed, `○` idle.
  - **chips** have no brackets and use the `gray_dim` tone (Grok's `row_title.rs`):
    `⎇ feat/ui` when `worktree` is set, the agent name in its colour, and the project name
    when grouping by state. Chips collapse before the title truncates (Grok's rule).
  - **age** uses the existing `relative_time`.
- Preview, top to bottom:
  - workspace path, branch, agent, model;
  - a context fraction bar (the existing meter);
  - a read-only mini transcript: the last user prompt (2 lines), then the active
    verb-group label or the last reply excerpt (6 lines), then any pending approval or
    question as a `◆` card.
- Header row: a scope toggle `This project | All projects` (reuses the project chips'
  data) and a `/ filter` box (the same fuzzy match as the sidebar).

### 6.3 Keys and mouse
| Input | Action |
| --- | --- |
| `↑`/`↓`, `j`/`k` | move |
| Enter, double-click | open session (a single click selects) |
| `Ctrl+N` | new session |
| `r` | rename inline (Enter saves, Escape cancels) |
| `d` / Delete | archive; refused with a toast on *working* or *needs input* rows (Grok's `allows_delete`) |
| `u` | restore (Archived filter) |
| `Tab` | focus the preview; arrows scroll it |
| `g` | group by state / project |
| `/` | filter |
| Escape | close (or clear the filter first) |

### 6.4 Host contract (rule 4)
- **`SessionPreview(session_id, max_rows=12)`** → `SessionPreviewResult` (new, in
  `nexus/host/protocol.py` and `facade.py`). It returns bounded rows built by the same
  projection helpers as the transcript, a context fraction, and a pending-input summary.
  The UI never reads another session's records.
- **`SessionRename(session_id, title)`** (new). The rename is durable: it appends a title
  record, so replay and other clients see it. Check how `host_support/auto_title.py`
  stores titles and use the same record, marked as user-set so auto-titling no longer
  overwrites it.
- Archive and restore use the existing `SessionDelete` / `SessionRestore`.
- Live status comes from the existing session poll. The preview refreshes at most once a
  second, for the highlighted row only.

### 6.5 Tests and mockups
- `design-mockups/src/sessions.rs`: add the dashboard at 140, 90 and 50 columns, and
  refresh `design-mockups/shots/` (`cargo test --manifest-path design-mockups/Cargo.toml`).
- `tests/test_host_facade.py`:
  - `SessionPreview` is bounded and redacted;
  - `SessionRename` persists across reopen;
  - archiving a working session is refused.
- `tests/test_ratatui_pty_sessions.py`:
  - `/sessions` opens the dashboard;
  - double-clicking a row switches to that session;
  - `r` renames;
  - `g` regroups;
  - the narrow layouts render.

## 7. Phase 5: Mermaid as Unicode diagrams (D4)

### 7.1 Port (`rust/tui/src/mermaid.rs`)
- Copy `grok:xai-grok-markdown/src/mermaid.rs` with its in-file tests. It depends only on
  `ratatui` and `unicode-width`, at the versions Nexus already uses (ratatui 0.29,
  unicode-width 0.2). **No new crates.**
- Keep the Apache-2.0 header. Add `// Ported from xai-org/grok-build 2bdd1d6,
  crates/codegen/xai-grok-markdown/src/mermaid.rs; modified by Nexus`, and list the local
  changes in the module docstring.
- Local changes:
  1. **Change the result type.** `render` returns `Option<MermaidArt>`: `Some` only when a
     diagram was drawn and fits `max_width`. Grok's internal `fallback()` framed source box
     is removed, so a diagram that is too wide (`Oversize::Width`), over the cell cap,
     unparseable, or of an unsupported type returns `None`.
  2. Map `MermaidStyles` onto `Palette`: `border` = `rail`, `node_text` = `text`,
     `edge` = `gray`, `edge_label` = `muted`, `title` = `gray_bright`.
  3. Drop `plain_lines` if nothing in Nexus uses it (copy uses the source, below).
  4. **Retry top-down before giving up.** If an `LR`/`RL` flowchart returns
     `Oversize::Width`, lay it out again as `TD` and use that if it fits. Grok's own
     screenshot of an 8-node LR flowchart needs ~150 columns. The same graph top-down fits
     in ~40. Only when both fail does the fence fall back to a code block.
- What the engine does (verified in the source):
  - Dispatch tries, in order: flowchart/graph (subgraphs to depth 6, TD/TB/BT/LR/RL),
    stateDiagram, classDiagram, erDiagram, sequenceDiagram.
  - Layered layout: DFS back-edge reversal, then barycenter ordering with crossing counts,
    then a character canvas that merges box-drawing joins, with dotted and thick edges
    and arrowheads.
  - Labels wrap at 24 columns, at most 4 lines.
  - Hard bounds: 128 nodes, 512 edges, 24 groups, 2²¹ canvas cells.

### 7.2 Hook (`rust/tui/src/markdown.rs::fence`)
Today a `mermaid` fence takes a separate literal path, with the comment "this text-only
transcript cannot place graphical output". Replace that path:
1. If the language's first token is `mermaid` (accepting extras such as
   `mermaid theme=dark`), the fence is **closed**, and
   `mermaid::render(body, &styles, Some(available_width))` returns `Some(art)`: push the
   art's lines on the **transcript background, with no code-block panel**, as Grok does,
   starting at the reply column. Put a quiet `◇ mermaid` label row directly under the art
   (where Grok puts its affordance row, but without the image buttons). Every row keeps
   `fence: Some(index)`, so the copy button still copies the **source**
   (`fence_sources`, `copy_button.rs`).
2. **Otherwise**, render it through the normal code-block path
   (`MarkdownBlock::code_block("mermaid", …)`), exactly like any other language. This
   covers a still-streaming fence, an unsupported type (pie, gantt, mindmap, …), a parse
   failure, and a diagram that does not fit. It looks exactly like today's output (panel
   background, `mermaid` language label). There is no Grok-style framed box, no "too wide"
   hint and no `[Open Image]` row.
3. The decision is re-made on resize. A diagram that stops fitting turns back into a code
   block, and one that fits again draws again.
4. Cache the rendered art per `(source hash, width)`, so scrolling does not lay a diagram
   out again.
5. Detect whether a fence is closed from the source text (a closing fence line after the
   body). pulldown-cmark ends an unclosed fence at the end of the document, so the parser
   alone cannot tell. Grok's `test_mermaid_streaming_matches_full` sets the bar: the
   finished render equals a one-shot render.

There is no art/source toggle. The source is always one copy-button click away, and
anything that cannot be drawn already shows as source.

Docs:
- `docs/ratatui-parity.md:503–506`: replace "Mermaid is labelled literal source" with the new rule.
- `docs/security.md`: Mermaid source is untrusted model output; the layout is bounded and does no I/O.
- `docs/module-map.md`: add the new module.

### 7.3 Tests
- Port Grok's in-file `mermaid.rs` tests, adjusted for the `Option` return.
- Nexus tests in `markdown.rs`:
  - flowchart TD and LR at 80 columns draw box art;
  - a sequence diagram draws;
  - `pie` → normal code block;
  - an LR flowchart too wide for LR but fitting TD → drawn top-down;
  - a flowchart too wide either way → normal code block;
  - an unclosed fence → normal code block;
  - CJK labels;
  - copy yields the source, not the art;
  - a resize across the fit threshold switches between the two forms.
- A bounded fuzz-style test: random fence bodies render or return `None` within a time
  limit and never panic (Grok ships `fuzz/fuzz_targets/render_all.rs`).
- `tests/test_ratatui_pty.py`: a scripted reply with a mermaid flowchart draws `┌`, `─`
  and `▼` in the PTY screen. The same reply in a narrow PTY shows the source lines.

## 8. Composer (user instruction)

The composer stays as it is. The only change is removing the **left colour border**.
- Remove the agent-coloured `┃` / `╹` rail loop in `rust/tui/src/render.rs` (~lines 1322–1350).
  The card background then starts at `box_area.x`, with the same inner padding as today,
  so the text column does not move. Keep the `▀` half-row bottom edge.
- Information the rail carried must stay visible:
  - Agent identity: the agent control on the runtime row, which is already drawn in the agent colour.
  - Shell mode (today the rail turns `warning`): the existing bash indicator. Make sure it
    is coloured `warning`, so the mode is still unmistakable.
- Anchors that refer to the rail move to the card's left edge:
  - the queue box (`queue_box`, "on the composer rail's columns");
  - the workspace breadcrumb (`chrome.rs::breadcrumb_row`).
- Remove the matching `▎` from the mockup at `design-mockups/src/screens.rs:144`.
- Tests to update:
  - `shell_drafts_show_a_bash_indicator_and_warning_rail`: assert the indicator colour, not the rail;
  - `breadcrumb_starts_at_the_composer_rail_and_keeps_suffix`;
  - the rail-edge assertions near `render.rs:3474`.
- Docs: rewrite the rail descriptions in `docs/ratatui-parity.md` (~lines 195–255 and ~392).
- The live/voice strip and the `Sending next` queue box stay docked above the composer, unchanged.

## 8A. Scrolling: the sent prompt goes to the top, with room below (user instruction)

User instruction (2026-10-14): when the user sends a message, it should appear at the top
of the transcript, and the transcript should scroll well below the last message.

Today:
- `rust/tui/src/render.rs:1198` sets `cache.max_scroll = total - height`, so the last
  row can never rise above the bottom edge.
- `main.rs` keeps one `follow: bool` that snaps `scroll` to `max_scroll`.
- A short reply therefore sits at the bottom of the screen with the old conversation above it.

Grok has the same idea as its "page-flip pose": after a send, the prompt is pinned at the
top of the viewport. Bottom padding, held for the turn, makes that position a real scroll
bottom (`grok:xai-grok-pager/src/scrollback/state/pin_reserve.rs`). Nexus makes the
padding permanent, as asked.

### 8A.1 Behaviour
- **Tail padding, always.** The scrollable height is `content + pad`, with
  `pad = viewport_height − 2`. The wheel, PageDown and Down can scroll until only the last
  content row (plus one margin row) is left at the top. The padding is plain transcript
  background: no rule, no marker.
- **Send → prompt at the top.** On submit, the view arms a pin. When the new prompt card
  arrives from the host (the first `user` block with an unseen `turn_id`), scroll so the
  card's top row sits on the first transcript row, after the one-row gap. The pin is
  keyed by that `turn_id`, not a row index (Grok resolves by stable prompt id the same
  way). Folding of older turns, the context header above it, or a resize therefore
  re-derive the same pose.
- **While the reply streams:**
  - The prompt stays pinned at the top, and the reply grows downward into the padding.
  - When the reply's newest row reaches the viewport bottom, the view switches to
    following the **live row** (the newest content row held at the bottom edge, not the
    padded end). The prompt then scrolls off the top as text keeps coming.
- **Reader control (unchanged rules, new targets):**
  - Any scroll up releases the pin and stops following, as today.
  - Scrolling down until the live row is back at the bottom resumes following.
  - Scrolling further down goes into the padding without resuming follow.
  - `Ctrl+End`, typing and sending all return to the live row (sending re-pins to the new prompt).
- **Opening or switching a session** lands with the last content row at the bottom, not
  at the padded end, so the screen is never blank on arrival. If the last turn is still
  running, it pins that turn's prompt instead.
- Subagent pages (`agent_page`) keep their own saved position (`page_positions`). The
  same rules apply inside them.

### 8A.2 Implementation (Rust-local, no host change)
- `render.rs`: compute `content_total` and `pad` separately. `max_scroll` becomes
  `content_total + pad − height`. Add `live_scroll = content_total.saturating_sub(height)`
  (today's `max_scroll` meaning), and `prompt_top(turn_id) -> Option<usize>` from the cached
  rows. Each `user` block's first row carries its `turn_id` in `cache.operations`.
- New module `rust/tui/src/scroll.rs` (new terminal behaviour, own module):
  ```rust
  pub enum Follow { Live, Pin { turn: Option<String> /* None = armed, waiting for the card */ }, Free }
  pub fn target(follow: &Follow, cache: &Cache) -> Option<usize>; // where to put `scroll` this frame
  ```
  `follow: bool` in `main.rs` becomes `Follow`. Every `follow = true` site maps to
  `Follow::Live`, and the submit path sets `Follow::Pin { turn: None }`. The ~10
  `scroll >= cache.max_scroll` checks change to compare against `live_scroll`.
- `page_positions` stores `Follow` instead of `bool`.
- The transcript scrollbar/position indicator, if shown, counts the padding as scrollable space.
- Docs: rewrite the auto-scroll paragraph in `docs/ratatui-parity.md` ("The transcript
  auto-scrolls only while the reader is at the bottom…"). Add a `docs/decisions.md` entry:
  permanent padding, plus a pin by turn id rather than row.

### 8A.3 Tests
- `scroll.rs` unit tests:
  - the pad size;
  - a pin waits for a new `turn_id`, then targets its top row;
  - the pin survives a fold above it and a resize;
  - switching to `Live` when the reply reaches the bottom;
  - a scroll up gives `Free`;
  - scrolling down to `live_scroll` gives `Live`;
  - scrolling past `live_scroll` stays `Free`.
- `render.rs`: `max_scroll` includes the pad; an empty session has no pad beyond one screen.
- `tests/test_ratatui_pty.py`:
  - after sending in a long session, the prompt's text is on the first transcript row and
    the rows below the short reply are blank;
  - wheel-down scrolls past the last message;
  - `Ctrl+End` brings the last row back to the bottom;
  - a long streamed reply ends with its newest line on the bottom row.

## 8B. Grey headers; the agent colour only on the agent name and the activity bar (user instruction)

User instruction (2026-10-14):
- Headers are grey, and inactive headers are a darker grey.
- Remove the agent colour from headers.
- Keep the agent colour only on the agent name and on the activity/context animation bar at the bottom.

### 8B.1 Where the agent colour stays
| Site | Code |
| --- | --- |
| Agent name in the composer controls (`Build`) | `render/chrome.rs::control_spans` (`accent`) |
| Subagent **name** in a `task` row (e.g. `Explore`) | `transcript.rs` `"task"` (~line 1095). Only the name span; the rest of the row is grey. |
| Activity meter (the moving fifth under the workspace bar while working) | `render.rs::activity_meter` |

### 8B.2 Where it is removed
| Site | Today | After |
| --- | --- | --- |
| Context header titles (`System prompt`, `Environment`, `AGENTS.md`, `Skills`, `Tools`, …) | agent colour (blue in the screenshot), bold | **active:** `header` grey (`muted`), bold kept. **Inactive** (empty, `~0`, e.g. `MEMORY.md`, `MCP [0]`): `header_inactive`, the darker grey (`quiet`), not bold. |
| Context header `│` rails | agent colour | same grey as the title: `header` / `header_inactive` |
| Token figures beside headers (and counts, when open) | `muted` / `quiet` | always one step darker than the title. Active: title `muted`, tokens `quiet`. Inactive: title `quiet`, tokens `gray_dim`. Headers are closed by default (§8C). |
| `context` block chip (`render` ~line 791, inspector rows) | agent-coloured chip background | `element` grey background, with `text` foreground (`muted` when inactive) |
| Prompt card rail (user message, `user_card`, ~line 204) | agent colour (the blue bar in the screenshot) | `border_strong` grey. It is not an agent name. |
| Turn fold glyph / turn header tone (~line 518) | agent colour while running | `running` (neutral) |
| Verb-group header and spinner while running (~line 938) | agent colour | `running` (neutral, with the §3.2 wave) |
| `task` row glyph and title (~line 1092) | agent colour | glyph `running`, title `text`. The subagent name keeps its colour (8B.1). |
| Composer rail | agent colour | removed entirely (§8) |
| Sessions dashboard (§6) | – | agent-name chip coloured. Everything else grey. |

Not affected: the Live call card (`live_card`, purple: a live-call cue, not the agent),
semantic colours (`error`, `warning`, `success`, diff green/red), and project hues in the
sessions sidebar (`project_color`: project identity, not the agent).

### 8B.2a The `◆ <agent>` transcript label is removed (user instruction)
- No transcript row names the agent. The agent is named only in the composer controls.
- Checked 2026-10-14: the Python projection no longer emits an `agent` block. What
  remains is dead code and stale docs:
  - delete the `"agent"` arm in `rust/tui/src/transcript.rs` (~line 836);
  - remove `"agent"` from the kind list in the padding test (~line 1255) and from the
    `role in {"assistant", "agent"}` gap rule in `prototype.py` (~line 463);
  - remove the `◆` agent-label mentions in `docs/ratatui-parity.md` (~lines 174, 282, 403);
  - remove `agent` from the keyboard-focus target list (~line 577: "tool row, thought,
    prompt card, agent card, diff") if nothing else produces it.
- Add a projection test: no block of kind `agent` is emitted, for a normal turn, a turn
  after `/agent` switches agent, or a subagent page.
- A turn's agent stays visible where it matters: the composer name for the current
  agent, and a subagent's name in its `task` row.

### 8B.3 Projection change (`nexus/ui/ratatui/prototype.py`)
- Today "inactive" is implicit: an empty block "arrives with the neutral colour and is
  drawn greyed out" (`transcript.rs` ~line 735).
- Make it explicit. Each `context_header` member and each `context` block carries
  `active: bool`: false when the block contributes nothing (tokens `0` or unknown with an
  empty inventory, disabled, or not loaded).
- Stop sending `_agent_color(...)` as `color` on the `context_header`, `context`,
  `tool_group` and user-card blocks. Keep it only on `task` blocks (subagent name), and in
  `snapshot["agent_color"]` (used by the composer name and the activity meter).
- `_agent_color` stays for those uses.

### 8B.4 Tests and docs
- Rust tests:
  - context header titles and rails are `muted` when active and `quiet` when inactive,
    and never the agent colour;
  - the prompt card rail is grey;
  - `activity_rule_is_quiet_when_idle_and_agent_colored_when_running` still passes
    unchanged (the bar keeps the colour);
  - the composer agent name is agent-coloured.
- `tests/test_ratatui_projection.py`:
  - `active` is set correctly for empty, disabled and populated blocks;
  - no `color` on header, context or group blocks;
  - `task` blocks keep theirs;
  - no `agent` block is emitted (8B.2a).
- `docs/ratatui-parity.md`: rewrite the context-header lines (~62–66: "agent colour and
  carries a `│` left rail"), the prompt-card rail note (~166), and line ~244 ("The
  composer border and agent name share the active agent's context-header colour").
- `docs/decisions.md`: the agent colour marks only the agent's name (composer, subagent
  rows) and its live activity bar, so colour means "which agent" and nothing else. The
  transcript carries no per-turn agent label.

## 8C. Context headers closed by default (user instruction)

User instruction (2026-10-14):
- Context headers are closed by default. Nothing is shown except each header's name and
  its token count.
- The token count is a darker grey than the name.

### 8C.1 Closed (default)
One row per header, nothing beneath it:

```
│ System prompt  ~348
│ Environment  ~25
│ AGENTS.md  ~2.5K
│ MEMORY.md  ~0            ← inactive: whole row darker
│ Skills  ~102
│ Tools  ~4.4K
│ MCP  ~0                  ← inactive
```

- **Name:** `header` grey (`muted`), bold. Inactive: `header_inactive` (`quiet`), not bold (§8B).
- **Tokens:** always one step darker than the name. That is `quiet` beside an active name,
  `gray_dim` beside an inactive one. The format is `~348`, with no "tokens" word, and
  `~?` when the count is unknown (today: "tokens unknown").
- **Not shown when closed:** the `[13]` / `[1 0]` counts, the one-line previews (`You are
  an expert coding assistant…`, `# AGENTS.md`), the inventories (the tools grid, the skill
  list) and the `▸` glyph. The closed row is exactly name + tokens.
- **Rail:** the `│` rail stays, in the same grey as the name.
- Spacing: closed headers stack with **no blank row between them**, which today's
  `gap: 1` per block adds. The whole header strip keeps one blank row before the first
  prompt card.

### 8C.2 Open
- A single click on a header row, or Enter/Space when it is focused, opens that one
  header. It is a header, not a tool row, so D1's single-click rule applies.
- When open it shows what it shows today: the counts beside the name, and below it the
  preview or inventory, behind the same rail.
- Clicking again closes it. Each header opens independently.
- Open state is Rust-local disclosure (`disclosure.rs`), keyed `context:<key>` in the
  session scope. Every session, and every newly opened one, starts with all headers
  closed. Opened headers stay open while the session stays in the bounded LRU, like other
  disclosure.
- `/verbose` opens all of them, as it reveals everything else.

### 8C.3 The Context Sections inspector moves to `i`
- Today a click on a header runs `context_show` (the inspector page with per-section
  toggles). A click now toggles inline, so the inspector is reached by:
  - `i` on a focused header: the same key that opens a tool's full details (§5.3);
  - the header strip's `context_menu` operation and the existing `/context` command, both
    unchanged.
- Update the PTY test that clicks a header expecting the inspector
  (`tests/test_ratatui_pty_context_sections.py`), and the operation map in `main.rs`
  (~lines 2661–2662).

### 8C.4 Code
- `nexus/ui/ratatui/prototype.py` (`_context_blocks`, `_compact_header`):
  - send each header with `title`, `tokens` (compact string), `active` (§8B.3), and the
    body fields (`counts`, `text`, `local_preview`) marked `local_ui: True, local_open: False`,
    so Rust can toggle without a round trip;
  - drop the per-block `gap: 1` inside the strip.
- `rust/tui/src/transcript.rs` `context_header` / `context`:
  - closed: draw `rail name  tokens` only;
  - open: today's drawing.
- The `context_preview` preference (Settings → Layout, default `True`) keeps its meaning:
  show the header strip at all. It does not open the headers.
- Docs:
  - `docs/ratatui-parity.md` ~lines 60–70 and 545–550: closed by default, name + tokens,
    the darker token tone, click to open, `i` for the inspector;
  - `docs/cli.md` and `nexus/ui_support/shortcuts.py`: the `i` key on headers.

### 8C.5 Tests
- Rust:
  - a closed header draws exactly one row (name + tokens), with no count, preview or inventory;
  - the token span is darker than the name for active and inactive headers;
  - no blank rows between closed headers;
  - a click opens one header only, and a second click closes it;
  - a new session scope starts closed.
- `tests/test_ratatui_projection.py`: `tokens` is formatted as `~348` / `~2.5K` / `~?`;
  `active` is correct; the body is sent but marked closed.
- `tests/test_ratatui_pty_context_sections.py`:
  - the header renders closed on start;
  - a click opens it inline;
  - `i` opens the inspector;
  - the section toggles inside the inspector still work.

## 9. Nexus-only features that must keep working

Colours do not change (D3). Each feature keeps its existing tests, and Phase 6 adds a screenshot check.

| Feature | Code | Change |
| --- | --- | --- |
| Live call / dictation strip | `rust/tui/src/live_bar.rs`, `nexus/ui/ratatui/live.py`, `voice.py` | The recording dot may use `pulse`; otherwise none. |
| Context header and Context Sections inspector | `transcript.rs` `context_header`, `render/context.rs`, `ui/ratatui/context_sections.py` | Grey instead of agent colour, inactive darker (§8B). Headers closed by default; the inspector is reached with `i`, the context menu or `/context` (§8C). The inspector itself is unchanged. |
| Provider usage and thresholds | `host_support/provider_usage.py`, runtime row | None. |
| Model tiers | `tier_pages.py`, `ui_support/tier_settings.py` | None. |
| Subagent rows, pages and worktree review | `task` block, `agents/worktrees.py`, details sidebar | None to the rows (D2). The worktree `⎇` chip appears in the dashboard. |
| External worker cards | `transcript.rs` `external` | None. |
| Hot extensions and quarantine | `nexus/ext/`, settings pages | None. |
| Split diffs and `change` blocks | `transcript.rs` `diff` / `change` | None. |

## 10. Licensing and attribution
- Add `rust/tui/NOTICE` naming Grok Build (Apache-2.0, © SpaceXAI) for `mermaid.rs`.
- The ported file keeps the upstream licence header and adds the "modified by Nexus" note (Apache-2.0 §4b).
- Only `mermaid.rs` is copied. Everything else (the verb-group label rules, the wave
  function, the dashboard layout numbers) is reimplemented from this plan, with a comment
  citing the Grok path.
- `references/` is a local reference clone and is not committed. Add it to `.gitignore`.

## 11. Order of work, sizes and exit criteria

Status column reflects the evidence in the final ledger (§0), scoped to what was actually run: ✅ = exit criteria met by the recorded tests/PTY runs; ◐ = met except the named gap.

| Phase | Depends on | Size | Status | Exit criteria |
| --- | --- | --- | --- | --- |
| 0. Composer rail removal (§8) | – | S | ✅ | Rail gone; text column unchanged; shell mode still visible; tests and doc updated |
| 0c. Grey headers, agent colour narrowed, agent label removed (§8B) | 1 (roles) | S | ✅ | Agent colour only on the composer agent name, subagent names and the activity bar; inactive headers darker; no `agent` transcript block or code path; tests and docs updated |
| 0d. Context headers closed by default (§8C) | 0c | S | ✅ | Closed rows show name + darker tokens only; click opens inline; `i` opens the inspector; PTY context tests updated |
| 0b. Prompt-to-top scrolling and tail padding (§8A) | – | M | ◐ | Prompt pinned to the top after send; scroll past the last message; opening a session never lands on blank padding; PTY tests pass. Gap: the `Follow` enum and `scroll.rs` module of §8A.2 do not exist as specified; behaviour is Rust-local in `session.rs` and its tests/PTY pass |
| 1. Roles, glyphs, animation (§3) | – | S | ◐ | No visible change; anim and parity tests pass. Gap: Animate activity is a `tui.json` key and key binding plus the Appearance popup; the animate-only idle scenario is not timed |
| 2. Verb groups and thinking (§4) | 1 | M | ✅ | Membership pinned and unchanged; projection, PTY and replay tests pass; parity doc rewritten; reversal recorded |
| 3. Double-click and relevant-only view (§5) | 2 | M | ✅ | D1 behaviour in PTY; no metadata in the expanded view; `i` reaches full details; `test_ui_tool_details.py` unchanged and passing |
| 4. Sessions dashboard (§6) | 1 | L | ✅ | New host commands tested; mockups refreshed (`design-mockups` 20 passed); PTY journey passes; dashboard screenshots reviewed at 50/90/140 |
| 5. Mermaid (§7) | – | M | ✅ | Ported and Nexus tests pass; PTY shows box art when it fits and a code block when not (seen in the mermaid screenshots); NOTICE added |
| 6. Polish and verification (§12) | all | S | ◐ | Screenshot review of every surface in §9, dark and light, and with `NEXUS_ASCII=1`. Done for the 24-capture matrix (dashboard, groups, mermaid, context × 50/90/140 × dark/light, PTY→xterm, not OS screenshots). Open: web check fails (archive sidebar check, line 762, unresolved); no animate-only idle timing; surfaces outside the four scenes were not part of this matrix |

Phases 0, 0b, 1 and 5 can run in parallel. Each phase is its own Conventional Commit series
(`feat(tui): …`), with docs in the same commit.

## 12. Verification
- `.venv/bin/python -m pytest -q` (the full offline suite) and `ruff check nexus tests`.
- `cargo test` for `rust/widgets`, `rust/tui` and `design-mockups`.
- `tests/test_layering.py` and `tests/test_ui_layering.py`: the new Python modules live
  under `nexus/ui/ratatui/` and `nexus/ui_support/` and import only what rule 2 allows.
- `tests/test_docs.py`: the new modules have `docs/module-map.md` rows.
- PTY suites: `tests/test_ratatui_pty.py`, `test_ratatui_pty_sessions.py`,
  `test_ratatui_pty_live.py` and `test_ratatui_pty_context_sections.py`.
- `tests/ratatui_performance_check.py`: idle CPU is unchanged, and redraws while running
  stay at or below today's rate.
- The web check still passes (`tests/playwright_web_check.py`), since shared Python output changed.
- Visual review with the native screenshot skill (`skills/native-app-review/SKILL.md`) at
  50, 90 and 140 columns. The docs say "not verified" for anything not run against a real
  terminal or live provider.

## 13. Risks
- **Click semantics on tool rows (D1)** change muscle memory, and some PTY tests click
  tool rows to toggle them. Mitigations:
  - Enter/Space still toggle;
  - only tool rows change;
  - tests are updated in the same commit.
- **Hiding by summarising.** The relevant-only view drops metadata. Mitigations:
  - the full details stay one key away (`i`) and in `/verbose`;
  - failures are never folded away;
  - every group header keeps its counts.
- **Animation cost** over SSH and tmux. The wave applies only to visible running rows,
  and **Animate activity** turns it off.
- **Mermaid on hostile input.** The ported engine is bounded (node, edge and cell caps),
  but it is still 5.7k lines of parser over untrusted text. The fuzz-style test and the
  `None` → code-block path cover it.
- **Upstream drift.** The Grok commit is recorded in the port header. Re-sync deliberately, never automatically.
