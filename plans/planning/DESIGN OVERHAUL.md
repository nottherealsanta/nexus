# Design overhaul: the native Ratatui client

Status: steps 1–12, 14 and 15 are implemented. Verified now: `cargo test` (rust/tui) and
`cargo build --release` pass, the full Python suite and `ruff check nexus tests` pass (`cargo test` 557 + 38 passed;
pytest 6687 passed, 336 skipped, 6 deselected), and a PTY run with the splash on opened real sessions from the
dashboard, typed text and left `~/.nexus/tui-panic.log` empty. Animations-off and Monochrome were
reviewed for every step (ledger "Review pass"). Not verified: a PTY capture of queue
promote/restore with a live running turn, the GPUI desktop client, the remaining open decisions
(§2) and step 13.

Implementation ledger (review):
- Step 1: implemented; reviewed agent color/optional metrics and fixed names to
  ten Unicode display cells (including wide/combining text). The color test file
  exists as an untracked, non-ignored addition; include it when committing.
- Step 2: implemented; reviewed recording horizontal-rule layout, elapsed slot,
  and transcribing label. Focused Rust tests pass.
- Step 3: implemented: durable terminal timestamps, send/completion clocks,
  completion fallback, hour bands and running-clock spacing; replay fixtures and
  focused Python/Rust tests pass. PTY/plate and animations-off/monochrome manual
  review remain outstanding. Later steps are unchanged.
- Step 4: implemented in source (projection flag, `look::write_head`, builder and
  draw-loop cooling); written without a compiler or test run available: build and run
  the Rust and Python suites before relying on it. Differences from the plan: the glow is
  applied as the row is built (rows rebuild as text arrives), the draw loop only cools it.
- Step 5: implemented in source on the same terms (grouping, labels, `Working` tree,
  `· failed`, `transcript/fold.rs` fold transient). PTY capture of the fold and the
  visual review are outstanding; `animating()` stays snapshot-only and `main.rs` also
  keeps the clock running while `Cache::folding()`.
- Steps 6, 7, 11, 14 (STK-3 default): implemented in source with unit tests, **written
  without a shell, compiler or test run** (no `cargo`/`pytest` was executed): build and run
  `cargo test -p nexus-tui` and fix any compile slip before relying on them. PTY captures,
  plate comparison, animations-off and Monochrome review are outstanding. Differences:
  - 6: `∴` replaces the agent-coloured spinner on the status line. **Decode scramble REPLACED
    at the user's request by a grey shimmer** (light grey to dark grey, real text always shown):
    `render.rs::shimmer_spans` / `shimmer_greys`, driven by `cache.activity_frame` (30 Hz), 54-frame
    sweep, greys are the lighter/darker of `p.text` and `p.quiet`, `∴` in the dark grey, no purple.
    `decode` / `decode_marks` / `decode_spans` are deleted. Was: scrambled `p.purple` glyphs on `cache.spin`.
  - 7: **REVERTED at the user's request (bars and grow animation removed; the `total` row stays).** Was: `render/context.rs` bars, `total` row and `context::Anim` (`Cache.ctx`), session key
    is `Snapshot.composer_key`. `animating(&Snapshot)` cannot see it, so `main.rs` ORs
    `Cache.ctx.growing(..)` into the folding flag. A window change resets the row cache.
    `docs/context.md` got a pointer; the detail is in `docs/ratatui-parity.md`. The bridge's
    `Tokens::Count` makes `Content::token_label()` return "" (not changed here; the header
    reads `tokens.count()` first).
  - 11: `live_bar.rs` phase strip + `Mic` ring buffer (`Cache.mic`) + `captions`; the
    `mic`/`spk` meters and the `Live` title are gone. Word timing and the agent turn number are
    fallbacks (newest word; `agent running` without a `turn #N` row). ASCII fallbacks are local
    to the modules, not in `rust/widgets/src/glyphs.rs`. `tests/test_ratatui_pty_live.py`
    was updated to the new header text but not run.
  - 14: `sticky::indicator` / `sticky::above` (STK-3). N counts from the card's real last row
    (`offset - last - 1`), not `offset - first - rows`, which over-counts for a clipped long card.
    The STK pick itself is still open.
- Step 8: implemented and verified (`cargo test` 537 + 36 passed; focused pytest 346 passed;
  PTY capture of the dashboard). Sections Needs you / Working / Idle / Archived (collapsed,
  newest 20 via `ProjectSessionsList.archived`), `pending` {request_id, kind, summary, ts}
  on the row, `a`/`d` through `PermissionResolve` without opening, chips, blank row after
  `+ New Agent`, "Open Previous" and `resume_hit` removed, no composer (keys reach
  `cache.dashboard` first in `main.rs`). Fixed the doubled status glyph (`draw_at` inserted a
  glyph that `title_spans` already drew). Not exercised live: Needs you and Archived rows
  (no such sessions in the dev DB); covered by unit tests only. `ruff check` reports one
  unrelated F841 in `tests/test_ratatui_pty.py` (`down_up`).
- Step 9: implemented and verified (`cargo test` 540 + 36 passed; focused pytest 310 passed;
  PTY permission check in `tests/test_ratatui_pty.py` passes). Python:
  `nexus/ui/ratatui/permission_prompt.py` adds `title`, `since` (`PermissionView.ts`, which the
  reducer now sets to the request event time), `what` (targets, else preview; max 12 rows) and
  `why` (`default_rule`) beside `lines`. Rust: `dialogs.rs` `prompt_rows`/`prompt_body_lines`/
  `prompt_choice_line`, `prompt_area` clamps the body to 1..10 rows, `animating()` is true while
  a structured prompt is open. Differences: `what` shows 3 rows then `… N more` (4-row budget);
  `why` falls back to nothing (no `reason` field exists on `PermissionView`); chips stay one per
  row to keep click mapping. Not run: `test_tool_single_click_selects_double_click_toggles_locally`
  in the same PTY file fails (tool-row click, outside this step).
- Step 10: implemented in source, **never compiled or run** (no shell was available):
  `render/notifications.rs` (history of 50, counter text, drawer), `TabHit::Notifications`
  in `chrome.rs`, `/notifications` + `notifications_open_seq`. Differences: the chord is
  `Ctrl+X Shift+N` because `Ctrl+X n` is `/new`; the pop uses the text colour (no "peak"
  palette entry); the `◔` glyph's ASCII fallback (`o`) is local, not a `Glyphs` field. The
  PTY drawer step is added but unrun; the visual review is outstanding.
