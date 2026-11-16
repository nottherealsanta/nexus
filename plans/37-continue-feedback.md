# 37. `/continue`: show it at once

User report (2026-11-11): `/continue` takes a few seconds before anything shows. The UI should react immediately, even
when Core takes a while (to start, to accept, or to get the model's first output). Both clients in the same change;
mobile too (it has all slash commands).

## What the data says (read-only, `~/.nexus/nexus.db`, last 3 days)

- **Core is not the slow part once it has the request.** For recent `/continue` calls the `session.status busy`
  event is published 0–1 ms after the mutation is accepted, and the first request is built 10–20 ms later.
- **Time to the provider and to the first token is the wait:**

  | Started (local) | Model | Request start → on the wire | Time to first byte |
  | --- | --- | --- | --- |
  | 11-09 05:57 | copilot/claude-sonnet-5.5 | 0.0 s | 3.2 s |
  | 11-09 15:17 | copilot/claude-sonnet-5.5 (119K input) | 0.0 s | 4.3 s |
  | 11-10 09:02 | codex/gpt-6.1-sol | 0.0 s | 0.9 s |
  | 11-11 15:53 | codex/gpt-6.1-sol | 0.6 s | 2.1 s |
  | 11-11 18:16 | codex/gpt-6.1-sol | **37.8 s**, then `ConnectTimeout` | — |
  | 11-11 18:16 | codex/gpt-6.1-sol | **47.9 s** | 65 s |

  So a normal `/continue` waits 1–4 s for the model's first byte, and during network trouble 40–65 s
  (the network side is [34](34-network-errors-no-fallback.md)).
- **The clients show nothing until Core's `busy` event arrives.** TUI `app.rs` ~6817 just sends
  `ApiCall::Continue`; NSH `commands.ts` `continueSession()` awaits `POST …/continue` and only toasts on error. Neither
  shows a pending state. If Core is not connected yet (starting, reconnecting), the call fails or waits invisibly.
- Not yet measured: the client side (command → HTTP sent → `busy` received over SSE). Add that measurement first
  (step 1) so the fix is checked against numbers.

## Plan

- [~] **Measure the client path** in both clients behind a debug flag: time from the command to the request being
  sent, to the 202, to the `busy` event, to the first visible delta. Record five runs each with a scratch Core and a
  scripted provider (instant and 3 s first byte), plus one with Core still starting.
- [x] **Immediate pending state (optimistic):** the moment `/continue` runs, the session shows a **Continuing…** row
  where the working row appears (spinner, elapsed time), the status strip clears, and the composer behaves as busy
  (Enter steers, Tab queues, as during a turn). No message is added to the timeline: `/continue` adds no user
  message, and the model has received nothing yet.
- [x] **Phases on that row, all from real events:** `Waiting for Core…` (not connected or no 202 yet) →
  `Waiting for the model…` (after `busy`, before the first delta; shows elapsed seconds) → the normal live turn.
  With [34](34-network-errors-no-fallback.md): `Waiting for network · retrying in Ns`.
- [x] **Core not ready:** if Core is starting or reconnecting, keep the command (with its idempotency key, which
  `/continue` already sends) and send it once connected; show `Waiting for Core…`. The key makes a resend after a
  reconnect safe (`/continue` is in `_KEYED` in `server.py`). Esc cancels the pending command before it is sent.
- [x] **Failure:** Core refusing (`Conflict`: running turn, unfinished durable turn, ZMX, archived, empty session)
  removes the pending row at once and shows the reason in the status strip ([21](done/21-session-status.md)), not only a
  toast. A timeout with no 202 after e.g. 10 s says so and offers Retry.
- [x] **Same treatment for `/prompt` sends?** The user message already appears immediately; check whether the gap
  until the first output also needs the `Waiting for the model…` phase. Default: yes, the same row for every turn
  start, so `/continue` is not a special case.
- [x] **Core (small):** nothing is required for correctness. Optional: `continue_session` loads every stored message
  only to check that one exists (`self.store.messages(session_id)`, `runtime.py` ~2991); replace it with an existence
  check so very long sessions don't pay for it.
- [x] Tests: TUI behaviour test and NSH browser test with a scripted Core that delays the 202 and the first delta (the
  pending row appears in the same frame as the command; phases change in order; a refusal clears it); Core unit test
  for the existence check if done. Matching screenshots of each phase.
- [x] Docs: `docs/api.md` (`/continue` client behaviour note), both READMEs, `interface/design-language.md` (working
  row phases).


## Landed 2026-11-16 (still open)

Both clients (and mobile NSH) show the pending row, phases, queueing with one idempotency key, Esc cancel, refusal strip and Retry after 10 s; `/prompt` sends share the row (D3); Core uses `Store.has_messages`. Debug timing: TUI `NEXUS_TUI_TIMING=<file>`, NSH pref `debug.timing`. Tests: `nexus-tui/tests/pending_turn.rs`, `nexus-nsh/pending-turn.test.tsx`, `tests/test_continuation.py`. Captures: `reports/37-continue-feedback/`.

Not done: the five recorded timing runs per client against a scripted delayed Core (the flag exists; the numbers are not recorded), so the plan stays open on that item. Not verified on a real device or with a real provider.

Release-gate follow-up: fixed two NSH type errors in the pending-turn test network fixture and nullable event session ID. `npm run check` and `pending-turn.test.tsx` (5 tests) pass; timing measurements remain open.
