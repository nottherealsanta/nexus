# Native TUI redesign plan

Status: implemented with a remaining streaming CPU budget gap (2026-10-02). Scope: the native Ratatui client only
(`rust/tui/`, `nexus/ui/ratatui/`, pure helpers in `nexus/ui_support/`).
The web client is out of scope. Textual is the fallback and does not need to
follow these changes; where a helper is shared, keep Textual's output unchanged.

This plan supersedes §3 ("Composer context figures and activity line") of
`RATATUI_UI_REFINEMENT_PLAN.md` and the "full-height sidebars are optional"
note in its §1.

## Goal

A terminal interface that is calm, aligned and fast. Fewer glyphs and less
padding, one clear column grid, and no row that does not carry information.
Context stays fully visible: nothing the agent sees is hidden, but detail sits
behind one keystroke or click instead of filling the transcript.

Two hard requirements apply to every item below:

1. **Fast.** Scrolling, typing and streaming should feel instant on a
   2,000-turn session. Budgets are listed in §9.
2. **Functional.** Every mouse target has a keyboard path, every collapsed
   thing announces what it hides, and clicks map to the same rectangles that
   were drawn.

## Design principles

- **One grid.** Transcript content starts at a fixed text column (col 4 inside
  the transcript). Glyphs such as chevrons, batch counts, spinners and bars sit
  in the gutter (cols 0–3) and never push text right.
- **One accent per session.** The agent colour is used for the composer bar, the
  context bars, the activity sweep and the context header chips. It changes
  everywhere at once when the agent changes.
- **No dead rows.** Every blank row is deliberate: one between turns, one
  between a message and its tools. Nowhere else.
- **Collapsed by default, never hidden.** Groups and folded output always show a
  count ("7 tools", "142 lines"), and Enter or a click expands them.
- **Python decides what, Rust decides how.** Grouping, counts, labels and
  colours are projected in `prototype.py`. Rust wraps, pads and paints.

## Target layout