- Step 12: implemented in source on the same terms: `splash.rs` state machine, `draw_splash`
  delegating to it, curtain overlay on the first chat frame, input held until it ends.
  Differences: the five milestones are client-side observations (not host stages); particle
  stagger is relative to progress; `NEXUS_SPLASH=off` skips the motion (the pytest suite sets
  it in `tests/conftest.py`); the splash uses the dark palette. Now built and run. Fix pass:
  the clock is 8 ms ticks (was 80 ms steps, laggy), flash 160 ms, curtain 500 cells/s, and
  input is held only until the chat is on screen (it was held ~4 s after load, which is why
  typing "never worked"; it was not a stuck state). The "crash on opening a session" was a
  `"tokens": null` task card failing `Tokens` parsing (bridge.rs); a panic/error log
  (`~/.nexus/tui-panic.log`) was added.
- Step 15: implemented and verified (`cargo test` 544 + 36 passed, `cargo build --release` ok).
  Host: `SessionQueueSetMode` (upgrades only), durable `input.mode_changed` `{queued_id, mode,
  order}` reduced in `_on_input`, `SessionQueueRemove(return_content=True)`; interrupt cancels the
  turn with `drop_queue=False` and the promoted item runs next. Bridge: `queue_edit` ops
  `steer`/`interrupt`, steering items carry no `can_up`/`can_down`, steering removal restores via
  `restore` + `restore_if_empty` + `restore_note`. Rust: per-kind controls in `queue_buttons`
  (hit-testing shares the cells), `QueueFlash` transient in `Cache` (12 frames, `main.rs` ORs
  `queue_flash.live` into the animation flag). Differences: no keyboard focus on queue rows exists
  in the client, so controls show on hover only; the chip "pressed" colours are not drawn (no
  mouse-down tracking); restored text only, not attachments; queued items are not restored
  (`docs/decisions.md`). No PTY promote/restore capture was run.

- Step 15 gaps closed: keyboard focus on queue rows (`render::QueueUi`, `queue_key`; Tab/Down
  enter, Enter/`s` promote, Delete/Backspace/`x` remove, Alt+Up/Down reorder queued only, Esc
  leaves), pressed chips (`p.muted`, `✕` `p.error`, reversed in Monochrome; release sends), and
  attachments of a removed steering message restored with the text (`restore_attachments`).
- Dashboard open failure: `switch_project` toasts `Could not open the session in <ws>: <redacted
  reason>` (`host_support/errors.py`) and keeps the dashboard open; empty exception messages no
  longer vanish.
- Review pass (animations off / Monochrome / ASCII): fixed the voice preview scramble and the
  activity meter ignoring `animate`, spinners now rest on frame 0 when animations are off, the
  meter gets `━`/`=` lit cells and the dashboard selected row a focus bar in Monochrome, and
  ASCII twins for the dashboard status marks, chevrons, permission prompt marks and queue
  controls. `render::animations_off_tests` compares a busy screen at two ticks.

Source of truth for the look: `design-mockups/variations/index.html`, section
**Your picks** (plates `P-*`) and section **STK**. Each plate is a cell-accurate
mock drawn at the client's 80 ms tick in the Nexus palette. Where this plan and a
plate disagree, this plan wins (it records decisions made after the plate).

Scope: `rust/tui`, `rust/widgets`, the Python bridge `nexus/ui/ratatui/`, and the
shared helpers in `nexus/ui_support/`. The web client is not touched (it is being
deprecated). The GPUI desktop client is out of scope unless a step changes shared
Python output it also reads; such steps say so.

---

## 0. Ground rules (apply to every step)

1. **Motion budget.** All animation runs on the existing frame clock:
   `cache.spin` (spinner frames), `cache.activity_frame` (30 Hz motion clock),
   `nexus_widgets::anim::{wave, pulse, shimmer_spans}` and the live-span marker
   `transcript::look::live` / `look::animate_row`. Use only glyph swaps and colour
   blends; no sub-cell motion. Rows stay cached and tick-free; anything that moves
   is either a live span recoloured in the draw loop, or a short transient state
   kept in `render::Cache` with an `Instant`.
2. **Preferences.** `cache.animate` (Settings → Appearance) off means: draw the
   final state of every animation immediately. `cache.mono` (Monochrome) strips
   colour, so every state must also read from its glyph or texture.
3. **`animating()`** (`rust/tui/src/render.rs:426`) must return true while any new
   transient animation is in flight, or frames stop and the animation freezes.
   Every step that adds a transient adds its condition there, with a test.
4. **Host contract.** Any new data comes through `nexus/host/protocol.py` and
   `facade.py` (AGENTS.md rule 4). Anything that must survive a reconnect is in the
   durable log and the reducer (rule 5); purely visual state is client-only.
5. **Docs travel with code.** Each step updates `docs/ratatui-parity.md` and, when
   shared behaviour changes, `docs/surfaces.md`. New data fields go in
   `docs/events-and-view.md`; new commands in `docs/cli.md`; design reasons in
   `docs/decisions.md`. A new module gets a row in `docs/module-map.md`.
6. **Tests.** Rust unit tests sit in the module's own `#[cfg(test)] mod tests`.
   Python tests go next to their peers (`tests/test_ratatui_*.py`). Visual steps
   also get a PTY check where a PTY test for that area exists
   (`tests/test_ratatui_pty_*.py`). Per the memory note, run suites through a
   subagent.
7. **Glyphs.** Every new glyph needs an ASCII fallback in `rust/widgets/src/glyphs.rs`
   (`Glyphs::from_env`, `ascii`), e.g. `▰▱` → `#-`, `░` → `.`, `■` → `#`.

---

## 1. Decisions locked in review

| Area | Pick | Notes |
| --- | --- | --- |
| Splash | P-SPL | SPL-2 dots and SPL-5 bar **at the same time**, then SPL-7 curtain |
| Chat shell & layout | unchanged | SHL-0 stays |
| Thinking | THK-4 | ~~decode wave~~ grey shimmer, light to dark grey (user request) |
| Streaming text | STR-3 | glowing write head (not STR-4) |
| Tool groups | P-GRP | revised rules in step 5 |
| Sub-agents | P-AGT | two-line rows: capitalised name in a 10-char field, task from char 11; model-effort (tier) · tools · tokens · time; stable colour; agent-coloured spinner; no page redesign |
| Session dashboard | P-SES | "Needs you" section, spacing, no composer, no "Open Previous" |
| GPT Live | P-LIV | LIV-5 bar + LIV-3 captions, chip with no slide; pane stays above the composer |
| Voice dictation | unchanged + fix | red square and time as two marks on the activity rule |
| Permission | P-PRM | PRM-3 + PRM-2 timer, neutral key chips |
| Context header | P-CTX | ~~CTX-3 bars in grey textures, replays per session~~ reverted: no bars, no animation; `total` row kept |
| Composer | CMP-0 | unchanged |
| Toasts | P-TST | TST-4, opened by click, `/notifications`, or `Ctrl+X N` |
| Turn times | P-TURN | send time on the user card, completion time in the footer, `1h 4m` |
| Steering and queued | P-QUE | today's look and spacing, Steer / Interrupt buttons, arrows only on queued, cancelled steer returns to an empty composer |
| Pinned turn card | STK-? | **pending**, see step 14 |

