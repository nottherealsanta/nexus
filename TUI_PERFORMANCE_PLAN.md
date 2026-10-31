# Plan: TUI render performance

Status: phases 1–3 implemented 2026-10-31 (results below); phase 4 is deferred
until a larger reported workload (measurement in that section). The goal is to
remove the TUI's stream backlog and long-session frame cost
**without changing timeline content or interaction behavior**. Coalescing
intentionally skips intermediate stream frames. At equivalent render boundaries,
with the same clock and animation phase, buffers, cursor and interaction metadata
must match. Rendering currently mutates view state, so this needs testing rather
than assuming that fewer draws are behaviorally neutral.

## Why

### Reported baseline (measured 2026-10-31)

These are the original investigation’s measurements, not independently reproduced
during this review. Instrumentation was reverted; retain them as observations,
not reproducible acceptance results. The implementation must record the commit,
hardware, build profile, fixture/seed, command and raw results of its new run.

The release binary was driven in a pty against a scratch Core (`mock/stress`),
with temporary instrumentation in `src/main.rs` (since reverted). A second
session supplied long history. All numbers are on an M-series Mac, 160×48.

| Measurement | Result |
| --- | --- |
| Frames for 2,370 streamed events (200-message session) | **2,507 renders** — one full rebuild per event |
| Per-frame cost, that session | 5.15 ms mean, 6.0 ms p95, 13.3 ms max |
| Events per streamed chunk | **2** durable events (`wire_event` + `text_delta`); `wire_request`/`wire_response` per step |
| Stream filtering | none: the TUI receives every durable row, for every session |
| Frame cost vs. history (synthetic) | 20 msgs 0.74 ms; 100 1.91 ms; 200 4.01 ms; 200 × 1.4 KB 12.5 ms; 400 × 1.4 KB 30.9 ms |
| Live heavy session, as history loaded | 110 ms at ~400 messages → 248 ms at ~600 → **~500 ms at 1,000** |
| Heavy run, stream progress | ~38 events processed in 17 s (~2/s) while Core produced ~180/s → display minutes behind |
| Debug build, 200-message session | 35.7 ms mean, 42.4 ms p95, 74.6 ms max (release: 4.0/4.3/13.4) |

The heavy session grew from 200 to 1,000 messages **during the turn**: `tick()`
loads older pages automatically while a session is busy (`app.rs`,
`auto_older`), so the frame cost peaks exactly when the stream is live.

### Root cause

1. `render()` calls `timeline::build()` unconditionally. That walks every
   loaded message on every frame: `pulldown-cmark` parse, `text::wrap`, tool
   argument JSON, diffs, Mermaid. Only Mermaid art is cached; message layout
   is not (`markdown.rs`).
2. The main loop applies **one** message or input event per iteration and
   draws after it. A streamed turn is therefore one full rebuild per delta.
3. The stream is unfiltered. `wire_event` and `wire_response` never change the
   timeline, but each one currently costs a frame.
4. `auto_older` grows the timeline while a session runs, multiplying (1).
5. Debug builds are ~9× slower.

**Correction to an earlier claim:** keystrokes are *not* queued behind the
whole backlog. `tokio::select!` picks among ready branches; a Down probe was
handled immediately in the light run. The user-visible symptoms are (a)
streamed text falls behind and then jumps, (b) every key/mouse costs a full
rebuild, so at 250–500 ms/frame typing and scrolling are unusable, and (c)
CPU/battery use while streaming.

## Goals and limits

- Apply every event through the existing reducer, preserving FIFO order within
  each channel and durable-event deduplication. Do not filter the stream itself.
- Reduce redundant draws while keeping the final state of every burst visible.
- Preserve input, effects, status reporting, selection, folds, clipping notices,
  and automatic history loading.
- Reduce expensive parsing and layout in long sessions after measuring phase 1.

No Core changes, UI redesign, changed defaults or changed auto-load policy.
Cross-channel arrival order is not defined by the existing `select!`; preserve
causal boundaries instead of claiming a global order across independent channels.