### Left sidebar closed (tabs visible)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ☰  ● Fix the parser ×   ○ Release notes ×   +                             ▐  │ tabs
│ nexus · main                                                    ● running    │ breadcrumb
│──────────────────────────────────────────────────────────────────────────────│
│                                                                              │
│  ▌System prompt  ~3.1K   ▌Tools 24  ~9.8K   ▌AGENTS.md  ~2.2K   ▌Skills 6    │ context header
│                                                                              │
│ ▾ Make the parser accept trailing commas                                #12  │ user
│                                                                              │
│    I'll look at the grammar first.                                           │ assistant
│                                                                              │
│  ⠹ 5  Grep "trailing" · nexus/parse/                                         │ tool group (running)
│                                                                              │
│ ┃                                                                            │
│ ┃  Type a message…                                                           │ composer
│ ┃                                                                            │
│ ┃  Build · opus-5.5 anthropic · high          ╱╱╱╱╱╱╱╱╱╱  52K · 26% · 5.2%    │ controls + context
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │ activity
└──────────────────────────────────────────────────────────────────────────────┘
```

### Left sidebar open (tab row removed, sidebars full height)

```
┌───────────────────────┬──────────────────────────────────────┬────────────────────┐
│ ☰  Sessions        +  │ nexus · main             ● running ▐ │ Session Files MCP Logs
│                       │──────────────────────────────────────│────────────────────│
│ NEXUS                 │                                      │ ID   s_01J9…F3Q  ⧉ │
│ ● Fix the parser      │  ▌System prompt ~3.1K  ▌Tools ~9.8K  │ Agent  build       │
│   12 · 5m             │                                      │ Model  opus-5.5    │
│ ○ Release notes       │ ▾ Make the parser accept …      #12  │ Turns  12          │
│   3 · 2d              │                                      │                    │
│                       │    I'll look at the grammar first.   │                    │
│ OTHER                 │                                      │                    │
│ ○ Voice spike         │  ✓ 7  Edit nexus/parse/grammar.py    │                    │
│   40 · 1w             │                                      │                    │
│                       │ ┃                                    │                    │
│                       │ ┃  Type a message…                   │                    │
│                       │ ┃  Build · opus-5.5 · high  ╱╱╱╱ …   │                    │
│                       │  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  │                    │
└───────────────────────┴──────────────────────────────────────┴────────────────────┘
```

Rules:

- Sidebars span from row 0 to the last row. The centre column (breadcrumb,
  transcript, composer, activity line) sits between them.
- When the sessions sidebar is open, the tab row is removed: the sidebar *is*
  the session switcher. The sidebar header row carries the `☰` toggle and `+`
  (new session); the centre column's first row carries the breadcrumb, status
  and the details toggle `▐`.
- When the sessions sidebar is closed, the tab row returns at the top of the
  centre column (full width between any open right sidebar).
- One `│` border in `border_strong` between columns; no extra bands or blank
  separator rows.

**Open question (decide after screenshots):** whether the open tab set should
be marked inside the sessions sidebar (for example a dot or bold title for
sessions that have a tab), so nothing about the tab list is lost when it is
hidden. Recommendation: yes, a quiet `◦` in the gutter.

## 1. Bugs to fix first (P0)

### 1.1 Right sidebar opens a modal instead of a sidebar

Cause: the details shortcut (`l`), the `▐` button and the agent-page path in `rust/tui/src/main.rs`
fall back to `action("command", "/details")` (a modal panel) whenever
`width < 170` with the left sidebar open, or `< 110` without it. `regions()` in
`render.rs` applies the same thresholds and silently draws nothing.

Fix:

- The toggle always flips `details_sidebar`. `/details` stays as a command but
  is no longer the fallback for the button or shortcut.
- New width policy in `regions()`:
  - Both fit (centre column ≥ 60 cols after both sidebars): show both.
  - Only one fits: the sidebar opened most recently wins and the other is
    *collapsed for this width* (its preference is kept, so widening the
    terminal brings it back). Python needs no new state; Rust picks based on a
    `last_opened` field Rust already knows from the toggle it sent.
  - Terminal narrower than ~80 cols: the right sidebar draws as an overlay
    drawer on the right edge of the transcript (still a sidebar, not a centred
    modal), closed with Esc or the toggle.
- Hit-testing uses the same `Regions`; delete the duplicated threshold logic in
  `main.rs` (three copies today: key `l`, tab hit, agent page).

Files: `rust/tui/src/{render,main}.rs`.
Tests: `regions()` unit tests at 80, 120, 170 and 220 columns for each
sidebar combination; a regression test that the toggle never emits `/details`.

### 1.2 Composer colour does not follow the agent

Cause: `project()` (`prototype.py`, near the end) sets `agent_color` from
`shell.preview.agent` first and only falls back to the agent name. Shift+Tab
(`cycle_agent`, `prototype.py`) and the picker (`workflows.py`, `select_agent`)
change the agent but do not refresh `shell.preview`, so the old agent's colour
wins. `/agent <name>` does refresh, which is why it is inconsistent.

Fix:

- Derive the colour from the *current* agent. Use `preview.agent.color` only
  when `preview.agent.name == snapshot["agent"]`; otherwise use the agent
  definition's colour (from `list_agents`, which the picker already loads), and
  fall back to `context_header.agent_color(name)`.
- Every agent change path calls `shell.refresh_preview()` after
  `select_agent`. Put this in one helper (`ShellActions.select_agent`) and route
  the `/agent` command, `cycle_agent` and the picker through it.
- The context header blocks (`_context_blocks`) take their colour from the same
  resolved value, so header chips, composer bar, context bars and activity sweep
  always match.

Files: `nexus/ui/ratatui/{prototype,actions,workflows}.py`.
Tests: projection test that cycling build → orchestrator changes
`agent_color` and every `context:*` block colour in the same snapshot.

### 1.3 Empty row between the context line and the activity line

Cause: `composer_rows()` in `render.rs` reserves six rows; row 4 holds only
`update_notice`, which is usually empty.

Fix: remove the fixed row. The update notice moves to the breadcrumb row
(right side, accent colour, clickable to run `nexus update` help), or to a
transient notice. Composer height drops by one; adjust `composer_height`,
`composer_control_at` and the click rows in `main.rs` (`mouse.row ==
r.composer.bottom() - 1 / - 3`), which must come from `composer_rows()` rather
than fixed offsets.

### 1.4 User message chevron pushes the text

Today the user card is `" " + "│" + "  "` then text, with a separate corner row
holding `▼`. The chevron and the bar compete for the first cells, so the prompt
does not start in the same column as everything else.

Fix: drop the separate corner row. On the first text row, the chevron replaces
the bar glyph, one cell to the left of where it sits now:

```
▾ Make the parser accept trailing commas                         #12
│ and keep the old error for a lone comma.
```

- Gutter: col 0 is chevron (`▾` open, `▸` collapsed) on the first row and the
  blue `│` on the other rows; col 1 is a space; text starts at col 2 of the card
  (which is the transcript text column, see "One grid").
- Clicking the chevron cell or pressing Enter on the row toggles collapse.
- Wrapped rows and chips align to the same text column.

Note: confirm against a screenshot before and after; the exact current
offsets were read from `transcript.rs::user`, not from a live capture.

Files: `rust/tui/src/transcript.rs` (`user`), selection/hit-test helpers in
`render.rs`. Update `user_card_fills_width_and_right_aligns_number`.

## 2. Scroll lag and speed (P0)

The lag is not yet measured; the following are the likely causes found by
reading the code, in order of expected impact. Measure first (§2.1), then fix.

### 2.1 Measure

- Add `NEXUS_TUI_TRACE=1`: Rust records per-frame timings (snapshot parse,
  `update_content`, draw, flush) and per-event handling time into a bounded
  ring (last 2,000 frames), written to the log on exit and shown in the Logs
  tab (§5) as p50/p95/max.
- Add a Rust benchmark next to the existing `thousand_turns` test: 2,000 turns,
  wheel-scroll 300 events, while a turn streams at 50 snapshots/s.

### 2.2 Likely causes and fixes

1. **One full redraw per wheel event.** The main loop handles one event, then
   draws. A trackpad fling sends dozens of events. Fix: after `event::read()`,
   drain everything already queued with `event::poll(Duration::ZERO)` (cap 256),
   accumulate the scroll delta, then draw once.
2. **Whole-transcript work on every snapshot.** `Cache::update_content`
   compares `self.blocks == blocks` (deep equality over the whole transcript),
   clones all blocks, then rebuilds `self.lines` by copying every part's lines.
   While streaming this happens per snapshot, which is why lag appears
   "sometimes" (while the agent runs). Fix:
   - Python adds a cheap per-block `rev` (hash of the projected block) and the
     snapshot carries it. Rust compares ids and revs, not whole blocks.
   - Keep parts in a `Vec` with a prefix-sum of line offsets; on change, rebuild
     only the changed blocks (usually the tail) and recompute offsets from the
     first change.
   - The viewport draws by slicing parts through the offsets; stop materialising
     one flat `lines` vector.
3. **Full snapshot JSON for every token.** Each snapshot re-sends every block.
   Coalescing skips backlog, but each parsed snapshot is still the whole
   session. Fix (bridge schema 2): send `blocks_from: N` plus only blocks from
   index N, with a full resend on session switch or width-independent reset.
   Keep schema 1 parsing until both sides ship together.
4. **60 Hz animation while running.** `activity_frame` advances at 60 fps and
   marks the whole frame dirty, which competes with scrolling. Fix: 30 fps for
   the activity sweep, 8 Hz for spinners, and skip animation frames while input
   events are pending.
5. **Per-frame rebuilds of chrome.** `session_sidebar` and `details_rows` are
   rebuilt on every draw, and the scroll-down handler rebuilds `details_rows`
   just to clamp. Cache both keyed on `(revision, width, filter)`.
6. **Mouse handler recomputes layout.** Each mouse event calls
   `composer_height`, which wraps the whole draft. Keep the last drawn `Regions`
   and reuse them for hit-testing (they are also the truth for what was drawn).

Acceptance: see §9 budgets, verified with the trace on a real 2,000-turn
session and the benchmark.

## 3. Transcript: clean tool calls and grouping (P1)

### 3.1 Tool groups

Consecutive tool calls with no message between them form one **group**. A
group renders as one row:

```
  ✓ 7  Edit nexus/parse/grammar.py · +12 −3
  ⠹ 3  Bash  pytest -q tests/test_parse.py
  ✗ 4  Read nexus/parse/lexer.py · 1 failed