## 2. Open decisions (defaults used until answered)

1. **Pinned-card indicator** (step 14): STK-1…5. Default if unanswered: STK-3
   (shade texture plus `↑ N lines of this turn above`).
2. **Failed call inside a grey tool group** (step 5): the review removed ✗ and red.
   AGENTS.md says nothing the agent can see may be hidden, so a failure needs some
   mark. Default: the member row ends in a quiet ` · failed` word, still grey.
3. **Duration on a finished group summary** (step 5): default keeps the quiet
   `· 4.6s`.

---

## Step 1. Voice dictation: red square and time on the activity rule

Plate: **P-VOX**.

Today: `render.rs:2345-2360` draws a blinking `■` inside the composer
(`rows[4]`), alternating `p.accent` / `p.quiet`. The recording time lives in
`cache` (`recording_since`, `main.rs:272`).

Change:
- Remove the `■` from the composer.
- In the activity rule (`activity_meter`, `render.rs:442`, drawn at
  `render.rs:1876` directly under the composer card), overlay two separate marks
  while `s.voice_phase == "recording"`:
  - cell 2: `■`, blinking every 4 spin frames between `p.error` and
    `mix(p.error, p.panel, 0.55)`. It is always red, never accent or grey.
  - cells 4-8: the elapsed time `mm:ss` in `p.error`.
  - exactly **one** rule cell between them; the rule (and its context fill or
    running sweep) keeps drawing in every other cell: `──■─00:07──────…`.
- Implement as a post-pass over the `Vec<Span>` the meter returns (split the
  spans at those cells), so the meter function stays pure and tested.
- While `transcribing`, show neither mark (the composer preview already says so).
- ASCII: `■` → `#`.

Tests (`render.rs`): replace `voice_preview_is_inline_with_only_a_recording_square`
(`render.rs:3338`) with: (a) no `■` inside the composer area; (b) rule row contains
`■` at cell 2 and the time at cells 4-8; (c) cell 3 is still a rule glyph; (d) both
blink colours are red-family (`p.error` or its mix). Extend
`activity_meter_sits_directly_under_the_composer_card` (`render.rs:3424`) to cover
the recording overlay.

Docs: `docs/voice.md` (indicator location), `docs/ratatui-parity.md`.

## Step 2. Sub-agents: stable colour and two-line rows

Plate: **P-AGT**. Sub-agents and root agents are being unified into sessions
(plans 01–03), so every session will look alike and the separate sub-agent page
goes away. Do **not** redesign that page or its footer.

Stable colour:
- Cause: `nexus/ui/ratatui/prototype.py:476` passes `child.type or child.id` to
  `_agent_color`. A child without a `type` hashes its run-specific id, so its
  colour changes on every run.
- Fix: key the colour on the agent's **definition name**: `child.type`, else the
  task tool's `subagent_type`/agent argument, else the literal `"task"`. Never the
  id. `agent_color` (`nexus/ui_support/context_header.py:29`) is already a fixed
  SHA-256 hash, and `build`/`orchestrator` keep their semantic colours. An explicit
  `color` in the agent definition still wins (`_agent_color`, `prototype.py:563`).
- Check every other `_agent_color(` call site (`prototype.py:576, 579, 877, 1064`)
  uses a name, not an id.

Row layout (two lines per agent, replacing today's one-line card at
`rust/tui/src/transcript.rs:803`):

```
   ⠋ Explorer  Map every parse_ call site
     gpt-6-mini-medium (low) · 4 tools · 10K tokens · 3.2s
```

- **Line 1:** the mark (running: the spinner, **in the agent's colour**, today it
  is `p.text`; done: `›` in `p.muted`; failed: `◆` in `p.error`, as today), one
  space, then the **agent name capitalised** (`explorer` → `Explorer`) in the agent
  colour, in a fixed **10-character field**: longer names are cut to 9 characters
  plus `…`; shorter ones are padded. The **task always starts at character 11** of
  that field, whatever the name's length, so tasks line up down the list. Task in
  `p.text` while running, `p.muted` when done; truncated with `…` to the width.
  (A 10-character name touches the task with no space, by design of the fixed
  column.)
- **Line 2**, indented to the name column, in `p.quiet`, parts joined by ` · `:
  1. `model-effort (tier)`: e.g. `gpt-6-mini-medium (low)`. Effort omitted when
     unknown (`gpt-6-mini (low)`); tier omitted when unknown.
  2. `N tools`: only when the count is provided (omit for external workers that do
     not report calls).
  3. `N tokens`: only when provided (omit when `usage_unknown`), compact
     (`842`, `6.4K`, `18K`, `1.2M`).
  4. time: **live while running** (`p.muted`, ticking), the **total when done**.
     Same format as turn durations (step 3): `3.2s`, `4m 40s`, `1h 4m`.
- One blank row between agents.
- Data (Python, the task block at `prototype.py:457-477`): send separate fields
  instead of the composed title: `agent_name` (definition name),
  `task` (the phrase, up to 64 chars, as today), `model`, `effort`, `tier` (full
  name, not the letter), `tool_calls` (int or null), `tokens` (int or null),
  `started_ms` (from `AgentView.spawned_ts`), `elapsed_ms` (from `completed_ts`
  when done). Keep `title` for older clients. `_agent_card_model`
  (`prototype.py:272`) and `_agent_card_metrics` (`prototype.py:288`) already
  compute these; split them into fields instead of strings.
- Rust computes the live time from `started_ms` and `look::now_ms()` like tool
  groups do (`look::live_elapsed`), and `animating()` is already true while a task
  row is running (spinner slot).
- External workers keep their provider note in the task text (`· Antigravity
  (external, own tools)`), truncated like any task.

Tests:
- Python: `tests/test_ratatui_agents*.py` (or a new `tests/test_ratatui_agent_color.py`):
  the same agent name gives the same colour across two runtimes and two sessions;
  a child with no type gets the colour of its definition name, not its id; the
  task block carries `agent_name`, `effort`, `tier`, `tool_calls`, `tokens`
  (`null` when unknown), `started_ms`, `elapsed_ms`.