## Phase 1: coalesce safe sync work and skip invisible events

Status: implemented 2026-10-31; see the result after 1.5.

### 1.1 Track whether a sync message needs a draw

Add a testable conservative classifier in `app.rs`:

```rust
pub fn sync_msg_affects_view(msg: &SyncMsg) -> bool;
pub fn apply_sync_tracked(&mut self, msg: SyncMsg) -> bool;
```

The wrapper computes the flag before moving the message, then calls the existing
`apply_sync` exactly once. All variants and unknown event kinds default to true.
Only `wire_event` and `wire_response` are candidates for false: the current reducer
excludes both from scratch. Keep `wire_request` true because its model feeds the
composer and turn metadata. Prove the exceptions against the complete app path,
including sidebar, diagnostics, alerts and effects, before enabling them.
Invisible here means no draw needed; the event must still advance the reducer’s
cursor and run all existing handling.

### 1.2 Preserve boundaries in the interactive loop

Extract shared handlers rather than duplicating the blocking and drain paths.
Start dirty; clear dirty after a successful draw; OR in subsequent dirty flags.
A tick returning false must never erase an earlier dirty flag. Keep the 125 ms
interval and `MissedTickBehavior::Skip`.

Start with a deliberately narrow batch: after receiving a sync message, consume
additional queued sync messages with `try_recv`, subject to an **8 ms soft budget**
and a named maximum message count. Check the deadline before each additional
message. A single snapshot/history reducer can exceed the budget; it is not a
hard latency limit. Stop when the queue is empty; never wait to fill a batch.
Do not drain every channel indiscriminately.

Stop extending a batch when input, a tick, or an auxiliary reply needs service.
Use a bounded fair scheduling policy so ready input cannot be starved by sync
traffic and a continuously ready input channel cannot starve sync or ticks.
Keep image-paste, action-done and dictation replies on their existing handlers,
including capture generation checks and capture-handle cleanup. A due tick runs
at the next service opportunity, not once per message. Rendering and a single
slow handler can still delay it.

**Effects are a barrier.** Dispatch effects produced by each applied item before
applying the next item. Preserve their order and existing program-status and
preference handling initially; optimize those only with separate evidence.
Quit exits immediately. Suspend and external editor return paths force a redraw.
Preserve the existing accepted-event guard around `program_status.event()` and
snapshot handling. Reconciling only once per batch can suppress a transient
blocked/working report, so retain per-item reconciliation for phase 1.

**Input is a render boundary.** The renderer writes hit maps, timeline blocks,
selection indices, dimensions and scroll normalization. Keyboard navigation after
a fold or resize can depend on the previous draw. Render pending changes before
handling input, and render the result of each input before another input is
handled. Mouse selection/copy must continue to refer to the displayed frame.
If testing reveals another render-dependent transition, make it a barrier or
move its normalization out of rendering with explicit parity coverage.

After a batch and its effects, draw if dirty before waiting again. Invisible-only
traffic must not force a draw, but it must not cancel an outstanding dirty draw.
Keep the existing exit behavior when input or sync channels close; auxiliary
channel closure must not create a busy loop. Do not discard a message already
removed from a channel when a deadline or barrier is encountered.

### 1.3 Scheduling expectations

An 8 ms budget limits additional reducer work under backlog. It **does not cap
frame rate**: an empty queue ends a batch immediately, so spaced deltas may still
produce one frame each. It also does not guarantee catch-up when event processing
cost exceeds the producer rate.

Implement batching first and measure draws per second, CPU, backlog and input
latency. If paced traffic still draws too often, add a separate stream redraw
deadline (initial candidate: 16 ms), with immediate input redraws and a wakeup
that guarantees the final dirty stream state is rendered even after traffic
stops. Specify and test that scheduler before adding it. Neither a deadline nor
batching can make a 500 ms render responsive; that requires phase 2.

### 1.4 Tests

Use a testable scheduler/helper shared with `main.rs`; app-only tests cannot
verify that the production loop batches or handles closure correctly.