```

- Gutter (cols 0–3): status glyph (`⠹` running, `✓` done, `✗` any failure) and
  the tool count right-aligned in a 2-cell field. Count is muted; glyph uses
  success/error/agent colour.
- Text column: the **latest** tool's heading and one-line summary (the running
  one while running). A failed group adds `· N failed` in the error colour, so
  failures are never hidden by grouping.
- A single tool is a group of one and uses the same row shape (count omitted).
- Edit/write tools show `+a −r` in the summary; their diffs appear on expand.
- Subagent (`task`) calls end a group and keep their own row, because they open
  a page.

Expanded (Enter or click on the group row):

```
  ✓ 7  Edit nexus/parse/grammar.py · +12 −3                         ▾
       Read    nexus/parse/grammar.py · 412 lines
       Grep    "trailing" · nexus/parse/ · 9 matches
       Read    nexus/parse/lexer.py · 230 lines
     ▸ Bash    pytest -q tests/test_parse.py · exit 1 · 38 lines
       Edit    nexus/parse/grammar.py · +12 −3
```

- One row per tool: tool name in a fixed-width column (muted), then target and
  summary. No batch brackets (`┌│└`); parallel calls get a quiet `∥` in the
  gutter instead.
- Each tool row expands again to its **detail** (§3.2).

Projection: a pure helper `group_tools(turn) -> list[ToolGroup]` in
`nexus/ui_support/timeline.py` (counts, latest, failure count, members).
`prototype.py` emits a `kind: "tool_group"` block with `members` (already
formatted rows) and an `operation` of `{"kind": "block_toggle", "id": group_id}`.
Group ids are stable: `<turn id>:g<first call id>`. Expansion state lives in
`shell.expanded` (already bounded); a group that gains a new running tool keeps
its expansion state.

Settings: `/verbose` (existing) expands every group and every detail, matching
what the agent saw, for users who want everything open.

### 3.2 Tool detail

The current expanded detail mixes indents and glyphs. New shape: labelled
sections under a thin guide line in the gutter, same text column as the row.

```
     ▾ Bash    pytest -q tests/test_parse.py · exit 1 · 38 lines
       │ command   pytest -q tests/test_parse.py
       │ cwd       ~/repos/nexus
       │ timeout   120s
       │
       │ output
       │   FAILED tests/test_parse.py::test_trailing_comma - AssertionError
       │   …
       │   … 30 more lines · enter for all
