# Plan 18: Remove the delay before a new message (and a steer→interrupt promotion) shows up

Status: proposed (2026-10-25). Nothing in this plan is implemented, and **the cause is not yet
confirmed**: this plan starts with measurement. Source: root `TODO.md` item 11, quoted unchanged:

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

- Q1. Is the lag seen on the manager session in particular? (Manager turn admission runs idle
  compaction, `docs/manager.md:229-243`.) Measure it separately.
- Q2. Is the lag seen only in worktree sessions? (Worktree creation at the first message, and turn
  end Git work.)
- Risk: showing a pending card before the turn starts must not imply the model has seen it; label it
  as waiting, as the queue rows do.
- Risk: moving turn-end Git work off the critical path can reorder commits; do not do it without a
  test.
