# TUI responsiveness plan

Status: authorized by the user and implemented in the working tree. Verification
results and remaining limits are recorded below. No commit or release was made.

## Objective

Make the native Ratatui TUI feel instant. Keystrokes, scrolling, selection and
transcript disclosure must never wait on Python. A streamed token, from the
moment the host sends it to the moment it is drawn, must cost
O(changed content), not O(whole session). Keep one canonical session state and
the existing daemon/host contract. Measure every step. Do not promise "zero lag".

## Where the lag comes from (code review, 2026-10-04)

Already local and cheap: editing the draft, cursor movement, and wheel/key
scrolling. Rust's `Editor` owns the draft, and `main.rs` coalesces input bursts
into one draw. Those paths are not the problem. The cost is in what happens
around them:

| # | Hot path | Where | Cost |
| --- | --- | --- | --- |
| 1 | **Every streamed event triggers a full `update()`**: `project()`, then `json.dumps` of the *whole* snapshot as a change fingerprint, then encode, write and `await drain()`. Desktop coalesces at 16 ms; the TUI path does not. | `prototype.py` `update()` (`immediate` is ignored when not desktop) | Python CPU per token grows with session size and wire size. Rust must parse every schema-2 patch because patches depend on each other. |
| 2 | **Every snapshot resends static sections**: `sessions`, `tabs`, `history` (prompt history, up to `MAX_ENTRIES`), `logs`, `details_panel`, `nav` and `context_lines`. | `project()` lines ~556–640 | Bytes per token, serde parse time in Rust, and the fingerprint dump in Python. |
| 3 | **Transcript clicks round-trip to Python and re-project twice.** An `operation` action calls `project()` once to validate, then `update()` projects again. `answer` does the same. | `prototype.py` action loop | Click→frame includes two full projections, the encode and a Rust parse. This is what the original plan targets. |
| 4 | **Rust re-lays-out the whole transcript on any change.** `update_content` runs `disclosure.project(blocks)`, which deep-clones *every* block (text, members, diffs). It also builds a `format!` key per block and re-checks every part, even when a patch changed only the last block. | `render.rs` `update_content`, `disclosure.rs` `project` | During streaming, each frame costs O(transcript bytes). |
| 5 | **UI-only toggles round-trip to Python**: the sessions and details sidebars, the details tab, `file_toggle`, `logs_fold` and `details_visible`. `Preferences.set` also writes JSON to disk synchronously, on the event loop. | `main.rs` `send(toggle…)`, `preferences.py` | Each toggle waits for a projection, a disk write and a snapshot. |
| 6 | **Completion waits 120 ms for debounce, then a host round trip.** Cached ancestors fill some of the gap. | `main.rs` `complete_due` | The `/command` popup appears late, even though the command list is static. |
| 7 | **The action loop is serial.** One `await` on a slow host call (`complete`, `doctor`, `refresh_models`, clipboard read) delays every action queued behind it. | `prototype.py` `while line := await process.stdout.readline()` | Intermittent stalls that look like dropped keys. Not verified by measurement. |
| 8 | Minor per-tick work: `typed` clones the draft every 8 ms, `disclosure.scope` is re-`format!`ted every tick, and completion candidates are recomputed every tick. | `main.rs` main loop | Small. Fix only if the trace shows it. |

Items 1, 2 and 4 decide how responsive streaming feels. Items 3 and 5 decide
how responsive clicks feel. The original plan covered only item 3, with part of
item 4.

## Ownership (unchanged in principle, widened in scope)

| Concern | Owner |
| --- | --- |
| Session events, reduction, persistence, agent execution | Python / daemon |
| Tool permissions, approvals, submit/cancel, configuration | Python / host |
| Labelled, redacted, control-safe transcript content | Python projection |
| Transcript expansion/folding, local navigation | Rust |
| Sidebar visibility, details tab, file-diff open state, logs fold | Rust (applied optimistically, then persisted by Python without blocking) |
| Wrapping, viewport, selection, drawing, static command completion | Rust |
| Session switching, child pages, `@file` and argument completion | Host action via Python |