```

- Keys from `tool_details.flatten` / `tool_detail_sections` (every parameter,
  labelled; no JSON dumps). Keys in a muted fixed column, values in text colour,
  wrapped with a hanging indent.
- Output folds after 12 lines with an announced count; Enter shows all.
- Diffs keep the existing split diff renderer, indented to the text column.
- The guide `│` uses `border`, not `border_strong`, so detail recedes.

### 3.3 Other transcript cleanup

- Assistant markdown: text column 4, no extra 4-space lead beyond the grid.
- Thought rows: `◇` in the gutter, title muted italic, collapsed to one line.
- Turn footer (`summary`): right-aligned, quiet, one row, no blank row before it
  when the last block is a message.
- Blank rows: exactly one between turns, one after a user card, one between a
  message and a tool group. Python owns these gaps (`gap`).

Files: `nexus/ui_support/timeline.py`, `nexus/ui/ratatui/prototype.py`,
`rust/tui/src/{bridge,transcript,render,main}.rs`.
Tests: Python grouping tests (consecutive, interrupted by message, failure in
middle, running tail, parallel batch, task ends group); Rust row-shape tests;
projection stability test (group id unchanged while tools are appended).

## 4. Composer context and activity line (P1)

### 4.1 Context bars on the controls row

The controls row becomes:

```
┃  Build · opus-5.5 anthropic · high            ╱╱╱╱╱╱╱╱╱╱  52K · 26% · 5.2%
```

- **Ten slanted bars** (`╱`). Filled bars use the agent colour; unfilled bars
  use `border`. Fill = `used / full window`, rounded up so any usage shows one
  bar.
- **Three numbers:** used tokens, then the percentage of each tier:
  - first percentage: `used / first price-tier boundary` (for example of 200K);
  - second percentage: `used / full context window` (for example of 1M).
  - With no tier boundary, show one percentage. With no reported window, show
    the used tokens only and draw the bars empty with `?` muted. Never invent a
    tier.
- Narrow widths drop, in order: provider, effort, second percentage, bars. The
  used-token figure is the last to go.
- The whole cluster is one click target that opens the context popover (§4.3);
  add a keyboard path for it next to the existing leader shortcuts.

Python projects `context_used`, `context_tiers: [boundary, window]` (only
reported values) and the formatted text; Rust only draws bars and aligns text.
Replace `_context_label` and `context_marks` usage accordingly.

### 4.2 Activity line

The last row is the activity line only:

- Idle: a quiet full-width `─` in `border`.
- Working: a sweep segment (1/5 width) in the agent colour that moves left to
  right and back, as today, at 30 fps.
- No context fill and no tier ticks on this line any more.
- Remove the `activity_meter` context branch and its test expectations.

### 4.3 Context popover

Clicking the context cluster opens a bounded popover anchored above the
composer (same drawer style as the agent picker), showing the full context:

```
 Context · as of 14:32:07                                   esc close
 Used          52,140 tokens
 Tier 1        26.1% of 200K   (input $3 / output $15 below 200K)
 Window         5.2% of 1M     (above 200K: $6 / $22.5)
 Last turn     in 4,120 · out 812 · cache read 41,900
 ─────────────────────────────────────────────────────────────
 System prompt   ~3.1K   Tools 24   ~9.8K   AGENTS.md ~2.2K
 Skills 6        ~1.4K   MCP 2      ~0.6K   History  ~35K
 enter  open full /context view
