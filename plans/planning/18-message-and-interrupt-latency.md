# Plan 18: Remove the delay before a new message (and a steer→interrupt promotion) shows up

Status: implemented measurement fixtures, acceptance incomplete. Idle Enter→prompt p95 is 78.944 ms; first worktree prompt p95 is 135.465 ms, above the 100 ms target. Interrupt marker frame correlation remains missing, and **the end-to-end cause is not yet confirmed**. No production host scheduling change was justified. Source: root
`TODO.md` item 11, quoted unchanged:

> - new messages takes a second to show up ( remove the lag ) (same for steer to interupt promotion )

## 1. Intent

1. After the user presses Enter, their message appears in the conversation within one frame
   (target: p95 < 100 ms on a local daemon), whether the session is idle or busy.
2. After promoting a waiting steer to **interrupt**, the screen shows that within one frame, and
   the interrupt takes effect as fast as cancellation allows.

### Non-goals

- Steers are delivered at the next iteration boundary by design; this plan does not change *when*
  a steer reaches the model, only how fast the UI reflects state.
- No change to durability: every row must still be a reduction of the log or clearly a
  transient "sending" state (`AGENTS.md:101-105`).

## 2. Current state (checked 2026-10-25)

Path of a message (code read, not timed):

1. Rust composer → bridge action → `ShellActions.submit` (`nexus/ui/ratatui/actions.py:549-623`)
   → `await self.client.enqueue(...)` (round trip to the daemon).
2. Host `enqueue` (`nexus/host/facade.py:834-894`): title candidate, `admit_submission` (durable
   `input.queued`), for `interrupt` `await self.cancel(...)`, then `await self.supervisor.submit(...)`.
3. The session bus pushes events to subscribers (no polling: `Session.subscribe`,
   `nexus/session/session.py:1421-1481`).
4. Client `update()` (`nexus/ui/ratatui/prototype.py:1530-1647`): events not in the immediate set
   are coalesced with a 16 ms delay (`UpdateCoalescer(..., delay=0.016)`, `:1656`); the immediate set
   is permission/question/`tool.requested`/`turn.completed`/`turn.failed` (`:1550`), so
   `input.*`, `message`, `turn.started` are coalesced (16 ms is not a second).
5. Rust main loop polls input at 8–50 ms (`rust/tui/src/main.rs:1019-1050`).
6. There is **no optimistic echo** of the submitted text in Rust (no match for pending/echo state
   in `main.rs`).

Interrupt promotion: `queue_edit(op="interrupt")` → `QueueSetMode` → `HostFacade.set_queued_mode`
(`facade.py:904-920`) → durable `input.mode_changed` (`session.py:1866-1893`) → reorder →
`self.cancel(drop_queue=False)` → `Supervisor.cancel` only *signals* the session
(`nexus/host/supervisor.py:280-284`). The running turn then has to notice cancellation, end, run
turn-end work (for a worktree session: commit and landing detection, `nexus/worktree_sessions.py:539-571`,
Git in a thread), and the next turn must start before the promoted message becomes a turn.

## 3. Hypotheses (ranked; to be confirmed by §4.1)

- H1. **The user card is only drawn when the turn starts**, not when `input.queued` arrives. Turn
  start includes supervisor scheduling, `Session.start_turn` work (manager compaction check for the
  manager, hooks such as `UserPromptSubmit`, context assembly, worktree creation for a first message
  in a fresh worktree session), each of which can take hundreds of ms. Check how the reducer and
  `_waiting_inputs`/`queue_lines` show an `input.queued` for an **idle** session.
- H2. **The bridge serialises the submit round trip** behind other awaited work (dashboard refresh,
  ownership refresh, preview fetch) so the enqueue itself starts late. Check the bridge's action
  dispatch: is `submit` awaited inline in the same loop that handles polls?
- H3. **Interrupt latency is cancellation + turn-end work**: a long-running tool (shell) or a
  provider stream that only checks the token at boundaries, then the worktree commit at turn end.
- H4. The `queue_lines`/`queue_items` rows update late because they are part of a full projection
  that is skipped by the streaming fast path (`stream_projection.project`, `prototype.py:1585`).

## 4. Plan

### 4.1 Measure first (no behaviour change)

- Extend the existing trace (`NEXUS_TUI_TRACE=1`, `docs/testing.md:243-264`,
  `docs/ratatui-parity.md:1141-1142`, `:1314`) with these spans, all bounded samples:
  - Rust: Enter keypress → action written to the bridge.
  - Python: action received → `client.enqueue` sent → reply received.
  - Durable (from event `ts`): `input.queued` → `turn.started` → first `message` of the turn;
    for interrupts `input.mode_changed` → `turn.cancelled`/terminal → next `turn.started`.
  - Client: event received → snapshot written → frame drawn (already partly traced via
    `event_sent_at`).
- Add a small devtools script (`nexus/devtools/`, plus a `docs/module-map.md` row) that reads an
  exported session JSONL and prints those durable intervals, so real sessions can be checked
  without the TUI.