- Rust (`transcript.rs`): name capitalised and cut to 9 + `…`; task starts at
  column 11 of the name field for names of length 3, 10 and 21; line 2 omits tools
  and tokens when absent; running spinner span has the agent colour; done row shows
  the total time, running row a live time.

Docs: `docs/agents.md` (identity colour rule), `docs/ratatui-parity.md`.

## Step 3. Turn times

Plate: **P-TURN**.

User card send time:
- Python builds the user block at `prototype.py:385` (`"kind": "user"`, `"number"`).
  Add `"sent_at": _clock(turn.user_ts)` (the same `%I:%M %p` short form as
  `timeline._turn_clock`, factored into a shared helper in `ui_support/timeline.py`).
- Rust `user_card` (`transcript.rs:213`, tags built at `transcript.rs:228-244`):
  after `#N`, append ` · 12:36 AM` when `sent_at` is non-empty. Order of tags:
  `via voice`/`steer`, then `#N · time`. Number quiet, time muted.
- Add `sent_at: String` to `Content` (`bridge.rs:356`, `#[serde(default)]`).

Footer completion time:
- `turn_footer_text` (`ui_support/timeline.py:736`) uses `_turn_clock`, which reads
  `user_ts`/`started_ts`. Change it to the **completion** time.
- `TurnView` (`nexus/view/model.py:345`) has `started_ts`, `updated_ts`, `user_ts`,
  `assistant_ts`, `elapsed_ms`, but no completion timestamp. Add
  `completed_ts: float | None = None`, set in `nexus/view/reduce.py` from the turn's
  terminal event (`turn.completed` / failed / cancelled), so replay reproduces it.
- Fallback for sessions recorded before the field: `started_ts + elapsed_ms/1000`.
  Neither present: no clock.

Duration format (`_turn_duration`, `timeline.py:685`):
- `< 60 s`: `11.6s` (unchanged); `< 60 min`: `11m 40s` (unchanged);
  `≥ 60 min`: **`1h 4m`**, hours and minutes only.
- The status line's `fmt_turn` (`render.rs:1034`) prints `4m40s` / `1h02m`. Align it
  to the same three bands and spacing so the running clock and the footer agree.
  The verb-group `format_time` (`verb_groups.py:35`, `look.rs:38`) gets the same
  hour band.

Tests:
- `tests/test_timeline*.py` (whichever covers `turn_footer_text`): the three duration
  bands (59.9 s, 11 m 40 s, 1 h 4 m 59 s → `1h 4m`); the footer shows completion
  time; the fallback path.
- Reducer: a replay test asserting `completed_ts` equals the terminal event's `ts`.
- Rust: user card renders `#2 · 12:36 AM`; absent `sent_at` renders `#2` alone.

Docs: `docs/events-and-view.md` (`TurnView.completed_ts`), `docs/ratatui-parity.md`.
The desktop client reads `turn_footer_text` too, so note it in `docs/surfaces.md`.

## Step 4. Streaming text: glowing write head

Plate: **P-STR** (STR-3).

- While an assistant message is streaming, its last 12 graphemes blend from
  `p.text` (12th from the end) to `p.accent` (the newest), linearly. When more text
  arrives, the glow moves with the head, so earlier characters cool to `p.text`.
- When the message completes, nothing glows. No cursor.
- Rows are cached, so the glow cannot be baked into the row. Mark the streaming
  block (Python already knows the streaming turn: `prototype.py:1147`,
  `stream_turn`; expose `"streaming": true` on that text block). In the draw loop,
  for the visible rows of a streaming block, recolour the last 12 graphemes of the
  **last non-empty row** (and spill into the row above when the last row is short).
  Put this in a small pure helper `transcript::look::write_head(line, count, base,
  peak)` with unit tests.
- Code blocks and tables inside the stream: apply only to prose rows. Code keeps
  its syntax colours.
- `cache.animate` off: no glow. `cache.mono`: no glow.

Tests: helper unit tests (blend endpoints, grapheme-safe on emoji/CJK); a render
test: a streaming block's last row ends in accent, a completed block has none.

## Step 5. Tool groups

Plate: **P-GRP**.

### 5a. Grouping rules (Python, `nexus/ui/ratatui/verb_groups.py`)
- Today `split_runs` (`verb_groups.py:44`) puts every tool and thought (except
  `task`/`subagent`) in one run.
- New rule: **change tools** (`edit`, `write`, `apply_patch`, plus `multiedit`,
  `notebook_edit` if present: define `CHANGE_TOOLS`) never share a run with
  non-change tools. A change tool ends the current non-change run and starts a
  change run; consecutive change tools extend it; any other tool or a thought ends
  it. A single change call stays its own one-member group (it already renders as
  its own row today; keep that row look when the group has one member).
- Thoughts: stay in non-change runs as today; they never join a change run.
- Group identity (`turn.id + ":activity:" + first member id`) is unchanged, so
  disclosure state survives.

### 5b. Labels (Python)
- `_label` (`verb_groups.py:79`) gains a compact form: when a group has **more than
  5** members, drop the nouns: `Read 4, Searched 2, Ran 1`. With 5 or fewer:
  `Read 3 files, Searched 1 pattern`. Python decides the string and sends it as
  `text`; Rust draws it as given.
- While running, the header label is the literal `Working`. Python sends
  `"running_label": "Working"` (replacing today's present-tense summary).

### 5c. Rendering (Rust, `rust/tui/src/transcript/groups.rs`)
- **Header while running:** `▾ Working`: chevron in `p.muted`, `Working` in normal
  weight, tagged `look::live` so it shimmers (`muted`→`text`). No count, no time, no
  spinner. Remove the elapsed clock from the running header (`header`,
  `groups.rs:170`).
- **Members while running:** shown even when the group is folded, as a tree:
  `├─ ` / `└─ ` in `p.border`, then `Verb target` in `p.muted`. **No icons, no ✓/✗,
  no status colour, no slide-in.** The running member's text is a live span (it
  shimmers). Show at most the newest **5** members. When there are more, one
  `… N earlier` row in `p.quiet` comes first (clipping is always announced).
  Python must therefore send `members` (bounded to the last 5, plus `hidden: N`)
  while running, not only when opened (`build_run`, `verb_groups.py:150`).