Rust stores presentation state beside the mirrored transcript. It never
implements a second domain reducer.

## Stages, in order of expected impact

Each stage is landed and measured on its own. Keep a stage only if it improves
the numbers from stage 0 and passes the existing regressions.

### 0. Baseline and instrumentation

- Add Python-side timings behind `NEXUS_TUI_TRACE=1`: `project`,
  `fingerprint`, `encode`, `write+drain`, and `snapshot_bytes`. Keep a bounded
  ring and report p50/p95/max. Merge them into the existing `ui_trace` lines so
  that one Logs panel shows both sides. Never trace conversation bodies.
- Rust already records `parse`, `update_content`, `draw`, `flush`,
  `key→frame`, `wheel→frame` and `snapshot→frame`. Add `click→frame`, plus
  `event→frame`: a host event timestamp carried in the snapshot and compared
  at draw time.
- Write a repeatable benchmark with `tests/mock_llm_fixture.py` and the mock
  scenarios (docs/devtools.md). Cover a short session (10 turns), a long one
  (500 turns with tools, diffs and thoughts), one large tool output (~1 MB),
  idle, and a stream at about 50 tokens/s. Record hardware, workload sizes and
  results in this file.

### 1. Coalesce streamed updates in Python (items 1 and 7)

- Use the desktop `UpdateCoalescer` (`ui/desktop/wire_schedule.py`, 16 ms) for
  the TUI as well. `controller.ingest(event)` runs immediately for every event.
  `project`, encode and write run at most once per frame.
- These still flush immediately: user actions, one-shot composer fields
  (`restore`/`insert`), prompt/permission changes and disconnect notices.
  Coalescing must never drop a one-shot field (desktop already tests this; add
  the same test for the TUI).
- Run slow, independent action handlers (`complete`, `refresh_models`,
  `clipboard`, `copy_*`) as tracked tasks, so that they no longer block the
  reader. Keep ordering wherever it matters: `submit`, `cancel`, `answer` and
  `operation` stay serial. Bound the number of outstanding tasks. Discard
  completion results whose prefix is stale.

### 2. Send only changed sections (item 2)

- Fingerprint each section (`sessions`, `tabs`, `history`, `logs`,
  `details_panel`, `nav`, `context_lines`, header and status fields) by itself,
  using cheap keys such as revision counters, not `json.dumps` of the whole
  snapshot.
- Wire schema 3 in the TUI: an omitted section means "unchanged". Rust merges
  it into the previous `Snapshot`. Send `history` once per client, then only
  appends. Keep the version check: Rust rejects unknown schemas. Keep schema 2
  reading for one release.
- Target: a streamed token sends the patched block(s) plus small status fields
  only.

### 3. Rust-local disclosure (item 3; original plan stages 2–3)

- Python sends complete hidden presentation for groups, details, output folds,
  thoughts and turns (`local_ui`, `local_detail`, `local_preview`, `turn_id`,
  `fold_summary`) once, and resends it only when its source changes. Do not send
  a complete body and a clipped body when a fold boundary can describe both.
  Do not wrap hidden content.
- Rust toggles on mouse and keyboard with **no stdout action**. Choices survive
  patches and survive leaving and returning to a session or page. Choices are
  bounded: LRU with 4096 entries per scope, a defined eviction order, and
  client-lifetime only.
- `/verbose`: on reveals every disclosure; off restores the individual choices.
  Test both directions.
- Delete the now-dead Python paths: `shell.expanded`/`collapsed_turns` for the
  TUI, and the `block_toggle` handling when `local_transcript` is on. Keep them
  for desktop until desktop adopts the same model.
- For the actions that still go to Python (`answer`, non-disclosure
  `operation`): validate against an allowed-operation set built during the
  last `update()`, instead of calling `project()` again.

### 4. Incremental Rust layout (item 4)