1. Compare reference per-event rendering with batched replay at matched final
   boundaries, fixing clock and animation phase. Include starting context,
   text/reasoning deltas, actual tool event shapes from fixtures, history prepend,
   `turn.summary`, wire traffic and session switches. Compare the complete
   Ratatui buffer, cursor, hit map, blocks, selection, scroll and relevant model
   state. Rendering is stateful: investigate differences rather than weakening
   assertions to text equality.
2. Test classifier exceptions with complete valid events and assert cursor
   advancement, unchanged rendered output and unchanged effects. Unknown kinds
   and non-event variants stay conservative.
3. Test invisible-only batches, dirty followed by an invisible event or false
   tick, finite and continuously ready queues, budget/count exhaustion, and the
   last visible event followed by silence. Assert applied IDs and frame counts.
4. Exercise input fairness and render barriers: repeated block navigation,
   fold then Down/Enter, resize then click, session switch, selection and copy.
5. Compare ordered effects and OSC status reports with the reference, including
   approval appearing then resolving in one burst, Quit, suspend and editor
   return. Cover main and auxiliary channel closure and dictation generations.

Use an injected clock and deterministic queues for scheduler tests; wall-clock
performance belongs in benchmarks. Run the TUI suite, fmt and clippy as documented
in `docs/development.md`. Use a scratch `NEXUS10_HOME` for daemon/PTY runs.

### 1.5 Acceptance

- Every queued durable event is applied once in order, with matching final model
  and effects. A finite burst’s final visible state is drawn without another event.
- A prequeued sync burst needs fewer frames than events; paced-stream results
  are reported separately. Do not impose a fictitious 75 fps bound.
- At the reported 180 events/s workload, measure producer/applied cursors and
  queue depth over time, including history loading. Require no sustained backlog
  growth before claiming phase 1 solves it; otherwise proceed to phase 2.
- Record input-to-visible latency (p50/p95/max), draw time, reducer time, frame
  count, CPU and peak RSS. Phase 1 should leave individual draw costs comparable
  to baseline. No hard input-latency promise while draws still take 500 ms.

### Phase 1 result (2026-10-31)

Implemented in `src/frames.rs`, `src/app.rs` and `src/main.rs`, with coverage in
`tests/frames.rs`:

- The loop starts dirty, clears dirty only after a successful draw, and ORs each
  later result. A false tick cannot cancel an earlier draw.
- One sync message wakes the loop; up to `MAX_BATCH_MESSAGES` (64) queued sync
  messages then join it within an 8 ms soft budget. The queue is never waited
  on, and a barrier (ready input, a due tick, an auxiliary reply) is checked
  before each additional message so a removed message is never discarded.
- Input is a render boundary: pending changes are drawn before the select, and
  each input's result before the next input. Effects and per-item program-status
  reconciliation stay between items. Quit halts immediately; suspend and
  external-editor returns force a redraw.
- `sync_msg_affects_view` is conservative: only `wire_event` and
  `wire_response` skip a draw; they still advance the reducer's cursor,
  deduplication and `events_applied`.
- Closed auxiliary channels disable their select branch rather than spinning.
- `--bench WxH --replay` drives a deterministic in-memory stream through the
  same `frames::run_batch` policy. `NEXUS_TUI_REPLAY_MESSAGES` (default 200) and
  `NEXUS_TUI_REPLAY_EVENTS` (default 900) set the fixture.

Measured on an Apple M4, macOS 27.0, arm64, 160×48, base commit `8622de5` plus
this change, release build (debug in parentheses), scratch home and
`NEXUS10_REDUCED_MOTION=1`. The fixture is 200 messages of Markdown plus a code
fence each (~1.2 KB per message); the stream is 902 events (428 invisible
`wire_event`s, 451 text/reasoning deltas, a final message and a turn summary).
Command: `nexus-tui --dark --bench 160x48 --replay` (three runs).