```

- Works whether or not a turn is running. The host refuses
  `inspect_context` while the session is active, so the popover shows the
  most recent successful preview with its timestamp (`as of HH:MM:SS`), plus
  live usage from the view. The popover says when the breakdown is from before
  the running turn.
- Keep the last successful preview on `ShellActions` (`preview_at` timestamp);
  never clear it to `None` on the "active" refusal as `refresh_preview` does
  today.
- Follow-up (not required for this plan): a host command that returns the
  context of the request in flight, so the breakdown can be current during a
  turn. Needs a protocol change; record in `docs/decisions.md` if taken.

Files: `nexus/ui/ratatui/{prototype,actions}.py`,
`nexus/ui_support/context.py`, `rust/tui/src/{bridge,render,main}.rs`.

## 5. Right sidebar with tabs (P1)

The right sidebar becomes a tabbed panel. Tab strip at its top row:

```
 Session  Files  MCP  Logs
 ───────
```

- **Session:** session id, title, agent, model, provider, effort, turns,
  tokens, cost, created/updated, workspace and branch. Labelled rows.
- **Files:** modified files with `+a −r`, expandable to inline diffs (today's
  "MODIFIED FILES" section).
- **MCP:** servers with status and tool counts.
- **Logs:** replaces the separate Logs drawer.
  - Header pins the identifiers needed to debug: **session id** (full, with a
    copy action `⧉` that uses `desktop.py` clipboard), daemon pid and socket,
    client/bridge schema, Nexus version, and the trace summary from §2.1 when
    enabled.
  - Below: the existing bounded log rows (problems always shown, routine rows
    folded behind the existing toggle), newest at the bottom, auto-follow.
  - The Logs tab polls only while visible (as `shell.logs.open` does now).
- Switching tabs: click, or `[` / `]` while the sidebar has focus. The chosen
  tab is a per-user preference (`preferences.py`), not session state.
- The current logs shortcuts (`Ctrl+E`, and `e` after the `Ctrl+X` leader)
  open the sidebar on the Logs tab instead of the dock.

Projection: `details_panel` gains `tab` and a `logs_header` list of
`(label, value)`; the separate `logs` field stays for the rows. Remove the
36-column logs dock (`Regions.logs`) and its fallback.

Files: `nexus/ui/ratatui/{prototype,logs,preferences}.py`,
`rust/tui/src/{bridge,render,main}.rs`, `rust/tui/src/render/chrome.rs`.

## 6. Full-height sidebars and the tab row (P1)

- `regions()` splits horizontally first (left | centre | right), then
  vertically inside the centre: optional tab row, breadcrumb row, transcript,
  composer, activity line.
- Left sidebar header row: `☰ Sessions … +`. Its list starts on row 2 and runs
  to the bottom row; session filter input sits at the bottom when filtering.
- Tab row shown only when the left sidebar is closed.
- Top bar background: one panel colour across the tab and breadcrumb rows, one
  divider under the breadcrumb, nothing else (keeps refinement plan §2).
- Mouse targets for the toggles move with the layout; `tab_cells` takes the
  centre column rect.

## 7. Visual polish checklist

- Palette: no new colours. Agent colour, `text`, `muted`, `quiet`, `border`,
  `border_strong`, `success`, `warning`, `error` only.
- Glyph set, used consistently: `▾ ▸` expand, `✓ ✗` result, braille spinner,
  `╱` context bars, `│` guides, `·` separator, `…` clipping.
- No bold except agent name, active session title and section labels.
- Every truncation ends in `…` and, where content is hidden, an explicit count.
- Light theme: check bars and sweep remain visible against `panel`.

## 8. Order of work

1. P0 bugs: 1.1 right sidebar, 1.2 agent colour, 1.3 empty row, 1.4 chevron.
   Small, independent; one PR.
2. Speed: 2.1 measurement, then 2.2 items 1, 4, 5, 6 (local to Rust), then 2
   (block revs) and 3 (bridge schema 2). Separate PR for the schema change.
3. Layout: §6 full-height sidebars and tab-row removal, with §5 tabs.
4. Composer: §4 context bars, activity line, popover.
5. Transcript: §3 tool groups and detail redesign.
6. Polish pass (§7) with screenshots at 80, 120 and 200 columns, both themes.

Each step updates `docs/ratatui-parity.md`, `plans/RATATUI_PLAN.md`, and
`docs/decisions.md` for: tool grouping, tab row removed with sidebar open,
right sidebar width policy, context popover "as of" semantics.

## 9. Performance budgets

Measured with `NEXUS_TUI_TRACE=1` on an M-series Mac, 200×50 terminal,
2,000-turn session:

| Operation | Budget |
| --- | --- |
| Wheel scroll, event to frame flushed | p95 ≤ 8 ms |
| Keypress in composer, event to frame | p95 ≤ 8 ms |
| Streaming snapshot, receive to frame | p95 ≤ 12 ms |
| Idle CPU (no turn running) | ~0% (no redraws) |
| CPU while streaming | ≤ 15% of one core |

If a budget fails, the trace output goes into `plans/RATATUI_PLAN.md` under
"Performance and distribution measurements" with the cause.

## 10. Verification

- `cargo test` in `rust/tui/` (layout, row shapes, hit-testing, benchmarks).
- `.venv/bin/python -m pytest -q tests/test_ratatui*.py` plus new projection
  tests for grouping, agent colour and context tiers.
- PTY check and screenshots per `.agents/skills/nexus-ratatui/references/testing-and-verification.md`:
  before/after captures for each section, at 80, 120 and 200 columns, both
  themes, with sidebars in every combination.
- Manual: switch build → orchestrator with Shift+Tab, picker and `/agent`, and
  confirm composer, bars, sweep and header change together; open the right
  sidebar at 120 columns with the left sidebar open; fling-scroll a long
  session while a turn streams.

## Not verified

- Slanted bars across all target terminals/fonts; dark/light xterm captures passed.
- Live-provider end-to-end latency and the exact attribution of remaining CPU.
  Trace measurements and the failed CPU budget are recorded in RATATUI_PLAN.md.
- That `╱` renders as a clean slanted bar in all target terminals and fonts;
  fallback glyph is `▰/▱`, selectable if a capture looks wrong.

### Redesign verification and remaining performance gap (2026-10-02)

The earlier measurements above are the baseline, before redesign implementation.
The new wire uses schema 2 suffix patches; Rust retains revision-keyed wrapped
parts and indexed rows, drains bounded input bursts, caches animation state and
buffers terminal output. The optional trace records parse, content update, draw,
flush, event handling and receive/input-to-frame latency.

Optimized controlling-PTY run, 200×50 terminal, 2,000 turns/6,000 blocks, 150
patches at 50/s, 300 wheels at 100/s, and 50 keys:

| Measurement | p50 | p95 | Maximum |
| --- | --- | --- | --- |
| Key → frame | 0.702 ms | 0.837 ms | 14.134 ms |
| Wheel → frame | 1.771 ms | 4.164 ms | 6.109 ms |
| Snapshot receive → frame | 3.625 ms | 5.654 ms | 38.661 ms |
| Draw | 0.482 ms | 3.544 ms | 22.513 ms |
| Content update | 0.000 ms | 3.168 ms | 22.178 ms |
| Flush | 0.370 ms | 0.561 ms | 0.694 ms |

All three p95 latency budgets pass. Idle CPU was 0.00% at process-time sampling
resolution over one second; streaming CPU was **22.98% of one core over 3.05 s**,
which fails the 15% budget. Before buffering and animation caching these were
4% idle and 58.41% streaming. Content-cache bookkeeping still walks the block
list on changed snapshots; its p95 accounts for most draw time. Its exact share
of CPU has not been profiled separately. Further CPU optimization remains open.
The release TestBackend frame benchmark reports p95 1.287 ms; it excludes terminal
flush and process overhead and must not substitute for the PTY measurement.

46 Rust tests pass (two manual benchmarks ignored), 100 native Python checks pass
including the controlling-PTY check, and 30 browser matrix captures were produced.
The full offline suite reported 5,063 passed/312 skipped with three failures:
a native notice wording failure was corrected and rerun; a timing-sensitive
Textual inspector test passed on rerun; the existing Textual mock tool-gutter
spacing assertion still fails independently and was left unchanged. Real-provider
streaming and slanted bars across every terminal/font are not verified.

Artifacts: `artifacts/ratatui-parity/native-2000-performance-final.txt`,
`native-2000-trace.log`, and `redesign-*.png` (ignored).