- Make `Disclosure::project` borrow instead of clone. Return
  `Cow<Content>`, and clone only blocks that a local choice actually overrides.
- Carry the patch start (`blocks_from` from the wire, or the first index whose
  disclosure changed) into `update_content`. Reuse `parts` for the unchanged
  prefix without per-block comparisons or `format!` keys. Key parts by
  `(page, id)` with interned or precomputed strings.
- A disclosure click relays out only the affected block. Large outputs still
  wrap on open. Measure that case and report it separately. Add viewport-only
  wrapping only if the numbers require it.

### 5. Local UI-only toggles and completion (items 5, 6 and 8)

- In Rust, apply sidebar visibility, the details tab, file-diff open state and
  logs fold immediately. Then send the action, so Python can persist it and
  fetch any data it needs (for example, logs polling).
- Python's echo snapshot must not undo a newer local change. Carry a
  per-toggle sequence number and let Rust ignore stale echoes. Generalize the
  existing `last_opened` precedent.
- Move `Preferences.set` disk writes off the event loop (`asyncio.to_thread`)
  and debounce them (about 250 ms, flushed on exit).
- Send the static slash-command list once. Rust filters `/command` completions
  locally with no debounce. `@file` and command arguments keep the host path,
  with the debounce lowered to about 60 ms if the trace supports it.
- Fix the item-8 per-tick allocations only if stage 0 shows they matter.

### 6. Validate and document

Update `docs/ratatui-parity.md`, `docs/architecture.md` (ownership table),
`docs/decisions.md` (why disclosure and UI toggles are local, why schema 3), and
the `.agents/skills/nexus-ratatui` references, so that future UI work does not
reintroduce round trips.

## Acceptance criteria

Measure on the benchmark machine with the 500-turn session. Record the before and
after numbers in this file. Do not claim a target is met without numbers.

| Path | Target p95 |
| --- | --- |
| `key→frame` (typing, cursor), `wheel→frame` | ≤ 16 ms |
| Disclosure `click→frame` (group, detail, thought, turn, ordinary output) | ≤ 16 ms, with Python silent |
| Opening a large output (~1 MB) | reported separately; no target |
| Sidebar or details-tab toggle `key→frame` | ≤ 16 ms (local); persistence is asynchronous |
| `/command` completion popup | next frame after typing |
| Streamed `event→frame` | ≤ 50 ms, and does not grow with session length |
| Bytes per streamed token on the wire | report before and after; expect a reduction of more than 10× |

Functional requirements:

- Disclosure sends nothing to stdout, by keyboard or by mouse. Verify this on a
  PTY by asserting that no action is written.
- Streaming while blocks are expanded keeps those choices. Session and page
  navigation isolates choices and restores them.
- Parameters, results, errors, diffs and attachment actions remain reachable.
  Displayed and copied text stays control-safe and redacted. Clipping is
  announced.
- Coalescing never loses `restore`/`insert` and never reorders submit or cancel.
- A stale toggle echo never undoes a newer local toggle.
- Cache tests prove that unrelated blocks are *reused* (part pointer
  equality or build counters), not just that the displayed strings are equal.
- Rust tests, focused Python tests, `tests/test_layering.py`,
  `tests/test_ui_layering.py`, controlling-PTY checks and the inspected native
  captures all pass. Report unrelated pre-existing failures; do not fix them.

## Verification cases

On a real PTY: expand a group, then a member, then its output, with no new
Python snapshot. Repeat with the mouse. Stream while expanded. Leave and return
to a session and a child page. Fold and unfold a completed turn. Toggle
`/verbose` both ways. Inspect diffs and attachments. Open a large output.
Toggle both sidebars and the details tab quickly while streaming. Type a
`/command`, then an `@file`. Run the benchmark before and after each stage.

## Risks

- **Larger initial payload** from complete hidden content. Measure memory
  and first-snapshot time. Keep the existing content limits. No arbitrary
  transcript cutoff.