| | Light (200 messages) | Heavy (`NEXUS_TUI_REPLAY_MESSAGES=1000`) |
| --- | --- | --- |
| First frame | 9.8–17.3 ms | 166–169 ms |
| Burst frames for 902 events | **16** (was 902) | **16** (was 902) |
| Burst total / applied rate | 135–149 ms / 6.1k–6.7k events/s | 2.51–2.62 s / 344–360 events/s |
| Burst draw, mean / p95 | 8.4–9.2 / 8.7–12.7 ms | 157–164 / 163–187 ms |
| Paced frames (empty queue) | 475 (one per visible event) | 475 |
| Paced applied rate | 219–226 events/s | **11–12 events/s** |
| Paced draw, mean / p95 / max | 8.4–8.7 / 9.1–9.7 / 9.9–34.0 ms | 164–166 / 178–237 / 225–356 ms |
| CPU user / sys | ~4.1 s / 22–25 ms | ~79 s / 0.44–0.50 s |
| RSS after replay | 32–33 MB | 81–84 MB |

Debug, light fixture: 16 burst frames / 1.2 s (748 events/s), 475 paced frames /
36.5 s (25 events/s), draw mean 76.7 ms. Individual draw cost is unchanged from
the baseline ratio (release ≈ 9× debug here); phase 1 only removes redundant
draws. The earlier, slower light numbers came from runs while another build held
the machine; these are three consecutive idle runs.

Reading: batching removes burst amplification completely (902 frames → 16), and
invisible wire rows no longer cost a frame. A light 200-message session paces at
219–226 events/s, above the reported 180 events/s with about 20% headroom. The
1,000-message session sustains only 11–12 events/s because each draw still costs
~160 ms, so phase 1 does **not** solve long-session streaming and phase 2
(layout caching) is the next change. The original live PTY measurement was not
reproduced in this run; this replay measures scheduling, layout and paint only,
not terminal output or input latency.

## Phase 2: cache expensive layout

Status: implemented 2026-10-31; see the result at the end of this section.

Phase 1 reduces draw frequency; phase 2 reduces draw cost. A per-message cache
alone does **not** make total frame cost independent of loaded history: hashing
all parts, scanning turns, cloning lines and assembling all blocks remain O(N).

- Profile `timeline::build` and drawing separately. Identify stable layout units
  before splitting: activity groups span messages and turn metadata, tool results,
  streaming content and child status can change a unit without changing its
  message parts.
- Cache expensive Markdown/wrapping/diff work with explicit dependency keys.
  Include session/message identity, content revision or value, width, theme,
  detail/fold/raw state and every external dependency used by the cached unit.
  A parts hash alone is insufficient. Avoid reserializing all history each draw;
  use reducer-maintained revisions where justified, with correctness tests.
- Keep selection/focus, spinner/flash phase and time-dependent decorations outside
  stable cached content where possible. Store relative block offsets and preserve
  stable keys when assembling the timeline.
- Cover history merge/replacement, scratch-to-summary replacement, tool completion,
  child updates, resize, theme/detail/fold/raw changes and view switches.
- Bound cache memory by retained entries/bytes, evict removed messages and sessions,
  and measure peak RSS during streaming, history loading and view switching.

Provisional release-build targets at 160×48: realistic 200-message draws below
2 ms and the reported heavy 1,000-message case at 5–15 ms. Validate on reproducible
fixtures; these are targets, not predicted outcomes. Extend the parity harness
across all dependencies and assert cache hits/misses and eviction behavior.
Measure residual assembly cost before choosing the next phase.

### Phase 2 result (2026-10-31)

Implemented in `src/timeline.rs` with one `LayoutCache` per `App` (not a
thread-local), plus a cheaper wrapper:

- `markdown()` caches the rendered body (lines and relative fenced-code block
  offsets). Its key includes the body and full text, width, and a resolved
  theme fingerprint; the parent block key and turn are applied when the entry is
  appended, so selection/focus are not baked into cached lines.
- `text::wrap` results are cached by text and width (`wrap_text`), covering user
  bands, reasoning previews and legacy Markdown fallbacks.