- Record a table of p50/p95 for: idle root, busy root (steer), worktree root first message,
  interrupt during a model stream, interrupt during a running shell tool.

### 4.2 Fixes, applied only for the hypotheses the numbers confirm

- For H1: draw the user card from the durable `input.queued` record as soon as it is reduced,
  marked pending (`queued`, `starting…`) until `turn.started`, then it becomes the turn's prompt
  card (same id, no jump). This is a reducer/projection change (`nexus/view/reduce.py` input
  handling around `:1341`, `:1428`; `prototype.py` `_queue_items`/turn projection), it is
  replay-safe, and needs no optimistic client state.
- For H2: make `submit` not wait behind other bridge work (dispatch it first or on its own task with
  ordering per session), and send an immediate snapshot after the enqueue reply. If the round trip
  itself is slow, add an **optimistic** Rust row keyed by `client_request_id` (already sent,
  `actions.py:599-604`) labelled `sending…`, removed when the matching `input.queued` arrives or
  on error (the draft is restored via `composer_restore` already).
- For H3: on `input.mode_changed` to `interrupt`, the projection immediately shows the running turn
  as `interrupting…` (durable, replay-safe). Then look at the cancellation points that were slow
  (shell tool kill path, provider stream close) and at whether turn-end worktree work can run after
  the next turn is admitted without breaking commit ordering (risky; only with a test proving the
  commit still contains exactly the cancelled turn's paths).
- For H4: add `input.queued`, `input.mode_changed`, `input.started`, `input.dropped` and
  `turn.started` to the immediate set or make the fast path refresh `queue_*` fields.

### Host, events, persistence

- No new event types are expected. The trace spans are local and bounded. A pending card is a view
  of `input.queued`, which is already durable.

## 5. Tests and acceptance

- Python reducer/projection tests: an `input.queued` on an idle session yields a pending user card
  with a stable id that the `turn.started` keeps; replay gives the same blocks; `input.mode_changed`
  to interrupt yields `interrupting…` on the running turn.
- Bridge test: a `submit` issued while a slow poll is in flight reaches `client.enqueue` first
  (fake client with delays).
- Rust: optimistic row (only if H2 needs it) appears on Enter and is replaced, not duplicated.
- PTY timing check (tolerant): Enter → the text visible on screen within 200 ms with a scripted
  provider whose turn start is artificially delayed by 1 s.
- Acceptance:
  - [ ] Measurement table recorded in this plan before any fix.
  - [ ] A submitted message is visible within one frame in all measured cases.
  - [ ] A promotion to interrupt is visible within one frame; the time to the new turn is reported
        and, where a slow cancellation point was found, reduced.

## 6. Risks and open questions

### Host-only investigation ledger

Measured before any change with the main checkout's Python at
`/Users/santa/repos/nexus/.venv/bin/python` (this worktree has no `.venv`).
`python -m cProfile -o /tmp/plan18-before.prof -m pytest -q tests/test_host_facade.py
-k 'enqueue_runs_at_the_next_turn_boundary or queue_interrupt_cancels or
queue_set_mode_promotes or cancel_returns_pending' --durations=10` passed all
four selected tests. Profile totals below are cumulative active Python time
across the selected tests, **not** wall-clock request latency or p95; coroutine
resumptions count as calls and nested times overlap.

| Boundary | Profile cumulative time | Calls/resumptions |
| --- | ---: | ---: |
| Host enqueue | 2.470 ms | 8 |
| Host set queued mode | 0.359 ms | 4 |
| Host cancel | 0.412 ms | 3 |
| Supervisor submit | 122.129 ms | 16 |
| Supervisor start | 186.932 ms | 14 |

These scripted paths do not reproduce a one-second host delay. There is no
evidence here to change cancellation, title generation, compaction or Git work.
A new gate-controlled host regression proves that an admitted prompt is already
in `ConversationView.input_queue` while `start_turn` is blocked and the enqueue
reply is unfinished; its content/mode and replay agree, then it disappears on
consumption. Integration should render that existing contract as **Waiting**,
with the current mode, keyed by `queued_id`. No new host field is necessary.

Final targeted run: five tests passed in 0.81 s (whole test call times: idle
enqueue 130 ms, interrupt 100 ms, promotion/removal 180 ms, gated pending 90 ms,
cancel 30 ms; these include setup/cleanup, not visibility latency). Wider host
facade/supervisor run: 96 passed, one unrelated failure in the exhaustive command
inventory, missing `ProjectDirectoryList` in
`test_protocol_round_trips_every_command_and_result`; it does not execute the
new pending test or scheduling path. No UI files were changed in this work.

Still unmeasured: actual daemon transport/PTY frame timestamps, manager admission,
worktree setup and p95 screen visibility. The acceptance boxes above remain
unchecked; do not archive this plan on the strength of these host tests.

- Q1. Is the lag seen on the manager session in particular? (Manager turn admission runs idle
  compaction, `docs/manager.md:229-243`.) Measure it separately.
- Q2. Is the lag seen only in worktree sessions? (Worktree creation at the first message, and turn
  end Git work.)
- Risk: showing a pending card before the turn starts must not imply the model has seen it; label it
  as waiting, as the queue rows do.
- Risk: moving turn-end Git work off the critical path can reorder commits; do not do it without a
  test.

### Finisher baseline measurements (no performance changes)

Command: `PYTHONPATH=.:tests python tests/tui_responsiveness_bench.py --label
plan18-finisher --native --live --output /tmp/nexus-plan18-finisher` completed.
Artifacts are outside repository, JSON at `/tmp/nexus-plan18-finisher/plan18-finisher.json`.
Native 10-turn key→frame p50/p95/max 16.931/18.031/18.031 ms (n=8);
500-turn 18.590/615.733/615.733 ms (n=14). Snapshot→frame medians
18.039/26.174 ms respectively; maxima 1142.867/1029.445 ms include startup.
Disclosure click→frame p50/p95 16.441/18.004 ms (10), 16.971/21.384 ms (500).
Draw p95 5.415/8.285 ms. Live harness completes but reports “No trace” and
“No Python trace” for both sizes: it does NOT provide interrupt/message latency.
Therefore no causal interrupt diagnosis or safe performance fix is supported yet.
No runtime performance change made. Plan requires regressions and before/after
message/interrupt evidence, so remains OPEN, not investigated-closed or archived.

### Live fixture measurement continuation

Found the reason prior live runs published no trace: the headless PTY never
answers graphics negotiation; the scripted stream finishes before startup and
quit is sent before the native loop is ready. Fixture now explicitly selects
halfblocks, as existing PTY tests already do. No production scheduling change.
`live_benchmark(10/500, '/tmp/nexus-live-recheck')` now gracefully exits and writes
native/Python traces; results saved at `/tmp/nexus-live-recheck/results.json`.

| Scripted real-host workload | event→frame p50/p95 ms | stream→frame p50/p95 ms | snapshot→frame p50/p95 ms |
| --- | --- | --- | --- |
| 10 history turns | 27.677 / 36.954 | 27.680 / 38.251 | 10.078 / 19.622 |
| 500 history turns | 34.784 / 53.806 | 34.622 / 47.630 | 16.453 / 28.880 |

This uses in-process facade transport, not a daemon socket. Startup maxima remain
603–669 ms; Python projection p95 1.245/3.028 ms, maximum 7.838/155.271 ms.
These are streaming-event measurements, NOT Enter→message or interrupt-promotion
measurements. Those acceptance scenarios remain open; numbers do not justify
changing cancellation/Git ordering. No false latency-completion claim or archive.

### Deterministic admission/cancellation cases

Added reusable `scenario_benchmark` plus real-host fixture modes idle, worktree,
interrupt, shell. Ran all four (single samples, NOT p95): Enter→prompt bytes
69.868 ms idle, 131.423 ms first worktree-tool turn; steer→interrupt facade reply
1.218 ms with provider stream cancellation→idle 51.428 ms; running `sleep 10`
shell promotion reply 0.870 ms, cancellation→idle 54.108 ms. Artifacts and native
traces: `/tmp/nexus-live-recheck/scenarios.json` and `scenario-*.ansi/.log`.
The draft buffer is cleared before Enter measurement, so composer echo is excluded.
Worktree setup follows the actual `worktree_create` tool path. The initial attempt
to promote inside start_turn was reentrant and was removed before final measurement.
These are in-process facade, local native PTY cases, not external-provider or daemon
socket timings. Promotion visibility still needs operation-specific frame correlation;
aggregate event→frame traces cannot honestly prove that exact frame. No performance
change is warranted from these single samples. Plan remains open for p95 acceptance.

### Twenty-sample continuation

Ran 20 samples of each scenario through `scenario_benchmark`; durable sample
JSON at `/tmp/nexus-scenario-repeat/samples.json`, per-sample ANSI/native traces
in sibling directories. p95 uses sorted index floor(n*.95), capped at n-1 (with
20 samples this is the maximum); values in ms:

| Scenario/boundary | p50 | p95 | n |
| --- | ---: | ---: | ---: |
| Idle Enter→prompt terminal bytes | 72.597 | 78.944 | 20 |
| First worktree-tool turn Enter→prompt bytes | 127.214 | 135.465 | 20 |
| Provider steer→interrupt facade reply | 0.804 | 1.167 | 20 |
| Provider promotion→idle | 43.739 | 50.073 | 20 |
| Running shell promotion reply | 0.552 | 0.898 | 20 |
| Running shell promotion→idle | 44.951 | 56.828 | 20 |

Fixed measurement buffer race exposed by repetition (immutable output snapshots
instead of resizing a shared bytearray while decoding). One aborted interrupt
sample is excluded, not treated as a successful latency sample. First-message
worktree visibility exceeds the requested 100ms target; no claim of completion.
Interrupt uses facade operation start, not a native click; event-specific rendered
promotion marker correlation remains unimplemented. Existing aggregate frame traces
are insufficient to prove the marker frame. These are honest substantive gaps, so
plan 18 remains open; there is no evidence to weaken durability or cancellation.