- **Desktop shares the bridge projection.** Schema-3 sections and the
  local-disclosure fields must not break `ui/desktop/wire.py`. Run
  `tests/test_desktop_wire.py` on every stage.
- **Optimistic toggles can diverge** from Python state. The sequence number
  handles that. Python stays the source of truth for persistence.

## Current worktree status

Stages 0–6 are implemented and measured (see the ledger below). Nothing is
committed. Open items are listed under "Remaining work".

## Out of scope

Rewriting the agent core, duplicating the session reducer in Rust, moving
provider or permission logic, web features, and making presentation choices
durable across client restarts.


## Implementation and measurement ledger (2026-10-04)

Stages 1–5 are implemented: shared 16 ms scheduling, bounded independent handlers,
terminal schema-3 section deltas, local disclosure, incremental Rust layout,
optimistic sequenced UI toggles, debounced preference persistence, and next-frame
static command completion. File/argument debounce remains 120 ms: no measured
reason to lower it was established. Ordinary root text/thought deltas use a
changed-tail projector; other events and child pages retain full projection.
Agent dependencies invalidate only the owning turn cache, rather than every turn.

The baseline is a saved pre-stage-1 Python projection and native debug binary
(including the provisional disclosure edits), not a reconstructed pristine release.
Measurements ran on Apple M4, macOS 27 arm64, Python 3.14.3, a 140×40 controlling
PTY with output continuously drained. Workloads have 10/500 turns, each with Read,
Edit/diff, thought and assistant content. The stream is 60 updates paced at 50/s,
plus typing/deletion and wheel input. Projection/wire summaries use 50 samples.
Disclosure measures 60 silent mouse toggles with a 50/2,500-block history; it does
not simulate provider latency. Metrics include native terminal writes, but do not
measure the user's terminal emulator painting pixels. Raw reports are under
`artifacts/tui-responsiveness/` (`baseline-native.json`, `stage2.json`,
`stage4.json`, `final-checked.json`). Stage 1 scheduling has correctness tests;
its live-host end-to-end latency was not separately benchmarked. Stages 3/5 did
not receive isolated baseline runs; the final native report measures them together.

| Metric | Before | After |
| --- | ---: | ---: |
| 500-turn Python projection p95 | 6.26 ms | 0.33 ms |
| 10-turn Python projection p95 | 0.76 ms | 0.31 ms |
| 500-turn wire comparison p95 | 0.85 ms | ~0.01 ms |
| 500-turn native key→frame p95 | 37.49 ms | 5.39 ms |
| 500-turn native wheel→frame p95 | 11.13 ms | 5.59 ms |
| 500-turn native snapshot→frame p95 | 70.47 ms | 14.42 ms |
| 500-turn native layout p95 | 31.93 ms | 0.24 ms |
| Silent disclosure click→frame p95, 10-turn equivalent | not measured | 3.92 ms |
| Silent disclosure click→frame p95, 500-turn equivalent | not measured | 4.24 ms |
| Median ordinary streamed payload, 500 turns | 7,055 bytes | 548 bytes (12.9× smaller) |
| p95 streamed payload, 500 turns | 10,113 bytes | 2,748 bytes (3.7× smaller) |
| Large-source-output projection p95 | 142.13 ms | 2.46 ms |

Ordinary root stream event→frame p95 was ~15 ms in the final native probe at both
history sizes. Its timestamp is set before projection in the probe; this figure
**excludes the live bridge coalescer and actual host/network transport**. The ≤50 ms
live-host acceptance target is therefore not claimed as verified. Recent real host
timestamps are instrumented for a future live run, with historical replay using
bridge ingress instead. Median wire reduction exceeds 10×; p95 reduction does not.

Initial 500-turn projection took 167 ms and produced ~1.38 MB (baseline ~1.81 MB,
which duplicated details). Initial native parse/layout maxima were ~43/27 ms.
Sampled native RSS was ~8.5 MiB for 10 turns and ~30 MiB for 500 turns. These are
sampled process RSS figures, not allocation-profiler peaks; Python memory was not
measured separately.