- `tool_detail()` caches unclipped detail rows by tool name, arguments, result
  output/error/media, `raw`, width and theme. Clipping stays outside the cache,
  so high detail, `o show all`, and the raw toggle re-use the same rows.
- `wrap_spans` now tracks borrowed graphemes and merges adjacent same-style
  cells into runs, cutting allocations and making cache hits cheap.
- The turn loop carries the message index instead of scanning all messages with
  `msg_index` per user message; that O(N²) scan dominated the 1,000-message
  frame once parsing was cached.
- The cache is bounded at 32 MiB with FIFO eviction, exposes hit/miss/eviction
  counters, and evicts oldest entries first. Removed content ages out under the
  byte cap; keys are content-derived, so a rebuilt entry cannot be stale.
- `tests/frames.rs` adds: cold-vs-cached frame equality, an unchanged frame
  doing no parse/wrap work, width and theme invalidation, fold/unfold keys,
  history merge and replacement, a tiny-cache eviction test, and tool
  completion/raw views. 23 frame tests plus the full suite pass.

Measured with the same deterministic replay (Apple M4, macOS 27.0, 160×48,
release build, scratch home, `NEXUS10_REDUCED_MOTION=1`; three light and two
heavy runs):

| | 200 messages | 1,000 messages |
| --- | --- | --- |
| First frame | 7.5–10.0 ms | 33.8–34.1 ms |
| `timeline::build` (width 85) | 1.46–1.58 ms | 7.3–8.5 ms |
| Burst draw, mean / p95 | 1.20–1.26 / 1.32–1.48 ms | 4.99–5.23 / 5.58–5.97 ms |
| Paced draw, mean / p95 / max | 1.13–1.17 / 1.21–1.37 / 6.4–6.7 ms | 5.15–5.23 / 5.89–5.97 / 31.5–31.9 ms |
| Paced applied rate | 1,615–1,678 events/s | 363–368 events/s |
| RSS after replay | 25–26 MB | 53–55 MB |
| CPU user / sys | 0.58–0.60 s / 4–6 ms | 2.64–2.65 s / 15–18 ms |

Both provisional targets are met: realistic 200-message draws are below 2 ms,
and the 1,000-message case is 5.2 ms (5–15 ms target). Hover history with long
diffs (`NEXUS_TUI_REPLAY_TOOLS=1`, high detail, every tenth message an `edit`
call and 120-line result) costs 2.0 ms at 200 messages and 9.4–9.9 ms at 1,000;
the cache holds ~2–15 MB, while RSS reaches ~32 MB / ~309 MB because the
allocator retains the per-frame diff-row churn at high-water. Debug builds are
9.3 ms (200) and 32 ms (1,000), about 8× release.

Residual cost is now assembling/cloning all loaded lines and painting them
(O(N) with a small constant); 5.2 ms at 1,000 messages is within budget, so
phase 3/4 are optional until a larger history or a heavier terminal changes the
measurement.

## Phase 3: separate timeline and chrome invalidation

Status: implemented 2026-10-31; see the result below.

Optional after profiling. Non-selected session events can still affect child
cards, shared settings, waits and other visible state. Use explicit dependencies,
not selected-session identity alone, before reusing the last timeline. Continue
redrawing chrome when required and preserve interaction metadata.

### Phase 3 result (2026-10-31)

`App::timeline_for(width)` (`app.rs`, called from `render.rs`) keeps the last
`Rc<Timeline>` and reuses it when a cheap key is unchanged. The key covers:

- a `timeline_revision` bumped by every sync message and API result, so new
  messages, history merges, tool completion, child updates, summaries, snapshots
  and non-selected session events invalidate conservatively;
- the viewed session, content width and resolved theme fingerprint;
- detail level, session/settings view, focus, selected block, every fold/raw/
  `show all` entry, group flash state and the animation frame when motion is on;
- wall-clock seconds while a session is running, because turn-footer durations
  are time-dependent.