- **Failure (default, pending decision 2):** a failed member row ends in
  ` · failed` in `p.quiet`; no red. Remove the red `failed_members` rows under a
  folded header (`groups.rs` tail and `render.rs:48`'s darker red) for groups.
  Failures remain fully visible in the expanded member and via `i` inspect.
- **Fold on completion:** when a group goes from `running` to `completed`, its
  member rows disappear **one per frame from the bottom** (fast: at most ~5 frames),
  then the header **flashes** once: `mix(p.accent, p.muted, f)` over ~6 frames
  (≈0.5 s), then settles to `p.muted`. The header becomes
  `▸ <past summary>` in normal weight, plus the quiet ` · 4.6s` (pending decision 3).
- Transient state: `render::Cache` gets `group_folds: HashMap<String, (Instant,
  usize)>` (group id → completion time, member rows at completion). The draw loop
  truncates the cached rows of that group by elapsed frames, and recolours the
  header. Entries expire after 1 s. `animating()` returns true while any entry is
  live. Detection: compare the group's `status` with the previous snapshot's (keep
  a `HashMap<id, status>` in cache, bounded to visible groups).
- **Expanded by the user** (Enter): unchanged member boxes (`member`, `groups.rs:56`),
  but grey per the same rules.
- Update the module docstring (it promises "no spinner or animation").

Tests:
- Python `tests/test_ratatui_verb_groups.py`: change tools split runs; consecutive
  edits form one group; `Read 4, Searched 2, Ran 1` above 5 members; nouns kept at 5;
  running label is `Working`; running groups send the last 5 members and `hidden`.
- Rust `groups.rs`: running header text is exactly `▾ Working` with no digits;
  members have no `✓ ✗` glyphs and no `p.error`/`p.success` colours; 7 members show
  `… 2 earlier` plus 5 rows; failed member ends in `· failed`.
- Rust `render.rs`: fold transient truncates rows frame by frame and expires;
  `animating()` true during the fold.
- PTY: extend the existing tool-group PTY check to capture the fold.

Docs: `docs/ratatui-parity.md`, `docs/surfaces.md` (grouping rule is shared
projection), `docs/decisions.md` (why edits are grouped apart: changes are what a
reader audits; they must not hide inside a read run).

## Step 6. Thinking line: decode (superseded: grey shimmer, see the ledger entry for step 6)

Plate: **P-THK** (THK-4).

- The status line (`draw_status_line`, `render.rs:1097`) today: agent-coloured
  spinner, `activity…` in `p.quiet`, right side turn clock and tokens.
- New left side: `∴` in `p.purple`, then the activity word (e.g. `reasoning`,
  `Waiting for response` → keep the host's word) where a wave of scrambled glyphs
  sweeps left to right: every 36 frames a window of 5 graphemes moves across the
  word, each grapheme inside the window replaced by a pseudo-random glyph from a
  fixed set (`A–Z` lowercase plus `#%&*+=<>?`), deterministic from `(tick, index)`.
  Outside the window the real text shows in `p.text`.
- The right side (`4m 40s ↓121k [stop]`) is unchanged; the `[stop]` hit test
  (`stop_at`, `render.rs:1060`) keeps its geometry (the left label width must not
  change length: the scramble replaces graphemes 1:1).
- `cache.animate` off: plain word, no scramble. Mono: unaffected (glyph-based).

Tests: pure helper `decode(word, tick)` returns the same width as `word`, equals the
word outside the window, and is deterministic; `stop_at` still hits `[stop]`.

## Step 7. Context header: grey texture bars, replay per session

**REVERTED.** The user asked for the bars and the grow animation to be removed after review; only the `total` row remains (see `docs/decisions.md`). The text below is kept as history.

Plate: **P-CTX**.

- `render/context.rs:107` `build` draws each closed block as title, counts and
  tokens. Add a bar column: width up to 28 cells, proportional to the block's
  tokens against the largest block.
- No hue. Each block gets a texture by position: `█` `▓` `▒` `░` `▚` (sixth onward
  repeat), in `p.muted` (the fifth in `p.quiet`). Inactive (zero-token) blocks draw
  no bar. ASCII: `#`, `%`, `+`, `.`, `:`.
- A total row under the blocks: `total  17,580 tok · 8.8% of the window`
  (`window` from the snapshot's context window when known; omit the % otherwise).
- **Grow animation:** bars grow from 0 with `ease(1-(1-p)^3)` over ~18 frames,
  staggered 4 frames per row; the token figure counts up in step. It replays
  **every time a session opens** (new, resumed or switched to), not on redraws of
  the same session. Key: the snapshot's session id (`bridge.rs:492`,
  `s.session`); store `ctx_anim: Option<(String, Instant)>` in `Cache`; restart when
  the id changes. `animating()` true while it runs.
- Rows are cached: draw bars at full size in the cached row, and let the draw loop
  scale bar spans (and figures) for the context-header rows while the transient is
  live. Alternatively bypass the cache for these ≤6 rows during the animation.

Tests: texture order and widths; zero-token block has no bar; total row; animation
restarts on session id change and not on a same-session snapshot.

Docs: `docs/context.md` (header presentation), `docs/ratatui-parity.md`.

## Step 8. Session dashboard

Plate: **P-SES**.

Today (`rust/tui/src/render/sessions.rs:168` `draw`; sections from
`picker::section`, `picker.rs:67`, by time bucket or by project):

- **Sections by status**, in this order, each a collapsible group like today's
  headings: **Needs you** (`input`), **Working** (`working`), **Idle** (`idle` and
  `done`; `done` keeps its `●` "finished since viewed" glyph), **Archived**.
  Within a section, keep today's order (newest user prompt first, as in
  `session_groups._order_time`).
- The existing "by project" mode (`state.by_project`) stays: when on, project groups
  apply inside each status section. Not verified whether that reads well; check in
  the PTY capture and drop project sub-headings if it is noisy.
- **Archived:** the dashboard rows today exclude archived sessions (archived is a
  separate query, `picker.rs:528`). Add archived rows to the dashboard payload,
  bounded (newest 20), collapsed by default. This is a host change: add an
  `include_archived`/`archived` slice to the dashboard command in
  `nexus/host/protocol.py` and `facade.py`.
- **Needs you rows** add two lines under the title: the pending question
  (`Allow Bash: git push origin main`) with `waiting mm:ss`, and the keys
  `Enter open  a allow once  d deny`.
  - Data: the dashboard row needs `pending` = {request_id, kind, summary, ts}.
    `PermissionView` has `ts`, `reason`, `targets` (`nexus/view/model.py:270-296`).
    The session summary carries only `state` (`awaiting_permission`), not the
    request itself, so add `pending` to the summary the dashboard reads (host
    change, bounded text).
  - `a`/`d` answer the permission without opening the session, through the
    existing `PermissionResolve(session, request_id, decision)`
    (`nexus/host/protocol.py:414`), which already names the session. The row
    needs the pending `request_id`.
- **Header chips** (`status_chips`, `sessions.rs:117`) in the same order:
  `◆ N needs you | ⠋ N working | ◇ N idle | ● N archived`.
- **Spacing:** one blank row between the `+ New Agent` line and the first section.
- **Remove** "Open Previous /resume" and "Open Previous" (`sessions.rs:261-266`) and
  `state.resume_hit`. Keep `Worktree Ctrl+w`. `/resume` stays a chat command.
- **No composer** on the dashboard: `regions` (`render.rs:1268`) gives the composer
  zero height when `s.dashboard.is_some()`. Typing still filters (the dashboard's
  own filter), so make sure keys reach `cache.dashboard` and not the draft.
  Keys already reach the dashboard first when it owns them
  (`cache.dashboard.owns` / `.key`, `main.rs:851-855`); confirm typed filter text
  takes that path, not the draft, before removing the composer.

Tests: `tests/test_ratatui_dashboard.py`, `tests/test_session_dashboard_order.py`,
Rust `sessions.rs` tests (update `sessions.rs:681`, which asserts "Open Previous"):
section order and names; Needs you rows show the question, wait and keys; blank row
after New Agent; no composer rows; chips order.

Docs: `docs/ratatui-parity.md`, `docs/sessions.md` (dashboard), `docs/host.md`
for the new commands.

## Step 9. Permission prompt

Plate: **P-PRM**.

Today: Python sends `{"kind":"permission","lines": labelled(...),"choices":[...]}`
(`prototype.py:945`); Rust draws plain lines and choices in the bottom panel
(`render/dialogs.rs:648` `prompt_area`).

- Send structured fields alongside `lines` (keep `lines` for older clients):
  `title` (`Allow <tool> <target>?`), `since` (`PermissionView.ts`),
  `what` (list of rows: the targets with their kind, e.g. paths to be written, the
  command and cwd), `why` (`reason` and `default_rule`).
- Layout:
  1. `◆ Allow git push origin main?` (`◆` warn, text bold) then
     `   waiting on you · mm:ss` in `p.muted`, counting up from `since`.
  2. blank
  3. **What happens** (muted bold) and its rows. For a known tool, a short plain
     description. Commit lists etc. are not available in general; show only what
     the request carries. Not verified: whether any tool supplies a richer preview.
  4. **Why it asked** (muted bold) `rule: <default_rule or reason>`.
  5. blank, then key chips: ` y ` once, ` s ` session, ` a ` always for this
     pattern, ` n ` deny: **all neutral**, bold `p.text` on `p.element`, label in
     `p.muted`. No colours. Keys come from `choices[].key`; only show `a` when the
     choice exists.
- The timer needs redraws: `animating()` true while a permission prompt is open
  (1 Hz is enough; reuse the frame clock).
- `prompt_area` height grows to fit the new rows, bounded (`clamp(1, 10)`); excess
  `what` rows clip with `… N more`.

Tests: Python: structured fields present, `since` from `PermissionView.ts`; Rust:
title row, timer text from a fake `now`, neutral chip colours (no `p.success`,
`p.error`, `p.blue`, `p.purple` on chips), `a` chip absent without its choice.
PTY: the existing permission check.

Docs: `docs/tools.md` (what the prompt shows), `docs/ratatui-parity.md`.

## Step 10. Toasts and the notifications drawer

Plate: **P-TST**.

- **Counter** in the top bar (`render/chrome.rs`): `◔ N` in `p.warn` when N > 0,
  `p.quiet` otherwise; on change it pops for ~4 frames as `( ◔ N )` in peak colour,
  bold. N = notifications since the drawer was last opened.
- **History:** toasts are sent by the host as the newest bounded list
  (`render/toasts.rs` header comment). Keep a client-side history of shown toasts
  (bounded, 50, newest first, with level, text, time). It need not survive a
  reconnect (not durable state). Background events that are not toasts today
  (subagent finished, session needs you, tests passed) are out of scope unless
  already toasts.
- **Drawer:** a panel anchored under the top bar's right edge, ~46 cells wide,
  listing history rows `glyph text  age`, header `Notifications  N · Esc closes`.
  Opens with a 2-frame-per-row drop. Esc or a click outside closes it. Opening
  resets N.
- **Three ways to open:**
  1. Click on the counter: record its rect in `Cache` like the other top-bar buttons
     and handle the click in `main.rs`.
  2. `/notifications` chat command: add `CommandSpec("/notifications", "Show recent
     notifications")` to `nexus/ui/cli/commands.py`; the Ratatui bridge maps it to a
     local UI action (no host round-trip needed since the history is client-side).
  3. `Ctrl+X N` (leader key), as in the older mockups.

Tests: Rust: counter pop transient; drawer rows bounded at 50; click rect hit;
Esc closes. Python: `/notifications` parses and maps to the UI action.
`tests/test_ratatui_toasts.py`, `tests/test_ratatui_pty_toasts.py`.

Docs: `docs/cli.md` (new command), `docs/ratatui-parity.md`.

## Step 11. GPT Live: phase strip and captions

Plate: **P-LIV**. The pane keeps its place **directly above the composer**
(`live_bar::pane_rows`, `PANE_ROWS = 6`).

Header row (replaces `● Live · phase · mm:ss  mic ▰▰▱ spk ▱▱`, `live_bar.rs:84`):
- On `p.element`: phase in caps, bold, padded to 12 (`CONNECTING` muted,
  `LISTENING` blue, `WORKING` purple, `SPEAKING` accent, `MUTED`/`PAUSED` quiet,
  `ENDED` quiet), then the phase animation, clock right-aligned. Keep `⤢ expand`
  and `↑ N more` on the right as today.
- Phase animations:
  - connecting: `●○○ ●●○ ●●● ○●● ○○● ○○○`, one step per 2 frames.
  - listening: 8 cells of `▁…█`, blue, the newest mic levels. Needs a mic level
    history: keep a ring buffer of `level_in` samples in `Cache` (the snapshot only
    carries the current `level_in`, `bridge.rs:342`).
  - working: braille orbit spinner `⣾⣽⣻⢿⡿⣟⣯⣷` purple, then
    `agent turn #N running` (turn number from the live detail rows; not verified it
    is present: fall back to `agent running`).
  - speaking: `∿` × 8, each cell blended accent→background by
    `wave(tick, i, 8, 0.4)`, damped by `level_out`.
  - muted / paused: static, no animation (`paused` shows `pause_left`).

Pane rows (captions; from `LiveBar.detail`, `tone TAB label TAB text`):
- `you` rows: partial text italic `p.muted`; final text `p.text`.
- `Sent to agent` rows become a chip: ` → sent to agent ` (bold, `p.background` on
  `p.warn`) then ` turn #N · steer ` (`p.text` on `p.element`). It appears in
  place, **no slide**.
- `voice` rows: word by word: spoken words `p.text`, the current word `p.accent`
  bold underlined, upcoming words `p.quiet`. Word timing needs playback progress.
  Not verified the host sends any; fallback: the words in the row are the ones
  already spoken (current word = last word of the newest delta).
- `Started`/instruction rows keep today's quiet form.

Tests: Rust `live_bar.rs`: header per phase (text, colours, width bounded at 40
columns); listening meter reads the ring buffer; chip has no offset over frames;
voice row colours current word. `tests/test_ratatui_live.py`,
`tests/test_ratatui_pty_live.py`.

Docs: `docs/live.md`, `docs/ratatui-parity.md`.

## Step 12. Splash

Plate: **P-SPL**.

Today: `draw_splash` (`render.rs:1374`) draws `Nexus` in the accent at the centre
until the first snapshot (`main.rs:298`, `loaded`; drawn at `main.rs:615`).

New sequence (one function `draw_splash(frame, &SplashState)` plus a small
`splash.rs` module holding the state machine; add it to `docs/module-map.md`):
1. **Converge and load, together.** The small half-block wordmark
   (`█▄ █ █▀▀ ▀▄▀ █ █ █▀` / `█ ▀█ ██▄ █ █ █▄█ ▄█`) is the target. Each of its
   non-blank cells is a particle starting at a deterministic pseudo-random cell
   (seeded hash of its index) and easing (`1-(1-p)^3`) to its target, with a
   staggered delay ≤ 16 frames; in flight it draws `·` then `•`, blending
   `quiet`→`accent`. **At the same time** a 30-segment `▰▱` bar with a percentage
   and the current step name fills under the wordmark.
   - The bar is driven by real startup milestones, not a timer: `daemon`,
     `workspace`, `providers`, `session`, `ready`. Particles' progress follows the bar
     (they arrive when the bar completes), with a minimum duration (~0.6 s) so a
     fast start still shows the motion, and no maximum.
   - The bridge reports no startup milestones today: `main.rs` only knows
     "no snapshot yet" (`loaded`, `main.rs:298`). Emit local progress events from
     the client (bridge process spawned, daemon connected, first line received,
     first snapshot) and map them to the five steps. No host change is required.
2. **Flash.** When loading completes, the wordmark flashes once
   (`mix(accent, peak, f)`, ~8 frames).
3. **Curtain.** On the first snapshot, a `▓▒░` curtain (3 cells wide, accent) wipes
   left to right at ~4 cells per frame from the splash into the first chat frame.
   Draw the chat frame into a buffer and copy cells left of the curtain. Input
   stays held until the wipe finishes.
- `cache.animate` off or reduced motion: draw the wordmark and bar statically,
  then cut straight to the chat.
- ASCII glyph set: `#` wordmark cells, `.`/`o` particles, `#-` bar, `#=-` curtain.

Tests: `splash.rs` state machine with a fake clock (particles reach targets when
progress = 1; bar text per progress; curtain column over time; final frame equals
chat buffer); `draw_splash` renders within any size ≥ 1×1 without panic.

Docs: `docs/ratatui-parity.md`, `docs/decisions.md` (why progress-driven, not
timed).

## Step 13. Composer and shell

No changes (CMP-0, SHL-0). Listed so reviewers know they were considered.

## Step 14. Pinned turn card indicator (pending STK pick)

Plates: **STK-0…5**.

Today: `sticky.rs` pins the user card while its turn is on screen
(`select`, `sticky.rs:35`; `MAX_ROWS = 3`). The pinned copy's bottom padding row
gets a `·` every fourth column in `p.border` (`continuation_padding`,
`sticky.rs:89`, used at `render.rs:1643`), which is too faint to notice.

The real card keeps **one raised padding row above and one below** its text. Only
the pinned copy's bottom padding row changes, into the chosen indicator:
- STK-1 shade: `░` across, `p.border_strong` on `p.element`.
- STK-2 torn edge: `▀▚` repeated in `p.element` on the background.
- STK-3 shade and count (default): `░` with a right-aligned
  ` ↑ N lines of this turn above ` in `p.muted`.
- STK-4 position rule: `━` filled to (rows above / turn rows) in `p.muted`, the rest
  `─` in `p.border`.
- STK-5 dashed fold: ` ╌╌ N lines above ╌╌…` plus ` Home turn start` (adds a key:
  Home scrolls to the card; not bound today, verify no conflict).

`N` = rows of this turn above the viewport = `offset - pin.first - pin.rows`
(clamped ≥ 0); turn length for STK-4 comes from the next card's top. Both are
already known where `select` runs.

Tests (`sticky.rs`): the indicator row replaces only the bottom padding; the top
padding row is unchanged; N is correct at several offsets; widths 20–200.

## Step 15. Steering and queued messages

Plate: **P-QUE**.

Today: `draw_above_card` (`rust/tui/src/render.rs:1155`) lists waiting messages
under `▾ Steering N` / `▾ Queued N` headers with no padding, directly above the
composer. Hovering any row shows `↑ ↓ ✕` (`queue_controls`, `render.rs:978`;
click mapping `queue_control_at`, `render.rs:993`). Python sends `queue_items`
(`_queue_items`, `nexus/ui/ratatui/prototype.py:772`) with `tag` `S`/`Q`/`I`,
`can_up`/`can_down`, and the text **cut to 160 characters**. Actions go through
`queue_edit` (`prototype.py:1513`) to the host's `SessionQueueMove` and
`SessionQueueRemove` (`nexus/host/protocol.py:332-345`). The host cannot change a
waiting message's mode.

### 15a. Look
- **Keep today's layout and spacing.** No background on the sections, no padding
  rows inside them, and no blank row between Steering and Queued: Queued's header
  follows the last steering row directly. The only spacing is the existing
  combined margin: one blank row between the status line and the list, and the
  blank margin row before the composer (`above_card_rows`, `render.rs:1237`).
- Header and item rows are unchanged: `▾ Steering N` (`p.accent`, bold) /
  `▾ Queued N` (`p.muted`, bold), then `#n text`.
- Controls show on the hovered row (row background `p.element`, as today) and on
  the row with keyboard focus, right-aligned:
  - **Queued:** `↑ ↓  [Steer]  ✕`. The arrows keep today's `can_up`/`can_down`
    rule (a disabled move is blank).
  - **Steering (`S`):** `[Interrupt]  ✕`. **No arrows.**
  - **Interrupt (`I`)**, briefly pending: `✕` only.
  - Buttons are neutral chips (bold `p.text` on `p.element_hi`; pressed: background
    `p.muted`). `✕` is `p.muted`, `p.error` while pressed. No other colours.
- The controls get wider (`Steer`, `Interrupt` chips), so the text truncates
  earlier on the hovered row only; `queue_controls` cells move accordingly.
- A promoted message flashes once in its new section (row background
  `mix(p.background, p.accent, 0.22)` fading over ~12 frames); `animating()` covers it.

### 15b. Reordering only for queued
- Python: `_queue_items` sends `can_up = can_down = false` for steer and interrupt
  items. The host still accepts `SessionQueueMove` for any item (other clients),
  but no Ratatui control sends it for steering.
- Rust: `queue_controls` builds the cell list per kind (queued: up, down, steer,
  remove; steering: interrupt, remove; interrupt: remove), so hit-testing matches
  the drawn buttons exactly.

### 15c. Promote queued → steer, steer → interrupt (host)
- New command `SessionQueueSetMode(session, queued_id, mode)` in
  `nexus/host/protocol.py`, handled in `facade.py` next to `move_queued`
  (`facade.py:713`), client method `queue_set_mode` in `nexus/client/protocol.py`.
  Only upgrades are allowed: `queue → steer`, `steer → interrupt` (and
  `queue → interrupt`); anything else returns false.
- Durable: a new event `input.mode_changed` `{queued_id, mode}` appended by the
  session (`session.py`, beside `move_queued`), added to `INPUT_EVENTS`
  (`nexus/events.py:109`) and reduced in `nexus/view/reduce.py` (`_on_input`), so
  replay reproduces the new mode (rule 5).
- Position: a message promoted to steer moves to the **end of the steering group**
  (after the last waiting steer, before any queued message) in the same event, so
  steers stay consumed in the order shown. The facade then calls
  `supervisor.reorder(session_id, handle.queued_ids)`.
- Interrupt: after the durable mode change, reuse the submit path's interrupt
  behaviour (`facade.py:695-708`): `cancel(session_id, reason="Interrupted by user
  message", drop_queue=False)`, then give the item priority in the supervisor so it
  starts the next turn. The supervisor's `reorder(session_id, order)`
  (`nexus/host/supervisor.py:178`) follows the durable order, so the session
  moves the interrupted item to the head of its queue in the same event and the
  facade calls `reorder`. No new supervisor method is needed.
- Bridge: `queue_edit` (`prototype.py:1513`) accepts ops `steer` and `interrupt`
  and calls `queue_set_mode`.

### 15d. Cancelled steer message returns to the composer
- When `✕` removes a **steering** item (`S` or `I`) and the composer draft is
  empty (no text and no attachments), the message's full text becomes the draft,
  cursor at the end, and the status row briefly reads `restored from steer #n`.
- If the draft is not empty, the draft is left alone and the message is removed
  as today. A toast says `Steer message removed` (with the text's first words) so
  nothing disappears silently.
- Queued items (`Q`) are removed as today, not restored. (Only steering was asked
  for; say so in `docs/decisions.md`.)
- The full text is needed, because `queue_items` text is cut at 160 characters.
  Extend `SessionQueueRemove` with `return_content: bool = False`, returning the
  removed message's content blocks (text and attachment references), the way
  `SessionCancel.return_queue` already recovers queued text (`protocol.py:325`).
  The bridge hands the text to Rust through the existing one-shot restore
  channel: `shell.composer_restore` (`nexus/ui/ratatui/actions.py:412`) → snapshot
  `restore` (`prototype.py:1043`) → `main.rs:437`. That path today **prepends** the
  text to a non-empty draft. Add a `restore_if_empty: bool` beside `restore`;
  when set, Rust applies the text only if the draft is still empty when it
  arrives, else drops it and shows the toast above. Attachments in the removed
  message: restore them as draft attachments when the draft is empty (the same
  rule), since `return_content` returns them.

### Tests
- Python: `_queue_items` steering items have no `can_up`/`can_down`;
  `queue_edit` maps `steer`/`interrupt`; the bridge restores content only for
  steering removals and only into an empty draft.
- Host: `tests/test_host_facade.py`: `SessionQueueSetMode` upgrades only, reorders
  a promoted steer to the end of the steering group, an interrupt cancels the
  running turn without dropping the queue and runs that message next;
  `SessionQueueRemove(return_content=True)` returns the full content.
- Reducer: replay of `input.mode_changed` gives the same queue as live.
- Rust (`render.rs`): rows needed are unchanged from today; steering rows
  draw `Interrupt ✕` and no arrows; queued rows draw `↑ ↓ Steer ✕`; hit-testing
  matches drawn cells (update the tests at `render.rs:4052-4081`); flash transient.
- PTY: extend the queue PTY check (if present) with promote and restore.

Docs: `docs/loop.md` (steer, queue and interrupt semantics, mode upgrades),
`docs/host.md` (new command and the `return_content` flag),
`docs/events-and-view.md` (`input.mode_changed`), `docs/ratatui-parity.md`.

---

## Order of work

| Order | Step | Why here |
| --- | --- | --- |
| 1 | 1 Voice red square | tiny, isolated |
| 2 | 2 Agent colour + rows | small; fixes a real bug |
| 3 | 3 Turn times | small; adds a reducer field other steps rely on for times |
| 4 | 5 Tool groups | largest visible change; Python + Rust |
| 5 | 4 Streaming glow | transcript, shares the live-span machinery with 5 |
| 6 | 6 Thinking decode | status line |
| 7 | 14 Pinned card | after the STK pick |
| 8 | 7 Context header | |
| 9 | 9 Permission | needs structured payload |
| 10 | 8 Dashboard | needs host commands (archived slice, remote permission) |
| 11 | 10 Notifications | needs a new chat command |
| 12 | 15 Steering and queued | host command + durable event; independent, can move earlier |
| 13 | 11 GPT Live | needs mic history, word timing fallback |
| 14 | 12 Splash | last: depends on startup milestones |

Host-side work by step: 3 (`completed_ts` in the reducer), 8 (summary `pending`,
archived slice), 9 (structured permission fields), 15 (`SessionQueueSetMode`,
`input.mode_changed`, `return_content`, `restore_if_empty`). Steps 11 and 12 are
client-only with fallbacks. Ship the client half first where a fallback exists,
and the host half in the same PR when it is small.

## Verification for each PR

1. `.venv/bin/python -m pytest -q` for the touched areas (through a subagent).
2. `cargo test -p nexus-tui` and `cargo test -p nexus-widgets` in `rust/`.
3. `ruff check nexus tests`.
4. A PTY capture (the existing `tests/test_ratatui_pty_*.py` for the area), and a
   side-by-side look against the matching `P-*` plate in
   `design-mockups/variations/index.html`.
5. Run with Settings → Appearance animations off, and with Monochrome on: every
   state still readable.