Large-output exception: the source-output benchmark preserves existing labelled
clipping and sends ~60 KB initially for a 1 MB source result. A separate direct
native fixture bypasses that clipping to expose a full 1 MB, ~61,681-line body:
first full disclosure took ~470 ms and sampled RSS reached ~114 MiB. No performance
target was specified for this case. Viewport-only wrapping is not implemented.
Reopening cached content avoids its first-wrap work.

Verification:

- Rust unit/render/cache regressions pass, including pointer reuse of unaffected
  prefix and suffix parts, single-group projection, LRU eviction, verbose restore,
  section omissions and stale UI echoes.
- Independent controlling-PTY checks pass for keyboard/mouse disclosure with no
  stdout action, streamed output while expanded, static completion without a host
  request, optimistic sidebar toggles/stale echo, and existing context dialogs.
- Real mock LLM fixture tests preserve tool parameters/results/errors, edit diffs
  and child-page operations. Desktop wire/scheduling, docs and layering checks pass.
- Ruff passes. Native closed/group/detail captures were generated with a silent
  Python fixture and visually inspected; labelled parameters/results remained
  readable. Existing wide/narrow, tool panel and child-page captures were inspected.
- The broad native Python suite has pre-existing attachment assertions and session
  status expectations that disagree with the current dirty worktree. The old PTY
  all-in-one test expects queue mode where the existing renderer sends steer.
  Those unrelated expectations were left unchanged.

Remaining verification limits: no isolated measurements for every stage, no
live-host ≤50 ms claim, no child-page changed-tail optimization, and no complete
large-output viewport-only layout. These limits are not hidden by the ordinary
interaction measurements. The terminal launcher selects the newly built executable
on the next launch; an already running client must be restarted.

## Live-host measurement (2026-10-04, after the ledger above)

`tests/ratatui_live_latency_fixture.py` drives the real host subscription, the
Python bridge coalescer and the native terminal (debug build, Apple M4) with a
60-delta stream at 50/s after seeding 10 or 500 turns of history. Reports:
`artifacts/tui-responsiveness/live-host.json`, `live-stream.json`. Set
`NEXUS_TUI_TRACE_RAW=1` to append the ordered `event→frame` samples to the
native trace.

| Live metric | 10 turns | 500 turns |
| --- | ---: | ---: |
| event→frame p50 | 27.5 ms | 28–31 ms |
| event→frame p95 | 32–36 ms | 36–74 ms (varies run to run) |
| snapshot→frame p95 | 14–16 ms | 18–19 ms |

Ordered samples for a 500-turn run (ms): the first two events 82, 54; 56
ordinary deltas at 22–35; the last two events 78, 89. With n=63 the p95 is the
fourth-worst sample, so it is set by the turn-start and turn-end transitions,
not by token deltas. Per-token cost does not grow with history (p50 flat).
Rust snapshot→frame stays under 19 ms at p95, so the extra 40–60 ms on those
four events is spent before Python sends the snapshot. Cause not verified: the
fixture runs host and bridge on one event loop, so host turn start/end work may
be delaying the bridge, and these events also use the full projection.

Profiling (temporary logging in the bridge, since removed): at turn start the
events `turn.started`, `input.started` and `context.assembled` reach the bridge
already 68, 68 and 50 ms old; at turn end `model.stopped` and `turn.completed`
arrive 34 and 50 ms old. Bridge `update` itself takes 0 ms for ordinary events
and at most 32 ms for `turn.completed` (16 ms coalescer flush plus one full
projection). So the tail is dominated by host-side delivery delay: the host
does turn-boundary work (context assembly for 500 turns, persistence) between
creating an event and delivering it, and the fixture shares one event loop.
The UI path is not the bottleneck. Whether a separate-process daemon delivers
these events sooner was not measured.