Chrome-only changes (composer typing, scrolling, toasts, alerts, status text,
idle ticks) now redraw the shell without rebuilding the timeline. Direct model
edits outside the reducer must call `App::invalidate_timeline()`; the timeline
tests' synthetic-message helper does. `tests/frames.rs` adds 27 tests including
typing/scroll reuse, idle-tick reuse, animation-phase invalidation, model/UI/
fold/width invalidation and conservative non-selected event invalidation.

`--bench --replay` now also runs 110 chrome-only frames (100 composer
keystrokes plus 10 scrolls) after the paced stream. Release:

| | 200 messages | 1,000 messages |
| --- | --- | --- |
| Chrome frames / timeline builds | 110 / **0** | 110 / **0** |
| Chrome draw, mean / p95 | **0.36 / 0.39 ms** | **0.54 / 0.59 ms** |
| Stream draw for comparison | 1.48 ms | 5.54 ms |

Chrome-only frames are 4–10× cheaper than a streamed frame at the same history
size. Non-selected events still rebuild (correct but conservative); a full
per-dependency graph is left for when profiling shows it matters.

## Phase 4: viewport layout

Status: shared cached line slices landed 2026-10-31; viewport-only assembly is
still deferred. The phase 2/3 targets are met at 1,000 messages, so this is not
needed for the reported workload; the measurement that would trigger the rest is
at the end of this section.

If parsing caches leave assembly cost above budget, use indexed heights and build
only viewport blocks plus a margin. Preserve total height, jump-to-block, sticky
user headers, fold behavior, scroll anchors and selection/copy semantics. Width
changes and newly loaded history need explicit height invalidation.

Measured with the same replay beyond the target (`NEXUS_TUI_REPLAY_MESSAGES`,
release, no tools):

| Messages | Timeline build | Paced draw mean | RSS (burst run) |
| --- | --- | --- | --- |
| 1,000 | 7.3–8.5 ms | 5.15–5.23 ms | 53–55 MB |
| 2,000 | 21.7 ms | 15.2 ms | 468 MB |
| 5,000 | 47.3 ms | 32.7 ms | 1.1 GB |

At 5,000 messages per-frame assembly dominates. `Timeline::lines` now holds
`Rc<TLine>` and cached Markdown bodies hand out line references instead of deep
clones, so a cached block costs a pointer increment rather than a span clone.
That reduced the 5,000-message replay's peak RSS from ~1.1 GB to ~0.33 GB with
no change to the 1,000-message target (5.7 ms paced). Assembly still walks every
message and allocates block metadata, so viewport-only line materialization
(indexed heights, then build only the visible blocks plus a margin) remains the
next step for 2,000+ message histories; it is deferred rather than changing
block offsets, sticky headers and selection semantics without a workload to
validate them.

## Reproducible benchmark and delivery

Extend `--bench` with a deterministic streaming replay using the production
scheduler/helper. Keep the existing scroll benchmark for draw-cost comparisons.
Measure both prequeued bursts and paced 180 events/s input, with light and heavy
history fixtures. Include visible and invisible traffic, automatic older-page
loading, long tool/diff content and a run with input interleaved. A replay without
a PTY measures scheduling/layout; retain a scratch-Core PTY run for end-to-end
terminal behavior and input latency.

Report release and debug results separately; optimization may change their ratio.
Record build revision/profile, hardware, terminal size, fixture seed, commands,
produced/applied counts, frames, queue depth/lag, reducer/draw timings, CPU and RSS.
Do not equate frame time with total input or stream latency.

Expected files when implementation lands:

- `nexus-tui/src/main.rs` and a testable scheduling helper — batch policy and barriers.
- `nexus-tui/src/app.rs` — conservative sync dirty tracking.
- `nexus-tui/tests/frames.rs` — scheduler, parity and interaction coverage.
- `nexus-tui/tools/bench.sh` / `--bench` — streaming replay.
- `nexus-tui/README.md`, `docs/turn-summary-benchmark.md` — implemented behavior
  and reproducible results, updated with the code.
- `PLAN.md` — phase status and measured next step. Mark only the phase that landed
  complete; keep remaining performance work visible.
