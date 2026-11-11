# 25. Alt+Enter: interrupt and send

User request (2026-11-11): Alt+Enter interrupts. Old Nexus had it; port it deliberately.

## How old Nexus did it (`nexus01`, `~/repos/nexus-old`)

- TUI `rust/tui/src/main.rs` ~2284: Enter with text in the composer → `{"type": "submit", "mode": ...}` where plain
  Enter = `steer`, Ctrl+Enter = `queue`, **Alt+Enter = `interrupt`**; Shift+Enter / Ctrl+J insert a newline.
- Core `nexus/host/facade.py` ~856–920 and `nexus/session/session.py:167` (`_MODE_RANK = {"queue": 0, "steer": 1,
  "interrupt": 2}`): an `interrupt` submit cancels the running turn and starts a new turn with the message at the
  front (`priority=True`).
- Help text: "Alt+Enter interrupts" / "interrupt the running turn and send".

So the meaning is **interrupt and send this message**, not a second Esc.

## Today in this repo

- `session.interrupt` = Esc (`escape_chain`); pending steers and queued messages come back to the composer.
- `composer.steer` = Enter, `composer.queue` = Tab, newline = Shift+Enter (Ctrl+J fallback in the TUI).
- **Conflict:** NSH binds `alt+enter` to `waiting.allow_session` in the `waiting` key context
  (`interface/manifest.toml` ~2103; the TUI uses `a`).
- API: `POST /api/session/{id}/prompt {text, mode}` with `send` / `steer` / `queue`, and `POST …/interrupt`.

## Plan

- [ ] **Core:** add `mode: "interrupt"` to `POST …/prompt`. Atomic on the server: cancel the running turn the same way
  `/interrupt` does, then start a turn with this message. Decide what happens to existing pending steers and queued
  messages (default: as Esc today, returned to the client, which puts them back in the composer *after* the sent
  message is gone; or keep the queue and run it after: **ask the user**). When idle, `interrupt` behaves like `send`.
  Mutation matrix and idempotency in `docs/api.md`; tests for busy, idle, awaiting approval/question (a waiting
  approval is denied/cancelled as Esc does), and a race with the turn finishing.
- [ ] **Manifest:** new `composer.interrupt_send` ("Interrupt and send"), keys `alt+enter` in both clients,
  `key_context = "composer"`, `when = ["session.busy"]` (idle: falls through to send). Move NSH
  `waiting.allow_session` off `alt+enter` (proposal: `cmd+shift+enter`) or confirm the contexts never overlap; run
  `check_manifest.py`.
- [ ] **TUI:** detect Alt+Enter in `composer.rs`/`keys.rs` (Ghostty sends `ESC CR`; with the kitty keyboard
  protocol it is a modified Enter); route to the new command. Composer hint line shows it while busy.
- [ ] **NSH:** the same key in `composer.tsx`; on web check the browser does not eat Alt+Enter; macOS Option+Enter.
  Mobile gets it as a long-press on Send (or a menu item), since there is no Alt key: ask before adding.
- [ ] Timeline: the interrupted turn shows its normal interrupted state, then the user message; nothing hidden.
- [ ] Docs: `docs/loop.md` (interrupt), `docs/api.md`, both READMEs, ledger row ("ported from old Nexus").
- [ ] Tests: Core API tests; TUI behaviour test (Alt+Enter while busy sends and interrupts); NSH browser test.