Status against the ≤50 ms target: ordinary streamed deltas meet it at both
history sizes; the p95 at 500 turns does **not** reliably meet it because of
turn-boundary events. Not claimed as met.

## Remaining work

- Turn-boundary tail: shown above to be host delivery delay. Re-measure with
  the host in a separate daemon process; optionally cut the 32 ms
  `turn.completed` bridge cost by skipping the coalescer wait when nothing is
  pending.
- Child-page changed-tail projection (child pages use the full projection).
- Viewport-only wrapping for an uncapped ~1 MB body (first open ~470 ms). No
  target was set; do this only if the numbers are judged to require it.
- Pre-existing failures not touched: `test_ratatui_projection.py::test_session_cards_carry_status_words_age_and_the_current_marker` and
  `test_ratatui_actions.py::test_session_switch_retains_images_and_stable_markers`.

## Subagent page lag and the debug-binary finding (2026-10-04)

Reported: lag and glitching, worst with a subagent page open or a subagent tool
clicked, and "not slow a few commits ago".

- **Debug binary was the one running.** The launcher picks the most recently built
  executable. My `cargo build`/`cargo test` runs made `target/debug/nexus-ratatui`
  the newest, so `nexus chat` ran an unoptimized build. Same benchmark, 500 turns:
  draw p95 3.4 ms debug vs 1.1 ms release; key→frame p95 6.4 vs 1.9 ms; wheel→frame
  p95 5.5 vs 2.1 ms. Release rebuilt (it is now newest). Whether this alone explains
  the reported lag is not verified.
- **Host replay per click.** `HostFacade.agent_transcript` called `state()`, which
  re-folds the whole log: 160 ms at 500 turns, on the host loop. Now cached
  incrementally (first call 176 ms, later calls ~1 ms). Test:
  `test_facade_state_cache_applies_only_the_new_tail`.
- **Subagent page projection.** Every update re-projected the root session, re-hashed
  and redacted every child block and rescanned all tool diffs for the details panel.
  500 root + 100 child turns: 46 ms → 1.4 ms per projection (10+10 turns 4.5 → 0.4 ms).
- Not measured: a live subagent stream end to end, and the "glitching" part of the
  report (visual artifacts); I have not reproduced or inspected a glitch.


## Session open cost (2026-10-04)

Reported: opening a session with many tool calls and turns takes 1–2 s. Measured
in-process (no socket), 500 turns, Apple M4, before → after:

| Stage | Before | After |
| --- | ---: | ---: |
| `bootstrap()` first open | 425 ms | ~205 ms |
| `bootstrap()` repeat open | 240 ms | ~205 ms |
| First projection (500 turns) | 244 ms | ~211 ms |

Causes found:
- `bootstrap()` requested the host's full-view baseline (`SessionState`: whole-log
  fold, `to_dict`, 1.7 MB transfer) and then kept only its seq. The client then
  streamed every event and folded the log again. The baseline fetch is removed; the
  cursor is the streamed view's last seq.
- `escape_controls` ran `unicodedata.category` per character even on pure-ASCII text
  (592k calls per first projection). ASCII text now returns after the control regex.
  Test: `tests/test_ui_support_text_escape.py` (equivalence with the old code).

Still on the open path (not changed): the client reducer fold of the whole log
(~160 ms for 8,000 events), the full first projection (~210 ms; the turn cache is
cleared on a switch), Rust parse/layout of ~1.4 MB (~70 ms), `AgentCurrent` and
`inspect_context` host calls (~25 ms each). Real sessions with large tool outputs
and a socket transport will cost more than this fixture; not measured. Reducing the
rest needs a design choice (hydrating the view from the host baseline, or projecting
only the newest turns first); no transcript cutoff was introduced.

Full suite after these changes: 5,021 passed, 10 failed. All 10 also fail without
them (the 8 listed earlier, plus `test_session_summary_is_transport_neutral` and
`test_schema_upgrade_keeps_old_rows_and_leaves_their_title_alone`, a schema-version
mismatch in the dirty worktree).
